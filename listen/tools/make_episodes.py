"""Turn article URLs into MP3 episodes and rebuild the podcast feed.

Usage:
  python listen/tools/make_episodes.py URL [URL ...]
  python listen/tools/make_episodes.py --from-text "any text containing URLs"
  python listen/tools/make_episodes.py --feed-only      # just rebuild feed.xml
"""
import argparse
import asyncio
import html
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime

import edge_tts
import trafilatura
from mutagen.mp3 import MP3

from common import (AUDIO, CANDIDATES, EPISODES, FEED, fetch, load_json,
                    minutes_for, normalize_url, save_json, site, slugify, word_count)

# Lines that are Substack / newsletter chrome rather than the article.
BOILERPLATE = re.compile(
    r"^(subscribe( now)?|share|leave a comment|thanks for reading.*|"
    r"upgrade to paid|give a gift subscription|.*is a reader-supported publication.*|"
    r"read more|listen now|share this post|discussion about this post|"
    r"\d+ (likes?|comments?|restacks?))\.?$",
    re.I,
)


def clean_for_speech(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or BOILERPLATE.match(line):
            continue
        line = re.sub(r"https?://\S+", "", line)          # bare links are noise when spoken
        line = re.sub(r"\[\d+\]|\[\^?\d+\]", "", line)    # footnote markers
        line = re.sub(r"^[-*•]\s+", "", line)              # bullets
        line = re.sub(r"\s{2,}", " ", line).strip()
        if line:
            out.append(line)
    return "\n\n".join(out)


def extract(url):
    raw = fetch(url).decode("utf-8", errors="replace")
    doc = trafilatura.bare_extraction(raw, url=url, with_metadata=True,
                                      include_comments=False, include_tables=False,
                                      favor_recall=True)
    if not doc:
        raise RuntimeError("could not find article text on the page")
    doc = doc if isinstance(doc, dict) else doc.as_dict()
    text = clean_for_speech(doc.get("text") or "")
    if word_count(text) < 120:
        raise RuntimeError("page had almost no readable text (paywall or login wall?)")
    return {
        "title": (doc.get("title") or url).strip(),
        "author": (doc.get("author") or "").strip(),
        "source": (doc.get("sitename") or "").strip(),
        "text": text,
    }


def candidate_info(url):
    """Use the nicer feed metadata when the URL came from today's picks."""
    for c in load_json(CANDIDATES, {}).get("items", []):
        if c["url"] == url:
            return c
    return {}


def chunks(text, max_words=350):
    """Split at paragraph boundaries into pieces of roughly max_words."""
    out, cur, n = [], [], 0
    for para in text.split("\n\n"):
        w = word_count(para)
        if cur and n + w > max_words:
            out.append("\n\n".join(cur))
            cur, n = [], 0
        cur.append(para)
        n += w
    if cur:
        out.append("\n\n".join(cur))
    return out


async def speak(text, voice, path, parallel=8):
    """The free voice service streams at roughly real-time speed, so voice the
    article in chunks concurrently, then join the MP3 pieces (same encoding, so
    plain concatenation plays back as one file)."""
    parts = chunks(text)
    gate = asyncio.Semaphore(parallel)

    async def one(i, piece):
        async with gate:
            for attempt in range(3):
                try:
                    buf = bytearray()
                    async for msg in edge_tts.Communicate(piece, voice).stream():
                        if msg["type"] == "audio":
                            buf += msg["data"]
                    return bytes(buf)
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(5 * (attempt + 1))

    audio = await asyncio.gather(*(one(i, p) for i, p in enumerate(parts)))
    path.write_bytes(b"".join(audio))


def make_episode(url, cfg):
    url = normalize_url(url)
    art = extract(url)
    meta = candidate_info(url)
    title = meta.get("title") or art["title"]
    author = meta.get("author") or art["author"]
    source = meta.get("source") or art["source"]
    words = word_count(art["text"])
    paywalled = meta and not meta.get("full_text_in_feed", True) and words < 600

    intro = title + "."
    if author:
        intro += f" By {author}."
    if source:
        intro += f" From {source}."
    if paywalled:
        intro += " This is only the free preview of the article."
    script = intro + "\n\n" + art["text"] + "\n\nEnd of article."

    now = datetime.now(timezone.utc)
    fname = f"{now:%Y%m%d}-{slugify(title)}.mp3"
    AUDIO.mkdir(exist_ok=True)
    path = AUDIO / fname
    print(f"  speaking {words} words -> audio/{fname}", flush=True)
    asyncio.run(speak(script, cfg["voice"], path))

    return {
        "id": fname[:-4],
        "title": title,
        "author": author,
        "source": source,
        "source_url": url,
        "file": f"audio/{fname}",
        "bytes": path.stat().st_size,
        "seconds": int(MP3(path).info.length),
        "words": words,
        "created": now.isoformat(timespec="seconds"),
        "summary": meta.get("summary", art["text"][:260] + "…"),
    }


def build_feed(episodes, cfg):
    base = cfg["base_url"]
    esc = lambda s: html.escape(s or "", quote=True)
    items = []
    for ep in episodes:
        dur = ep["seconds"]
        desc = f"{ep['summary']}\n\nOriginal: {ep['source_url']}"
        by = " · ".join(x for x in (ep.get("author"), ep.get("source")) if x)
        items.append(f"""    <item>
      <title>{esc(ep['title'])}</title>
      <description>{esc(desc)}</description>
      <itunes:subtitle>{esc(by)}</itunes:subtitle>
      <link>{esc(ep['source_url'])}</link>
      <guid isPermaLink="false">{esc(ep['id'])}</guid>
      <pubDate>{format_datetime(datetime.fromisoformat(ep['created']))}</pubDate>
      <enclosure url="{esc(base + ep['file'])}" length="{ep['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{dur // 3600:02d}:{dur % 3600 // 60:02d}:{dur % 60:02d}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    now = format_datetime(datetime.now(timezone.utc))
    FEED.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{esc(cfg['title'])}</title>
    <link>{esc(base)}</link>
    <atom:link href="{esc(base)}feed.xml" rel="self" type="application/rss+xml"/>
    <description>{esc(cfg['description'])}</description>
    <language>en-us</language>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>{esc(cfg['title'])}</itunes:author>
    <itunes:image href="{esc(base)}cover.png"/>
    <image><url>{esc(base)}cover.png</url><title>{esc(cfg['title'])}</title><link>{esc(base)}</link></image>
    <itunes:category text="Technology"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
""")


def prune(episodes, keep):
    """Keep the newest `keep` episodes; delete older MP3s so the repo stays small."""
    for old in episodes[keep:]:
        (AUDIO.parent / old["file"]).unlink(missing_ok=True)
    return episodes[:keep]


def mark_done(urls):
    data = load_json(CANDIDATES, None)
    if data:
        for c in data.get("items", []):
            if c["url"] in urls:
                c["done"] = True
        save_json(CANDIDATES, data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--from-text", default="")
    ap.add_argument("--feed-only", action="store_true")
    args = ap.parse_args()
    cfg = site()

    urls = list(args.urls) + re.findall(r"https?://[^\s<>\"')\]]+", args.from_text)
    urls = list(dict.fromkeys(normalize_url(u) for u in urls))   # dedupe, keep order
    episodes = load_json(EPISODES, [])
    have = {ep["source_url"] for ep in episodes}

    made, failed = [], []
    for url in ([] if args.feed_only else urls):
        if url in have:
            print(f"skip (already in feed): {url}")
            continue
        print(f"article: {url}", flush=True)
        try:
            ep = make_episode(url, cfg)
            episodes.insert(0, ep)
            made.append(ep)
            print(f"  done: {ep['title']} ({ep['seconds'] // 60} min)")
        except Exception as exc:
            failed.append((url, str(exc)))
            print(f"  FAILED: {exc}", file=sys.stderr)

    episodes = prune(episodes, cfg["keep_episodes"])
    save_json(EPISODES, episodes)
    build_feed(episodes, cfg)
    mark_done({ep["source_url"] for ep in made})

    # Short report the GitHub workflow posts back on the request issue.
    lines = [f"- ✅ **{ep['title']}** ({max(1, ep['seconds'] // 60)} min)" for ep in made]
    lines += [f"- ❌ {u} — {why}" for u, why in failed]
    (AUDIO.parent / ".last_report.md").write_text("\n".join(lines) or "Nothing new to make.")
    if urls and not made and failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

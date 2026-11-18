"""Daily discovery: read the feeds, score against interests, write candidates.json.

Usage:  python listen/tools/discover.py
"""
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser

from common import (CANDIDATES, CONFIG, EPISODES, fetch, html_to_text, load_json,
                    minutes_for, normalize_url, save_json, site, word_count)


def read_feeds():
    feeds = []
    for line in (CONFIG / "feeds.txt").read_text().splitlines():
        url = line.split("#", 1)[0].strip()
        if url:
            feeds.append(url)
    return feeds


def read_interests():
    boosts, penalties, hides = [], [], []
    for line in (CONFIG / "interests.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line[0] not in "+-!":
            continue
        kind, rest = line[0], line[1:].strip()
        m = re.match(r"(.+?)\s+(\d+(?:\.\d+)?)$", rest)
        phrase, weight = (m.group(1), float(m.group(2))) if m else (rest, 1.0)
        phrase = phrase.strip().lower()
        if kind == "+":
            boosts.append((phrase, weight))
        elif kind == "-":
            penalties.append((phrase, weight))
        else:
            hides.append(phrase)
    return boosts, penalties, hides


def count(phrase, text):
    return len(re.findall(r"\b" + re.escape(phrase) + r"\b", text))


def score(title, body, boosts, penalties):
    t, b = title.lower(), body.lower()
    s, why = 0.0, []
    for phrase, w in boosts:
        hits = 3 * count(phrase, t) + min(count(phrase, b), 2)
        if hits:
            s += w * hits
            why.append(phrase)
    for phrase, w in penalties:
        hits = 3 * count(phrase, t) + min(count(phrase, b), 5)
        s -= w * hits
    return s, why


def entry_date(e):
    for key in ("published_parsed", "updated_parsed"):
        if e.get(key):
            return datetime.fromtimestamp(time.mktime(e[key]), tz=timezone.utc)
    return None


def summarize(text, limit=260):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


def main():
    cfg = site()
    boosts, penalties, hides = read_interests()
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["lookback_days"])
    done = {normalize_url(ep["source_url"]) for ep in load_json(EPISODES, [])}

    found, errors = [], []
    for url in read_feeds():
        try:
            feed = feedparser.parse(fetch(url))
        except Exception as exc:  # one bad feed shouldn't stop the rest
            errors.append(f"{url}: {exc}")
            continue
        source = feed.feed.get("title", url)
        for e in feed.entries:
            when = entry_date(e)
            if not when or when < cutoff or not e.get("link"):
                continue
            title = html_to_text(e.get("title", "")).strip()
            if any(h in title.lower() for h in hides):
                continue
            full = e["content"][0].get("value", "") if e.get("content") else ""
            body = html_to_text(full or e.get("summary", ""))
            words = word_count(body)
            s, why = score(title, body, boosts, penalties)
            # Newer is better: lose 0.5 points per day of age.
            age_days = (datetime.now(timezone.utc) - when).total_seconds() / 86400
            s -= 0.5 * age_days
            link = normalize_url(e["link"])
            found.append({
                "url": link,
                "title": title,
                "source": source,
                "author": e.get("author", ""),
                "published": when.isoformat(),
                "summary": summarize(html_to_text(e.get("summary", "")) or body),
                "words": words,
                "minutes": minutes_for(words),
                # A feed with a short body usually means a paywall or a teaser.
                "full_text_in_feed": words >= 400,
                "score": round(s, 1),
                "matched": why[:6],
                "done": link in done,
            })

    # One entry per URL, best first.
    best = {}
    for c in found:
        if c["url"] not in best or c["score"] > best[c["url"]]["score"]:
            best[c["url"]] = c
    ranked, per_source = [], {}
    for c in sorted(best.values(), key=lambda c: c["score"], reverse=True):
        # Keep the list varied: at most N picks from any one publication.
        if per_source.get(c["source"], 0) >= cfg.get("max_per_source", 3):
            continue
        per_source[c["source"]] = per_source.get(c["source"], 0) + 1
        ranked.append(c)
    ranked = ranked[: cfg["max_candidates"]]

    save_json(CANDIDATES, {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "feeds_checked": len(read_feeds()),
        "errors": errors,
        "items": ranked,
    })
    print(f"{len(ranked)} candidates written ({len(found)} recent articles seen, "
          f"{len(errors)} feed errors)")
    for err in errors:
        print("  feed error:", err, file=sys.stderr)


if __name__ == "__main__":
    main()

"""Shared helpers for the Listen Later pipeline."""
import json
import re
import ssl
import os
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # .../listen
CONFIG = ROOT / "config"
AUDIO = ROOT / "audio"
CANDIDATES = ROOT / "candidates.json"
EPISODES = ROOT / "episodes.json"
FEED = ROOT / "feed.xml"

UA = "Mozilla/5.0 (X11; Linux x86_64) ListenLater/0.1 (personal reading queue)"


def site():
    return json.loads((CONFIG / "site.json").read_text())


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _ssl_context():
    # Honour a custom CA bundle when one is set (e.g. behind a corporate proxy).
    cafile = os.environ.get("SSL_CERT_FILE")
    return ssl.create_default_context(cafile=cafile) if cafile else None


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        return r.read()


def slugify(text, maxlen=60):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].rstrip("-") or "article"


def html_to_text(html):
    """Very small HTML -> plain-text conversion for feed summaries."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    import html as _h
    text = _h.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def word_count(text):
    return len(re.findall(r"\w+", text or ""))


def minutes_for(words):
    # Neural TTS reads ~160 words per minute.
    return max(1, round(words / 160))


def normalize_url(url):
    url = url.strip()
    url = re.sub(r"[?#].*$", "", url)        # drop tracking params / anchors
    return url.rstrip("/")

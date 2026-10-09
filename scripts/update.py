#!/usr/bin/env python3
"""Collects Ninjago leak/rumour posts from public feeds into data/leaks.json.

Only uses the Python standard library, so there is nothing to install.
Run it by hand:   python scripts/update.py
GitHub Actions runs it automatically (see .github/workflows/update.yml).
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "leaks.json"

# ---------------------------------------------------------------------------
# SOURCES: add or remove feeds here. "tag" is the default label for the source.
#   rumour   = unconfirmed leak/rumour
#   official = revealed by LEGO / a retailer
#   news     = news site writing up leaks
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "name": "r/Legoleak",
        "url": "https://www.reddit.com/r/Legoleak/search.rss?q=ninjago&restrict_sr=1&sort=new",
        "tag": "rumour",
        "filter": False,  # the search already filters for ninjago
    },
    {
        "name": "r/lego",
        "url": "https://www.reddit.com/r/lego/search.rss?q=ninjago+leak&restrict_sr=1&sort=new",
        "tag": "rumour",
        "filter": False,
    },
    {
        "name": "Brick Fanatics",
        "url": "https://www.brickfanatics.com/feed/",
        "tag": "news",
        "filter": True,
    },
    {
        "name": "Brickset",
        "url": "https://brickset.com/feed",
        "tag": "official",
        "filter": True,
    },
    {
        "name": "r/ninjago",
        "url": "https://www.reddit.com/r/ninjago/search.rss?q=leak+OR+leaked+OR+rumor+OR+rumour&restrict_sr=1&sort=new",
        "tag": "rumour",
        "filter": False,
    },
    {
        "name": "Promobricks",
        "url": "https://promobricks.de/feed/",
        "tag": "news",
        "filter": True,
    },
    {
        "name": "Jay's Brick Blog",
        "url": "https://jaysbrickblog.com/feed/",
        "tag": "news",
        "filter": True,
    },
    {
        "name": "Stonewars",
        "url": "https://stonewars.com/feed/",
        "tag": "rumour",
        "filter": True,
    },
]

KEYWORD = re.compile(r"ninjago", re.I)
SET_NUMBER = re.compile(r"\b(71[0-9]{3})\b")
OFFICIAL_WORDS = re.compile(r"\b(officially|revealed|now available|available now|launches)\b", re.I)
MAX_ITEMS = 300
USER_AGENT = "ninjago-leaks-page/1.0 (personal hobby project)"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            # "Too many requests": wait a bit and try again
            if exc.code == 429 and attempt < 2:
                time.sleep(8 * (attempt + 1))
                continue
            raise


def clean(text):
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_ns(tag):
    return tag.split("}", 1)[-1]


def child_text(node, name):
    for child in node:
        if strip_ns(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    # Atom / ISO style
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # RSS (RFC 822) style
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_feed(xml_bytes):
    """Returns a list of dicts with title, url, summary, published (datetime|None)."""
    root = ET.fromstring(xml_bytes)
    items = []
    for node in root.iter():
        kind = strip_ns(node.tag)
        if kind not in ("item", "entry"):
            continue
        title = clean(child_text(node, "title"))
        link = child_text(node, "link")
        if not link:  # Atom puts the link in an attribute
            for child in node:
                if strip_ns(child.tag) == "link" and child.get("href"):
                    link = child.get("href")
                    break
        summary = clean(
            child_text(node, "description")
            or child_text(node, "summary")
            or child_text(node, "content")
            or child_text(node, "encoded")
        )
        published = parse_date(
            child_text(node, "pubDate")
            or child_text(node, "published")
            or child_text(node, "updated")
        )
        if title and link:
            items.append(
                {"title": title, "url": link, "summary": summary, "published": published}
            )
    return items


def decide_tag(source, title, summary):
    if source["tag"] in ("rumour", "official"):
        return source["tag"]
    # news sites: look at the headline wording
    if re.search(r"rumou?r|leak|gerücht|gerucht", title, re.I):
        return "rumour"
    if OFFICIAL_WORDS.search(title):
        return "official"
    return "news"


def load_existing():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"updated": None, "items": []}


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    existing = load_existing()
    by_url = {item["url"]: item for item in existing.get("items", [])}
    report = []

    for source in SOURCES:
        time.sleep(2)  # be polite to the sites
        try:
            parsed = parse_feed(fetch(source["url"]))
        except Exception as exc:  # one broken source must not stop the others
            report.append(f"  FAILED  {source['name']}: {exc}")
            continue

        added = 0
        for entry in parsed:
            haystack = f"{entry['title']} {entry['summary']}"
            if source["filter"] and not KEYWORD.search(haystack):
                continue
            url = entry["url"]
            if url in by_url:
                continue
            published = entry["published"].isoformat() if entry["published"] else now
            by_url[url] = {
                "title": entry["title"],
                "url": url,
                "source": source["name"],
                "tag": decide_tag(source, entry["title"], entry["summary"]),
                "summary": entry["summary"][:280],
                "set_numbers": sorted(set(SET_NUMBER.findall(haystack))),
                "published": published,
                "first_seen": now,
            }
            added += 1
        report.append(f"  ok      {source['name']}: {len(parsed)} read, {added} new")

    items = sorted(by_url.values(), key=lambda i: i["published"], reverse=True)[:MAX_ITEMS]
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps({"updated": now, "items": items}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\n".join(report))
    print(f"Saved {len(items)} items to {DATA_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())

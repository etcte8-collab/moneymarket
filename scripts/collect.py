#!/usr/bin/env python3
"""moneymarket collector: pull last-24h items from verified free feeds.

Stdlib only (no pip on this host). Emits JSON to stdout or --out.

  python3 scripts/collect.py --lane tech --lane stock --hours 24 --out cache/collect.json
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0 moneymarket/0.1"

# Verified reachable from this host on 2026-10-16 (HTTP 200 + real feed body).
FEEDS = {
    "tech": [
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("Ars Technica", "https://arstechnica.com/feed/"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
        ("The Information", "https://www.theinformation.com/feed"),
        ("The Next Platform", "https://www.nextplatform.com/feed/"),
        ("Hacker News (front)", "https://hnrss.org/frontpage"),
    ],
    "money": [
        ("CNBC Markets", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
        ("Bloomberg Economics", "https://feeds.bloomberg.com/economics/news.rss"),
        ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
        ("Dow Jones Newswires", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
        ("MarketWatch Top Stories", "https://www.marketwatch.com/rss/topstories"),
        ("Investing.com", "https://www.investing.com/rss/news_301.rss"),
    ],
    "politics": [
        ("NYT Politics", "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml"),
        ("NPR Politics", "https://feeds.npr.org/1013/rss.xml"),
        ("NPR News", "https://feeds.npr.org/1001/rss.xml"),
        ("WSJ World News", "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
        ("Dow Jones World News", "https://feeds.content.dowjones.io/public/rss/RSSWorldNews"),
        ("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ],
    "stock": [
        ("WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
        ("Dow Jones Newswires", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
        ("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("CNBC Markets", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
        ("MarketWatch Top Stories", "https://www.marketwatch.com/rss/topstories"),
        ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
        ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("Investing.com", "https://www.investing.com/rss/news_301.rss"),
    ],
}

# Google News RSS is the highest-yield source for a strict 24h window.
GNEWS = {
    "tech": [
        "semiconductor OR AI chips OR Nvidia when:1d",
        "big tech OR earnings OR startup funding when:1d",
        "artificial intelligence regulation when:1d",
    ],
    "stock": [
        "stock market close S&P 500 Nasdaq when:1d",
        "earnings report company results when:1d",
        "IPO OR buyback OR analyst upgrade when:1d",
    ],
    "money": [
        "Federal Reserve rate decision OR treasury yields when:1d",
        "inflation CPI PPI consumer prices when:1d",
        "dollar OR bitcoin OR gold OR oil market when:1d",
    ],
    "politics": [
        "Washington Congress policy markets when:1d",
        "tariffs sanctions trade deal when:1d",
        "election polls midterms when:1d",
    ],
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean(text: str | None, limit: int = 480) -> str:
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub(" ", text))
    text = WS_RE.sub(" ", text).strip()
    return text[:limit].rsplit(" ", 1)[0] + ("..." if len(text) > limit else "")


def fetch(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def items_from_xml(blob: bytes, source: str, feed: str) -> list[dict]:
    root = ET.fromstring(blob)
    out: list[dict] = []
    nodes = [n for n in root.iter() if localname(n.tag) in {"item", "entry"}]
    for node in nodes:
        title, link, desc, published = "", "", "", None
        for child in node:
            name = localname(child.tag)
            text = (child.text or "").strip()
            if name == "title":
                title = clean(text, 240)
            elif name == "link":
                if child.get("href"):
                    link = child.get("href")
                elif text:
                    link = text
            elif name in {"description", "summary", "content", "encoded"}:
                if not desc:
                    desc = clean(text)
            elif name in {"pubDate", "published", "updated", "date"}:
                parsed = parse_date(text)
                if parsed and (published is None or name in {"published", "pubDate"}):
                    published = parsed
        if not title:
            continue
        out.append(
            {
                "title": title,
                "link": link,
                "summary": desc,
                "published_utc": published.isoformat() if published else None,
                "published_ts": published.timestamp() if published else None,
                "source": source,
                "feed": feed,
            }
        )
    return out


def gnews_url(query: str) -> str:
    from urllib.parse import quote

    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


def collect(lanes: list[str], hours: float, per_feed_cap: int) -> tuple[list[dict], list[dict]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept: list[dict] = []
    log: list[dict] = []
    jobs: list[tuple[str, str, str]] = []
    for lane in lanes:
        for name, url in FEEDS.get(lane, []):
            jobs.append((lane, name, url))
        for query in GNEWS.get(lane, []):
            jobs.append((lane, f"Google News: {query.split(' when')[0]}", gnews_url(query)))

    for lane, name, url in jobs:
        try:
            blob = fetch(url)
            items = items_from_xml(blob, name, url)
        except Exception as exc:  # noqa: BLE001 - report, never abort the run
            log.append({"lane": lane, "source": name, "status": f"error: {type(exc).__name__}: {exc}"[:200], "total": 0, "fresh": 0})
            continue
        fresh = [i for i in items if i["published_ts"] and cutoff.timestamp() <= i["published_ts"]]
        fresh.sort(key=lambda i: i["published_ts"], reverse=True)
        fresh = fresh[:per_feed_cap]
        for item in fresh:
            item["lane_hint"] = lane
        kept.extend(fresh)
        log.append({"lane": lane, "source": name, "status": "ok", "total": len(items), "fresh": len(fresh)})
        time.sleep(0.35)
    return kept, log


def dedupe(items: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for item in items:
        key = re.sub(r"[^a-z0-9]+", "", item["title"].lower())[:70]
        if not key:
            continue
        prev = seen.get(key)
        if prev is None or (item["published_ts"] or 0) > (prev["published_ts"] or 0):
            seen[key] = item
    return sorted(seen.values(), key=lambda i: i["published_ts"] or 0, reverse=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", action="append", choices=sorted(FEEDS), required=True)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--per-feed-cap", type=int, default=25)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    items, log = collect(args.lane, args.hours, args.per_feed_cap)
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "window_hours": args.hours,
        "lanes": args.lane,
        "feeds": log,
        "counts": {"raw": len(items)},
        "items": {lane: dedupe([i for i in items if i["lane_hint"] == lane]) for lane in args.lane},
    }
    payload["counts"]["deduped"] = sum(len(v) for v in payload["items"].values())
    text = json.dumps(payload, indent=1, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out}: {payload['counts']}")
        for entry in log:
            print(f"  [{entry['lane']}] {entry['source']}: {entry['status']} total={entry['total']} fresh={entry['fresh']}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

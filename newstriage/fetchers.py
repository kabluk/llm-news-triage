"""Fetchers, selected by `method` in sources.yaml. Each returns raw items (dicts)
without full article text: URL, title, outlet, author, dates, a <=300-char
excerpt, and hints for classification."""
from __future__ import annotations

import gzip
from datetime import datetime
from urllib.parse import urljoin

import feedparser

from .http import FetchError, Http
from .util import parse_dt, snippet, strip_html


def _raw(src, url, title, **kw) -> dict:
    return {
        "source_id": src["id"], "source_role": src.get("role"), "source_type": src.get("type"),
        "url": url, "title": strip_html(title), "outlet": kw.pop("outlet", None) or src.get("outlet") or src["name"],
        "author": kw.pop("author", None), "published_at": kw.pop("published_at", None),
        "event_date": kw.pop("event_date", None), "snippet": snippet(kw.pop("snippet", "")),
        "extra": kw.pop("extra", {}) or {}, **kw,
    }


def _body(content: bytes) -> bytes:
    """Some CDNs intermittently return a gzip body without Content-Encoding."""
    if content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content


def fetch_rss(src, http: Http, now: datetime) -> list[dict]:
    """RSS 2.0 or Atom feed published by the source itself."""
    r = http.get(src["url"], delay=src.get("crawl_delay"), timeout=src.get("timeout"))
    feed = feedparser.parse(_body(r.content))
    if feed.bozo and not feed.entries:
        raise FetchError("parse", f"feed not parsed: {feed.get('bozo_exception')}")
    out = []
    for e in feed.entries[: src.get("max_items") or None]:
        link = urljoin(src.get("homepage") or src["url"], e.get("link", ""))
        pub = parse_dt(e.get("published_parsed") or e.get("updated_parsed") or e.get("published"))
        out.append(_raw(src, link, e.get("title", ""), author=e.get("author"), published_at=pub,
                        snippet=e.get("summary", ""), extra={"signal_only": True} if src.get("role") == "signal" else {}))
    return out


FETCHERS = {
    "rss": fetch_rss,
}

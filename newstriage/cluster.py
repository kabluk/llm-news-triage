"""Deduplication and grouping of items into events."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .util import canonical_url, jaccard, tokens


@dataclass
class EventRef:
    id: int
    keys: set[str] = field(default_factory=set)
    title_tokens: list[set[str]] = field(default_factory=list)
    last_date: datetime | None = None
    title_numbers: list[set[str]] = field(default_factory=list)


def dedupe(items: list[dict], known_canonical: set[str]) -> tuple[list[dict], list[dict]]:
    """Splits items into new and duplicates: by canonical URL, and within one
    run also by (outlet, normalized title)."""
    new, dup = [], []
    seen_urls = set(known_canonical)
    seen_titles = set()
    for it in items:
        cu = canonical_url(it["url"])
        it["url_canonical"] = cu
        tkey = ((it.get("outlet") or "").lower(), " ".join(sorted(tokens(it["title"]))))
        if not cu or cu in seen_urls or (tkey[1] and tkey in seen_titles):
            dup.append(it)
            continue
        seen_urls.add(cu)
        seen_titles.add(tkey)
        new.append(it)
    return new, dup


NUM = re.compile(r"\b\d+(?:[.,]\d+)*\b")


def _numbers(title: str) -> set[str]:
    return set(NUM.findall(title or ""))


def match_event(item: dict, events: list[EventRef], rules: dict, now: datetime) -> tuple[int | None, str]:
    """Finds the event an item belongs to. First a shared strong key (configured
    in rules.yaml event_keys, e.g. an arXiv id), then title similarity within
    window_days. Numbers in titles must match when both titles have them
    ("Model 3" and "Model 4" are different releases)."""
    cfg = rules["clustering"]
    window = timedelta(days=cfg["window_days"])
    keys = set(item.get("keys") or [])
    tt = tokens(item["title"])
    nums = _numbers(item["title"])
    best, best_score = None, 0.0
    for ev in events:
        if ev.last_date and now - ev.last_date > window:
            continue
        shared = keys & ev.keys
        if shared:
            return ev.id, f"shared key {sorted(shared)[0]}"
        for k, et in enumerate(ev.title_tokens):
            en = ev.title_numbers[k] if k < len(ev.title_numbers) else set()
            if nums and en and nums != en:
                continue
            j = jaccard(tt, et)
            if j >= cfg["title_jaccard"] and len(tt & et) >= cfg["min_shared_tokens"] and j > best_score:
                best, best_score = ev.id, j
    if best is not None:
        return best, f"title similarity {best_score:.2f}"
    return None, "new event"

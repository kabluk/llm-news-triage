"""Relevance filter, document type and status, event keys. All vocabularies
live in rules.yaml so the topic can be changed without touching code."""
from __future__ import annotations

import re

from .util import any_match


def text_of(item: dict) -> str:
    return f"{item.get('title', '')}. {item.get('snippet', '')}"


def _term_hits(terms, text: str, case_sensitive=()) -> list[str]:
    """Word-prefix match ("releas" hits "released"); terms listed in
    case_sensitive must match as whole words with exact case ("AI" but not "aid")."""
    hits = []
    for x in terms or []:
        if x in case_sensitive:
            if re.search(r"\b" + re.escape(x) + r"\b", text):
                hits.append(x)
        elif re.search(r"\b" + re.escape(x), text, re.I):
            hits.append(x)
    return hits


def is_relevant(item: dict, rules: dict, mode: str = "keywords") -> tuple[bool, str]:
    """mode: all - keep everything; strong - only strong phrases;
    keywords - a strong phrase, or (a core term AND a context term)."""
    if mode == "all":
        return True, "all"
    rel = rules["relevance"]
    cs = set(rel.get("case_sensitive") or [])
    t = text_of(item)
    if any_match([re.escape(x) for x in rel.get("exclude", [])], t):
        return False, "excluded"
    strong = _term_hits(rel.get("strong"), t, cs)
    if strong:
        return True, f"strong: {strong[0]}"
    if mode == "strong":
        return False, "no strong keyword"
    core = _term_hits(rel.get("core"), t, cs)
    ctx = _term_hits(rel.get("context"), t, cs)
    if core and ctx:
        return True, f"core: {core[0]} + context: {ctx[0]}"
    return False, "no keywords"


def classify_doc(item: dict, rules: dict) -> tuple[str, str]:
    """First matching rule in rules.yaml doc_types wins."""
    t = text_of(item)
    for r in rules["doc_types"]:
        if "when_source_id" in r and item.get("source_id") != r["when_source_id"]:
            continue
        if "when_source_type" in r and item.get("source_type") != r["when_source_type"]:
            continue
        if "when_source_role" in r and item.get("source_role") != r["when_source_role"]:
            continue
        if "patterns" in r and not any_match(r["patterns"], t):
            continue
        return r["type"], r["status"]
    return "news", "media report"


def extract_keys(item: dict, rules: dict) -> list[str]:
    """Strong event keys (e.g. arxiv:2410.01234) from title, excerpt and URL.
    Items that share a key are the same event regardless of wording."""
    t = f"{text_of(item)} {item.get('url', '')}"
    keys = set()
    for k in rules.get("event_keys") or []:
        for m in re.finditer(k["pattern"], t, re.I):
            keys.add(f"{k['prefix']}:{(m.group(1) if m.groups() else m.group(0)).lower()}")
    return sorted(keys)

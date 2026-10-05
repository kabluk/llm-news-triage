"""Explainable 100-point event score.

This is an editorial priority computed by transparent rules from rules.yaml,
not a probability that a story is true and not a traffic forecast."""
from __future__ import annotations

import re
from datetime import datetime

from .util import any_match, parse_dt

# Criterion keys in display order. Labels and maxima come from rules.yaml.
CRITERIA = ["impact", "primary_source", "novelty", "reach", "freshness", "clarity"]
# Criteria the LLM may refine (subjective ones). The rest are always rule-based.
LLM_CRITERIA = ["impact", "clarity"]


def event_text(ev: dict) -> str:
    return " ".join(f"{i.get('title', '')}. {i.get('snippet', '')}" for i in ev["items"])


def _c(points, mx, reason, insufficient=False, via="rules"):
    return {"points": max(0, min(mx, int(round(points)))), "max": mx, "reason": reason,
            "insufficient_data": insufficient, "via": via}


def impact(ev, cfg):
    t = event_text(ev)
    hit, pts = [], 0
    for g in cfg["groups"]:
        if any(re.search(r"\b" + re.escape(term), t, re.I) for term in g["terms"]):
            hit.append(g["label"])
            pts += g["points"]
    if not hit:
        return _c(0, cfg["max"], "no match with any impact topic", insufficient=len(t) < 80)
    reason = "topics: " + "; ".join(hit)
    dw = cfg.get("downweight") or {}
    if any_match(dw.get("patterns"), t):
        pts = min(pts, cfg["max"]) * dw.get("factor", 1)
        reason += f"; low-signal format, weight x{dw.get('factor')}"
    return _c(pts, cfg["max"], reason)


def _outlets(ev) -> set:
    return {i.get("outlet") for i in ev["items"] if i.get("outlet")}


def reach(ev, cfg, impact_points=None):
    n = len(_outlets(ev))
    steps = sorted(cfg["by_outlets"].items(), key=lambda kv: int(kv[0]))
    pts = 0
    for k, v in steps:
        if n >= int(k):
            pts = v
    reason = f"{n} independent outlet(s)"
    if any_match(cfg["magnitude_patterns"], event_text(ev)):
        pts += cfg["magnitude_bonus"]
        reason += "; text states a magnitude (size, users, share)"
    thr = cfg.get("low_impact_threshold")
    if thr is not None and impact_points is not None and impact_points < thr and pts > cfg["low_impact_cap"]:
        pts = cfg["low_impact_cap"]
        reason += f"; impact < {thr}, reach capped at {pts}"
    return _c(pts, cfg["max"], reason, insufficient=(n == 0))


def novelty(ev, cfg):
    state = ev.get("novelty_state", "new_event")
    labels = {"new_event": "event first seen in this run",
              "new_primary_doc": "a primary source was added to a known event",
              "new_outlet_on_known_event": "new coverage of a known event",
              "nothing_new": "nothing new since the last issue"}
    pts = cfg[state]
    reason = labels[state]
    titles = " ".join(i.get("title", "") for i in ev["items"])
    if any_match(cfg["rehash_patterns"], titles) and len(ev["items"]) == 1:
        pts -= cfg["rehash_penalty"]
        reason += "; explainer/opinion format, not a new event"
    return _c(pts, cfg["max"], reason)


def primary_source(ev, cfg):
    items = ev["items"]
    roles = {i.get("source_role") for i in items}
    reporting = {i.get("outlet") for i in items if i.get("source_role") != "primary"
                 and not (i.get("extra") or {}).get("signal_only")}
    if "primary" in roles and reporting:
        return _c(cfg["primary_plus_coverage"], cfg["max"],
                  "primary source in input, plus independent coverage")
    if "primary" in roles:
        return _c(cfg["primary_source_item"], cfg["max"], "primary source in input (the publisher itself)")
    outlets = _outlets(ev)
    if len(outlets) >= 2:
        return _c(cfg["two_plus_independent_outlets"], cfg["max"],
                  f"independent outlets: {', '.join(sorted(outlets))}; primary source not found")
    if reporting:
        return _c(cfg["one_reporting_outlet"], cfg["max"], f"one outlet ({next(iter(reporting))}); primary source not found")
    return _c(cfg["signal_only"], cfg["max"], "aggregator signal only; original not opened", insufficient=True)


def freshness(ev, cfg, now: datetime):
    d = parse_dt(ev.get("event_date"))
    basis = "event date"
    if d is None:
        pubs = [parse_dt(i.get("published_at")) for i in ev["items"] if i.get("published_at")]
        d = min(pubs) if pubs else None
        basis = "first publication (event date not extracted)"
    if d is None:
        return _c(0, cfg["max"], "date unknown", insufficient=True)
    hours = (now - d).total_seconds() / 3600
    for limit, pts in cfg["steps_hours"]:
        if hours <= limit:
            return _c(pts, cfg["max"], f"{basis}: {hours:.0f} h ago")
    return _c(0, cfg["max"], f"{basis}: {hours / 24:.0f} days ago")


def clarity(ev, cfg, n_flags: int):
    dt = ev.get("doc_type") or "news"
    pts = cfg["by_doc_type"].get(dt, cfg["by_doc_type"].get("news", 5))
    reason = f"type: {ev.get('doc_status') or dt}"
    if len(ev["items"]) == 1 and dt == "news":
        pts -= cfg["single_source_complex_penalty"]
        reason += "; single report, no document"
    if n_flags:
        pts -= cfg["flag_penalty"]
        reason += f"; {n_flags} stop flag(s), wording needs caveats"
    return _c(pts, cfg["max"], reason)


def _llm_override(crit: dict, name: str, llm: dict | None):
    v = (llm or {}).get("scores", {}).get(name)
    if v and v.get("points") is not None and not v.get("insufficient_data"):
        crit[name] = _c(v["points"], crit[name]["max"], v.get("reason", ""), via="llm")


def score_event(ev: dict, rules: dict, now: datetime, n_flags: int = 0, llm: dict | None = None) -> dict:
    s = rules["scoring"]
    crit = {"impact": impact(ev, s["impact"])}
    _llm_override(crit, "impact", llm)
    crit["primary_source"] = primary_source(ev, s["primary_source"])
    crit["novelty"] = novelty(ev, s["novelty"])
    crit["reach"] = reach(ev, s["reach"], crit["impact"]["points"])
    crit["freshness"] = freshness(ev, s["freshness"], now)
    crit["clarity"] = clarity(ev, s["clarity"], n_flags)
    # Subjective criteria may be refined by the LLM unless it said "insufficient data".
    _llm_override(crit, "clarity", llm)
    crit = {k: crit[k] for k in CRITERIA}
    total = sum(c["points"] for c in crit.values())
    return {"total": total, "criteria": crit}


def recommendation(total: int, flags: list, rules: dict) -> dict:
    th = rules["thresholds"]
    if total >= th["feature"]:
        tier = "feature"
    elif total >= th["editor_decision"]:
        tier = "editor_decision"
    else:
        tier = "log"
    ready = tier == "feature" and not flags
    labels = {"feature": "feature in the digest", "editor_decision": "editor's call", "log": "log only"}
    label = labels[tier]
    if tier == "feature" and flags:
        label = "high score but stop flags present: show to editor, not ready"
    return {"tier": tier, "label": label, "ready": ready}

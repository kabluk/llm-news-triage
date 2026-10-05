"""Event selection and digest rendering (HTML + plain text)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .scoring import CRITERIA
from .util import parse_dt

TEMPLATES = Path(__file__).parent / "templates"


def select(events: list[dict], rules: dict) -> tuple[list[dict], list[dict]]:
    """Top events (score >= editor_decision, at least top_min, at most top_max) and the rest."""
    d = rules["digest"]
    th = rules["thresholds"]["editor_decision"]
    ranked = sorted(events, key=lambda e: (-e["score"]["total"], e["id"]))
    top = [e for e in ranked if e["score"]["total"] >= th][: d["top_max"]]
    if len(top) < d["top_min"]:
        for e in ranked:
            if len(top) >= d["top_min"]:
                break
            if e not in top:
                top.append(e)
    others = [e for e in ranked if e not in top][: d["others_max"]]
    return top, others


def unknowns(ev: dict) -> list[str]:
    if ev.get("llm") and ev["llm"].get("unknown"):
        return ev["llm"]["unknown"]
    out = []
    if not (ev.get("primary") or {}).get("url"):
        out.append("No primary source found: reports are not checked against the publisher's own page.")
    if ev.get("doc_type") == "research_paper":
        out.append("Preprint: results are not independently reproduced or peer-reviewed.")
    if len({i.get("outlet") for i in ev["items"]}) == 1 and len(ev["items"]) >= 1:
        out.append("Only one outlet so far: no independent confirmation.")
    if all((i.get("extra") or {}).get("signal_only") for i in ev["items"]):
        out.append("Original article not opened (aggregator link); author and text unchecked.")
    return out or ["No gaps found by the rules; check manually before publishing."]


def why(ev: dict) -> str:
    if ev.get("llm") and ev["llm"].get("why_it_matters"):
        return ev["llm"]["why_it_matters"]
    c = ev["score"]["criteria"]
    return f"{c['impact']['reason']}. {c['reach']['reason']}."


def next_action(ev: dict, rules: dict) -> str:
    if ev.get("llm") and ev["llm"].get("editor_next_action"):
        return ev["llm"]["editor_next_action"]
    na = rules["editorial"]["next_action"]
    steps = []
    if ev["flags"]:
        steps.append(na["flags"])
    if all((i.get("extra") or {}).get("signal_only") for i in ev["items"]):
        steps.append(na["signal_only"])
    elif not (ev.get("primary") or {}).get("url"):
        steps.append(na["no_primary"])
    if ev.get("doc_type") == "research_paper":
        steps.append(na["preprint"])
    if not steps:
        steps.append(na["ready"])
    return " ".join(steps)


def angle(ev: dict, rules: dict) -> str:
    a = rules["editorial"]["angle"]
    return a.get(ev.get("doc_type"), a["news"])


def headline(ev: dict) -> str:
    if ev.get("llm") and ev["llm"].get("headline"):
        return ev["llm"]["headline"]
    return f"{ev['doc_status'][:1].upper()}{ev['doc_status'][1:]}: {ev['title']}"


def risk(ev: dict) -> list[str]:
    out = [f"STOP: {f['label']} ({f['reason']})" for f in ev["flags"]]
    out += ev.get("cautions", [])
    if ev.get("llm") and ev["llm"].get("misstatement_risk"):
        out.append(ev["llm"]["misstatement_risk"])
    return out or ["No stop flags."]


def _fmt(dt_s, tz) -> str:
    d = parse_dt(dt_s)
    return d.astimezone(tz).strftime("%Y-%m-%d") if d else "unknown"


def source_health(db, cfg) -> list[dict]:
    rows = []
    for s in cfg.sources:
        r = db.one("SELECT * FROM sources WHERE id=?", s["id"])
        rows.append({"id": s["id"], "name": s["name"], "url": s["url"], "method": s.get("method"),
                     "enabled": bool(s.get("enabled")), "role": s.get("role"),
                     "last_status": r["last_status"] if r else None,
                     "last_checked_at": r["last_checked_at"] if r else None,
                     "last_ok_at": r["last_ok_at"] if r else None,
                     "last_error": r["last_error"] if r else None,
                     "failures": r["consecutive_failures"] if r else 0,
                     "items": r["items_last_run"] if r else 0,
                     "note": (s.get("status_note") or "").strip()})
    return rows


def view(ev: dict, rules: dict, tz) -> dict:
    prim = ev.get("primary") or {}
    pubs = sorted(i["published_at"] for i in ev["items"] if i.get("published_at"))
    sc = rules["scoring"]
    return {
        "id": ev["id"], "doc_type": ev.get("doc_type"), "headline": headline(ev), "title": ev["title"],
        "total": ev["score"]["total"],
        "criteria": [{"label": sc[k].get("label", k), **ev["score"]["criteria"][k]} for k in CRITERIA],
        "rec": ev["rec"], "why": why(ev), "claims": ev.get("claims", [])[:6], "unknowns": unknowns(ev),
        "event_date": _fmt(ev.get("event_date"), tz) if ev.get("event_date") else "not extracted",
        "pub_date": _fmt(pubs[0], tz) if pubs else "unknown",
        "status": ev.get("doc_status"), "risk": risk(ev), "flags": ev["flags"],
        "pubs": [{"outlet": i.get("outlet"), "title": i["title"], "url": i["url"],
                  "signal": bool((i.get("extra") or {}).get("signal_only")),
                  "date": _fmt(i.get("published_at"), tz)} for i in ev["items"][:8]],
        "n_items": len(ev["items"]),
        "primary_url": prim.get("url"), "verification": prim.get("verification"),
        "angle": angle(ev, rules), "next_action": next_action(ev, rules),
        "llm": bool(ev.get("llm")), "dropped_claims": (ev.get("llm") or {}).get("dropped_claims", 0),
    }


def render(issue_key, now: datetime, top, others, health, errors, cfg, stats, mode, llm_note=None):
    rules = cfg.rules
    tz = ZoneInfo(rules["schedule"]["timezone"])
    local = now.astimezone(tz)
    part = "morning" if local.hour < 14 else "evening"
    top_v = [view(e, rules, tz) for e in top]
    oth_v = [view(e, rules, tz) for e in others]
    broken = [h for h in health if h["enabled"] and h["last_status"] not in ("ok", None)]
    not_connected = [h for h in health if not h["enabled"]]
    subject = (f"{rules['digest']['subject_prefix']} {local:%Y-%m-%d} {part}: "
               f"{len(top_v)} top, {len(oth_v)} logged" + (" [DRY RUN]" if mode != "send" else ""))
    ctx = dict(subject=subject, issue_key=issue_key, local=local.strftime("%Y-%m-%d %H:%M %Z"), top=top_v,
               others=oth_v, health=health, broken=broken, not_connected=not_connected, errors=errors,
               stats=stats, thresholds=rules["thresholds"], mode=mode, topic=rules.get("topic", ""),
               llm_note=llm_note)
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    html = env.get_template("digest.html.j2").render(**ctx)
    text = env.get_template("digest.txt.j2").render(**ctx)
    return subject, html, text

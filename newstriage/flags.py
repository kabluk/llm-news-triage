"""Stop flags. Computed independently of the score; any flag blocks the
"ready" mark, whatever the total."""
from __future__ import annotations

import re
from collections import defaultdict

from .util import any_match, parse_dt

FLAG_LABELS = {
    "single_unverified_source": "strong claim from a single unverified source",
    "no_primary_source": "announcement reported without the primary source",
    "speculation_as_fact": "rumor or leak presented without confirmation",
    "pii_risk": "possible personal data",
    "date_or_number_mismatch": "material mismatch in dates or numbers",
}


def _flag(code, reason, evidence=None):
    return {"code": code, "label": FLAG_LABELS[code], "reason": reason, "evidence": evidence or []}


def _texts(ev):
    return [(i.get("url"), f"{i.get('title', '')}. {i.get('snippet', '')}") for i in ev["items"]]


def _num_unit_re(units):
    alt = "|".join(re.escape(u) for u in sorted(units, key=len, reverse=True))
    return re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(" + alt + r")(?=\W|$)", re.I)


def _numbers_by_unit(text, rx, aliases):
    out = defaultdict(set)
    for m in rx.finditer(text):
        num = float(m.group(1).replace(",", ""))
        unit = m.group(2).lower()
        out[aliases.get(unit, unit)].add(num)
    return out


def compute_flags(ev: dict, rules: dict) -> tuple[list[dict], list[str]]:
    """Returns (stop flags, soft cautions)."""
    f = rules["flags"]
    flags, cautions = [], []
    texts = _texts(ev)
    alltext = " ".join(t for _, t in texts)
    roles = {i.get("source_role") for i in ev["items"]}
    outlets = {i.get("outlet") for i in ev["items"]}
    has_primary = "primary" in roles

    # 1. Strong claim, single non-primary source
    strong = any_match(f["strong_claim_terms"], alltext)
    if strong and not has_primary and len(outlets) < 2:
        flags.append(_flag("single_unverified_source",
                           "strong wording (" + ", ".join(p.replace("\\b", "") for p in strong[:3]) +
                           ") from one outlet and no primary source", [u for u, _ in texts]))

    # 2. An announcement/release reported only second-hand
    if not has_primary and any_match(f["primary_expected_patterns"], alltext):
        flags.append(_flag("no_primary_source",
                           "reads as an official release or announcement, but only secondary reports are in the input; "
                           "find the publisher's own page", [u for u, _ in texts]))

    # 3. Rumor or leak without confirmation
    spec = any_match(f["speculation_patterns"], alltext)
    if spec and not has_primary:
        flags.append(_flag("speculation_as_fact",
                           "speculative wording (" + ", ".join(p.replace("\\b", "") for p in spec[:3]) +
                           ") with no confirming primary source",
                           [u for u, t in texts if any_match(f["speculation_patterns"], t)]))

    # 4. Personal data
    pii = [(u, p) for u, t in texts for p in f["pii_patterns"] if re.search(p, t)]
    if pii:
        flags.append(_flag("pii_risk", "identifiers (phone, email, street address, date of birth) in the text; "
                                       "do not republish without need and consent", sorted({u for u, _ in pii})))

    # 5. Numbers and dates disagree across reports
    rx = _num_unit_re(f["number_units"])
    aliases = {k.lower(): v for k, v in (f.get("unit_aliases") or {}).items()}
    units = defaultdict(list)
    for u, t in texts:
        for unit, nums in _numbers_by_unit(t, rx, aliases).items():
            units[unit].append((u, max(nums)))
    for unit, vals in units.items():
        nums = [v for _, v in vals]
        if len({u for u, _ in vals}) >= 2 and max(nums) > 0 and (max(nums) - min(nums)) / max(nums) > f["number_mismatch_ratio"]:
            flags.append(_flag("date_or_number_mismatch",
                               f"different '{unit}' figures across reports: {', '.join(f'{n:g}' for n in sorted(set(nums)))}",
                               [u for u, _ in vals]))
            break
    edates = [parse_dt(i.get("event_date")) for i in ev["items"] if i.get("event_date")]
    edates = [d for d in edates if d]
    if len(edates) >= 2 and (max(edates) - min(edates)).days > f["date_mismatch_days"]:
        flags.append(_flag("date_or_number_mismatch",
                           f"event dates disagree across sources: {min(edates).date()} … {max(edates).date()}"))

    if ev.get("doc_type") == "research_paper":
        cautions.append("Preprint: claims are the authors' own and not peer-reviewed unless stated.")
    return flags, cautions

"""Issue slots (e.g. 08:00 and 17:00) in the configured timezone, DST-aware via
zoneinfo. The issue key is local date + slot, so two cron firings for the same
slot (for example UTC schedules covering both DST offsets) yield the same key
and therefore one email."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _slots(rules):
    tz = ZoneInfo(rules["schedule"]["timezone"])
    slots = [tuple(int(x) for x in s.split(":")) for s in rules["schedule"]["slots"]]
    return tz, sorted(slots)


def slot_key(local_dt: datetime, hm: tuple[int, int]) -> str:
    return f"{local_dt:%Y-%m-%d}-{hm[0]:02d}{hm[1]:02d}"


def due_slot(now_utc: datetime, rules: dict) -> tuple[str | None, str]:
    """Which slot is due right now. Returns (issue_key | None, explanation)."""
    tz, slots = _slots(rules)
    grace = timedelta(minutes=rules["schedule"].get("grace_minutes", 180))
    local = now_utc.astimezone(tz)
    best = None
    for day in (local.date() - timedelta(days=1), local.date()):
        for hm in slots:
            start = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=tz)
            if start <= local < start + grace:
                best = (start, hm)
    if best is None:
        return None, (f"now {local:%Y-%m-%d %H:%M %Z}: no slot due "
                      f"(slots {rules['schedule']['slots']}, grace {grace})")
    start, hm = best
    return slot_key(start, hm), f"slot {start:%Y-%m-%d %H:%M %Z}"


def next_slot(now_utc: datetime, rules: dict) -> datetime:
    tz, slots = _slots(rules)
    local = now_utc.astimezone(tz)
    for add in range(0, 3):
        day = local.date() + timedelta(days=add)
        for hm in slots:
            start = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=tz)
            if start > local:
                return start
    raise RuntimeError("unreachable")

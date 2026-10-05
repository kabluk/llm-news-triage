from datetime import datetime, timezone

from newstriage import mailer
from newstriage.schedule import due_slot, next_slot

ENV = {"NEWSTRIAGE_SMTP_HOST": "h", "NEWSTRIAGE_SMTP_PORT": "587", "NEWSTRIAGE_SMTP_USER": "u",
       "NEWSTRIAGE_SMTP_PASSWORD": "p", "NEWSTRIAGE_MAIL_FROM": "bot@example.org",
       "NEWSTRIAGE_MAIL_TO": "a@example.org, b@example.net"}


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_shipped_schedule_is_new_york_twice_daily(rules):
    assert rules["schedule"]["timezone"] == "America/New_York"
    assert rules["schedule"]["slots"] == ["08:00", "17:00"]


def test_summer_edt(rules):
    # 2026-07-01: EDT = UTC-4 -> 08:00 = 12:00 UTC, 17:00 = 21:00 UTC
    assert due_slot(utc(2026, 7, 1, 12, 5), rules)[0] == "2026-07-01-0800"
    assert due_slot(utc(2026, 7, 1, 21, 10), rules)[0] == "2026-07-01-1700"


def test_winter_est(rules):
    # 2026-12-01: EST = UTC-5 -> 08:00 = 13:00 UTC
    assert due_slot(utc(2026, 12, 1, 12, 5), rules)[0] is None          # 07:05 local: too early
    assert due_slot(utc(2026, 12, 1, 13, 5), rules)[0] == "2026-12-01-0800"
    assert due_slot(utc(2026, 12, 2, 0, 0), rules)[0] == "2026-12-01-1700"  # 19:00 local, previous day's slot


def test_both_utc_crons_map_to_same_issue(rules):
    # CI cron at 12:00 and 13:00 UTC to cover both offsets; in summer both land in one slot -> one key
    a = due_slot(utc(2026, 7, 1, 12, 0), rules)[0]
    b = due_slot(utc(2026, 7, 1, 13, 0), rules)[0]
    assert a == b == "2026-07-01-0800"


def test_dst_transition_days(rules):
    # US DST 2026: starts 2026-03-08, ends 2026-11-01
    assert due_slot(utc(2026, 3, 8, 12, 1), rules)[0] == "2026-03-08-0800"
    assert due_slot(utc(2026, 11, 1, 13, 1), rules)[0] == "2026-11-01-0800"


def test_outside_grace(rules):
    assert due_slot(utc(2026, 7, 1, 17, 0), rules)[0] is None   # 13:00 local


def test_next_slot(rules):
    n = next_slot(utc(2026, 9, 29, 16, 0), rules)                # 12:00 EDT
    assert (n.hour, n.day) == (17, 29)
    n = next_slot(utc(2026, 9, 29, 23, 0), rules)                # 19:00 EDT
    assert (n.hour, n.day) == (8, 30)


def test_mail_check_lists_missing_settings():
    assert set(mailer.missing_settings({})) == set(mailer.REQUIRED)
    assert mailer.missing_settings(ENV) == {}


def test_message_has_text_html_and_issue_header():
    msg = mailer.build_message("Subject", "<p>html</p>", "text", "2026-09-29-0800", ENV)
    assert msg["To"] == "a@example.org, b@example.net" and msg["X-Newstriage-Issue"] == "2026-09-29-0800"
    assert msg.get_body(("plain",)).get_content().strip() == "text"
    assert "<p>html</p>" in msg.get_body(("html",)).get_content()


def test_send_reports_refused_recipients(monkeypatch):
    monkeypatch.setattr(mailer, "_deliver", lambda msg, to, env: {a: (550, b"no such user") for a in to if a == "b@example.net"})
    info = mailer.send("s", "<p>h</p>", "t", "k", ENV)
    assert info["recipients"] == ["a@example.org"] and "550 no such user" in info["refused"]["b@example.net"]
    res = mailer.send_test(ENV)
    assert [r["ok"] for r in res] == [True, False]

"""Command line.

  python -m newstriage run --dry-run        build a local issue, do not send
  python -m newstriage run --send           build and email it (manual run)
  python -m newstriage scheduled            for cron: send only when a slot is due
  python -m newstriage daemon               wait for the next slot itself (no cron)
  python -m newstriage sources [--probe]    source registry and health; --probe fetches each live
  python -m newstriage errors [--limit N]   error log
  python -m newstriage mail-check           which mail env vars are missing
  python -m newstriage mail-test            short test email to each recipient
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import timezone

from .config import load_config
from .db import DB
from .fetchers import FETCHERS
from .http import FetchError
from .mailer import missing_settings
from .pipeline import make_http, run_pipeline
from .schedule import due_slot, next_slot
from .util import utcnow


def _print_result(res):
    out = {k: v for k, v in res.items() if k not in ("errors", "top", "others")}
    if "top" in res:
        out.update(top_events=len(res["top"]), logged_events=len(res["others"]))
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_run(cfg, a):
    res = run_pipeline(cfg, mode="send" if a.send else "dry_run", issue_key=a.issue_key, force=a.force,
                       only_sources=a.sources.split(",") if a.sources else None)
    _print_result(res)
    ok = ("dry_run", "sent", "skipped_empty", "skipped_duplicate")
    return 0 if res.get("status") in ok or res.get("skipped") else 1


def cmd_scheduled(cfg, a):
    key, why = due_slot(utcnow(), cfg.rules)
    print(why)
    if not key:
        return 0
    res = run_pipeline(cfg, mode="dry_run" if a.dry_run else "send", issue_key=key)
    _print_result(res)
    return 0 if res.get("status") != "failed" else 1


def cmd_daemon(cfg, a):
    while True:
        nxt = next_slot(utcnow(), cfg.rules)
        wait = (nxt.astimezone(timezone.utc) - utcnow()).total_seconds()
        print(f"next issue: {nxt:%Y-%m-%d %H:%M %Z} (in {wait / 3600:.1f} h)", flush=True)
        time.sleep(max(1, wait + 5))
        cmd_scheduled(cfg, a)


def cmd_sources(cfg, a):
    db = DB(cfg.db_path)
    db.sync_sources(cfg.sources)
    http = make_http(cfg) if a.probe else None
    for s in cfg.sources:
        line = f"{'ON ' if s.get('enabled') else 'off'} {s['id']:<22} {s.get('method', ''):<6} {s['url']}"
        if a.probe and s.get("enabled") and s.get("method") in FETCHERS:
            try:
                n = len(FETCHERS[s["method"]](s, http, utcnow()))
                db.source_result(s["id"], True, "ok", None, n)
                line += f"  -> ok, {n} entries"
            except FetchError as e:
                db.source_result(s["id"], False, e.kind, str(e), 0)
                line += f"  -> {e.kind}: {e}"
        r = db.one("SELECT * FROM sources WHERE id=?", s["id"])
        print(line)
        if r and r["last_checked_at"]:
            print(f"      last check {r['last_checked_at']}: {r['last_status']}"
                  f"{' - ' + r['last_error'] if r['last_error'] else ''}; consecutive failures {r['consecutive_failures']}")
        if not s.get("enabled") and s.get("status_note"):
            print("      " + " ".join(s["status_note"].split()))
    db.close()
    return 0


def cmd_errors(cfg, a):
    db = DB(cfg.db_path)
    for r in db.q("SELECT * FROM errors ORDER BY id DESC LIMIT ?", a.limit):
        print(f"{r['at']} run={r['run_id']} {r['source_id']} {r['kind']}: {r['message'][:300]}")
    db.close()
    return 0


def cmd_mail_check(cfg, a):
    miss = missing_settings(cfg.env)
    if not miss:
        print("All mail settings are set.")
        return 0
    print("Not set (put them in .env or your environment's secrets):")
    for k, v in miss.items():
        print(f"  {k:<26} {v}")
    return 1


def cmd_mail_test(cfg, a):
    from .mailer import send_test
    res = send_test(cfg.env)
    for r in res:
        print(f"  {'ok' if r['ok'] else 'x '} {r['to']}: {r['detail']}")
    return 0 if all(r["ok"] for r in res) else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="newstriage", description="Twice-daily news triage with a guarded LLM")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="manual run")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="do not send email")
    g.add_argument("--send", action="store_true", help="send the email")
    r.add_argument("--issue-key", help="issue key (default manual-<timestamp>)")
    r.add_argument("--force", action="store_true", help="allow re-sending the same issue")
    r.add_argument("--sources", help="only these source ids, comma-separated")
    s = sub.add_parser("scheduled", help="run from cron; sends only in a due slot")
    s.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("daemon", help="wait for slots and run")
    d.add_argument("--dry-run", action="store_true")
    so = sub.add_parser("sources", help="source registry and health")
    so.add_argument("--probe", action="store_true", help="live-fetch every enabled source")
    e = sub.add_parser("errors", help="error log")
    e.add_argument("--limit", type=int, default=30)
    sub.add_parser("mail-check", help="list missing mail env vars")
    sub.add_parser("mail-test", help="send a test email to each recipient")
    a = p.parse_args(argv)
    cfg = load_config()
    return {"run": cmd_run, "scheduled": cmd_scheduled, "daemon": cmd_daemon, "sources": cmd_sources,
            "errors": cmd_errors, "mail-check": cmd_mail_check, "mail-test": cmd_mail_test}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())

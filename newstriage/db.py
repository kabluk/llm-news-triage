"""SQLite store. Full text of third-party articles is never stored: only
metadata, a short excerpt (<=300 chars) and extracted, attributed claims.
The `issues` table keys every digest by date+slot, which is what makes
repeated sends of the same issue a no-op."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .util import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  name TEXT, url TEXT, type TEXT, role TEXT, method TEXT,
  enabled INTEGER, frequency TEXT, status_note TEXT,
  last_checked_at TEXT, last_ok_at TEXT, last_status TEXT,
  last_error TEXT, consecutive_failures INTEGER DEFAULT 0,
  items_last_run INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  url_canonical TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  outlet TEXT, author TEXT,
  published_at TEXT, event_date TEXT, discovered_at TEXT NOT NULL,
  last_seen_at TEXT,
  source_id TEXT NOT NULL, source_role TEXT, source_type TEXT,
  doc_type TEXT, doc_status TEXT,
  snippet TEXT,
  keys_json TEXT, extra_json TEXT,
  verification_status TEXT DEFAULT 'unverified',
  event_id INTEGER REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS items_event ON items(event_id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  event_date TEXT, doc_type TEXT, doc_status TEXT,
  keys_json TEXT,
  primary_url TEXT, primary_json TEXT,
  score_total INTEGER, score_json TEXT, flags_json TEXT,
  recommendation TEXT, llm_json TEXT,
  status TEXT DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id),
  item_id INTEGER REFERENCES items(id),
  text TEXT NOT NULL, source_url TEXT NOT NULL,
  kind TEXT, via TEXT, created_at TEXT,
  UNIQUE(event_id, text, source_url)
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity TEXT, entity_id INTEGER, at TEXT, action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, finished_at TEXT, mode TEXT, issue_key TEXT,
  stats_json TEXT
);
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, source_id TEXT, at TEXT, kind TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS issues (
  issue_key TEXT PRIMARY KEY,
  created_at TEXT, status TEXT, sent_at TEXT,
  subject TEXT, html_path TEXT, text_path TEXT, recipients TEXT,
  content_hash TEXT, event_ids_json TEXT, run_id INTEGER
);
CREATE TABLE IF NOT EXISTS event_issue (
  event_id INTEGER, issue_key TEXT, role TEXT,
  PRIMARY KEY(event_id, issue_key)
);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def ex(self, sql, *args):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def log(self, entity: str, entity_id: int, action: str, detail=None):
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)
        self.ex("INSERT INTO history(entity, entity_id, at, action, detail) VALUES (?,?,?,?,?)",
                entity, entity_id, iso(utcnow()), action, detail)

    def error(self, run_id, source_id, kind, message):
        self.ex("INSERT INTO errors(run_id, source_id, at, kind, message) VALUES (?,?,?,?,?)",
                run_id, source_id, iso(utcnow()), kind, str(message)[:1000])

    def sync_sources(self, sources: list[dict]):
        for s in sources:
            self.ex(
                """INSERT INTO sources(id, name, url, type, role, method, enabled, frequency, status_note)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url,
                     type=excluded.type, role=excluded.role, method=excluded.method,
                     enabled=excluded.enabled, frequency=excluded.frequency,
                     status_note=excluded.status_note""",
                s["id"], s["name"], s["url"], s.get("type"), s.get("role"), s.get("method"),
                1 if s.get("enabled") else 0,
                "every run" if not s.get("min_interval_hours") else f"every {s['min_interval_hours']} h",
                (s.get("status_note") or "").strip())

    def source_result(self, sid: str, ok: bool, status: str, error: str | None, n: int):
        now = iso(utcnow())
        if ok:
            self.ex("""UPDATE sources SET last_checked_at=?, last_ok_at=?, last_status=?, last_error=NULL,
                       consecutive_failures=0, items_last_run=? WHERE id=?""", now, now, status, n, sid)
        else:
            self.ex("""UPDATE sources SET last_checked_at=?, last_status=?, last_error=?,
                       consecutive_failures=consecutive_failures+1, items_last_run=0 WHERE id=?""",
                    now, status, (error or "")[:500], sid)

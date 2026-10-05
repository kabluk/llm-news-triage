"""One run: fetch -> normalize -> relevance filter -> dedup -> events ->
stop flags -> score -> optional LLM enrichment -> digest -> (send)."""
from __future__ import annotations

import hashlib
import json
import traceback
from datetime import datetime, timedelta

from . import digest as digest_mod
from . import llm as llm_mod
from . import mailer
from .classify import classify_doc, extract_keys, is_relevant
from .cluster import EventRef, _numbers, dedupe, match_event
from .config import Config
from .db import DB
from .fetchers import FETCHERS
from .flags import compute_flags
from .http import FetchError, Http
from .scoring import recommendation, score_event
from .util import iso, parse_dt, tokens, utcnow

DOC_PRIORITY = ["release_notes", "announcement", "research_paper", "blog_post", "news"]


def make_http(cfg: Config) -> Http:
    h = cfg.rules["http"]
    ua = h["user_agent"].format(contact=cfg.env.get("NEWSTRIAGE_CONTACT_EMAIL") or "n/a")
    return Http(ua, timeout=h["timeout"], retries=h["retries"], default_delay=h["default_crawl_delay"])


def _row_to_item(r) -> dict:
    d = dict(r)
    d["extra"] = json.loads(d.pop("extra_json") or "{}")
    d["keys"] = json.loads(d.pop("keys_json") or "[]")
    return d


class Run:
    def __init__(self, cfg: Config, db: DB, http: Http, now: datetime | None = None, log=print):
        self.cfg, self.db, self.http = cfg, db, http
        self.rules = cfg.rules
        self.now = now or utcnow()
        self.log = log
        self.stats = {"fetched": 0, "relevant": 0, "new_items": 0, "duplicates": 0,
                      "new_events": 0, "updated_events": 0, "source_errors": 0}
        self.run_id = None
        self.touched: dict[int, str] = {}   # event_id -> novelty_state

    # --- 1. fetch -------------------------------------------------------------
    def collect(self, only: list[str] | None = None) -> list[dict]:
        items = []
        for src in self.cfg.sources:
            if only and src["id"] not in only:
                continue
            if not src.get("enabled"):
                continue
            method = FETCHERS.get(src.get("method"))
            if method is None:
                continue
            iv = src.get("min_interval_hours") or 0
            if iv:
                row = self.db.one("SELECT last_ok_at FROM sources WHERE id=?", src["id"])
                last = parse_dt(row["last_ok_at"]) if row and row["last_ok_at"] else None
                if last and self.now - last < timedelta(hours=iv):
                    continue
            try:
                got = method(src, self.http, self.now)
            except FetchError as e:
                self.stats["source_errors"] += 1
                self.db.error(self.run_id, src["id"], e.kind, str(e))
                self.db.source_result(src["id"], False, e.kind, str(e), 0)
                self.log(f"  x {src['id']}: {e.kind}: {e}")
                continue
            except Exception as e:  # noqa: BLE001 - one broken source must not stop the run
                self.stats["source_errors"] += 1
                msg = f"{type(e).__name__}: {e}"
                self.db.error(self.run_id, src["id"], "exception", msg + "\n" + traceback.format_exc()[-800:])
                self.db.source_result(src["id"], False, "exception", msg, 0)
                self.log(f"  x {src['id']}: {msg}")
                continue
            kept = []
            for it in got:
                if not it.get("url") or not it.get("title"):
                    continue
                it["published_at"] = iso(parse_dt(it.get("published_at")))
                it["event_date"] = iso(parse_dt(it.get("event_date")))
                ok, why = is_relevant(it, self.rules, src.get("relevance", "keywords"))
                if not ok:
                    continue
                it["relevance"] = why
                it["doc_type"], it["doc_status"] = classify_doc(it, self.rules)
                it["keys"] = extract_keys(it, self.rules)
                kept.append(it)
            self.stats["fetched"] += len(got)
            self.stats["relevant"] += len(kept)
            self.db.source_result(src["id"], True, "ok", None, len(kept))
            self.log(f"  ok {src['id']}: {len(got)} fetched, {len(kept)} relevant")
            items.extend(kept)
        return items

    # --- 2. dedup and events -------------------------------------------------
    def ingest(self, items: list[dict]):
        known = {r["url_canonical"] for r in self.db.q("SELECT url_canonical FROM items")}
        new, dup = dedupe(items, known)
        self.stats["new_items"] += len(new)
        self.stats["duplicates"] += len(dup)
        for d in dup:
            self.db.ex("UPDATE items SET last_seen_at=? WHERE url_canonical=?", iso(self.now), d["url_canonical"])

        since = iso(self.now - timedelta(days=self.rules["clustering"]["window_days"]))
        refs: dict[int, EventRef] = {}
        for ev in self.db.q("SELECT id, keys_json, updated_at FROM events WHERE updated_at >= ?", since):
            ref = EventRef(ev["id"], set(json.loads(ev["keys_json"] or "[]")), [], parse_dt(ev["updated_at"]))
            for r in self.db.q("SELECT title FROM items WHERE event_id=?", ev["id"]):
                ref.title_tokens.append(tokens(r["title"]))
                ref.title_numbers.append(_numbers(r["title"]))
            refs[ev["id"]] = ref

        for it in sorted(new, key=lambda x: x.get("published_at") or ""):
            eid, why = match_event(it, list(refs.values()), self.rules, self.now)
            if eid is None:
                cur = self.db.ex("INSERT INTO events(title, created_at, updated_at, keys_json, status) VALUES (?,?,?,?,?)",
                                 it["title"], iso(self.now), iso(self.now), json.dumps(it["keys"]), "new")
                eid = cur.lastrowid
                refs[eid] = EventRef(eid, set(it["keys"]), [], self.now)
                self.touched[eid] = "new_event"
                self.stats["new_events"] += 1
                self.db.log("event", eid, "created", {"from_item": it["url"]})
            else:
                prev = self.touched.get(eid)
                if prev != "new_event":
                    state = "new_primary_doc" if it.get("source_role") == "primary" else "new_outlet_on_known_event"
                    if prev != "new_primary_doc":
                        self.touched[eid] = state
                    if prev is None:
                        self.stats["updated_events"] += 1
            ref = refs[eid]
            ref.keys |= set(it["keys"])
            ref.title_tokens.append(tokens(it["title"]))
            ref.title_numbers.append(_numbers(it["title"]))
            ref.last_date = self.now
            cur = self.db.ex(
                """INSERT INTO items(url, url_canonical, title, outlet, author, published_at, event_date,
                   discovered_at, last_seen_at, source_id, source_role, source_type, doc_type, doc_status,
                   snippet, keys_json, extra_json, event_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                it["url"], it["url_canonical"], it["title"], it.get("outlet"), it.get("author"),
                it.get("published_at"), it.get("event_date"), iso(self.now), iso(self.now), it["source_id"],
                it.get("source_role"), it.get("source_type"), it["doc_type"], it["doc_status"],
                it.get("snippet"), json.dumps(it["keys"]),
                json.dumps(it.get("extra") or {}, ensure_ascii=False, default=str), eid)
            self.db.log("item", cur.lastrowid, "discovered", {"source": it["source_id"], "event": eid, "match": why,
                                                              "relevance": it.get("relevance")})

    # --- 3. evaluate an event ------------------------------------------------
    def load_event(self, eid: int) -> dict:
        ev = dict(self.db.one("SELECT * FROM events WHERE id=?", eid))
        ev["items"] = [_row_to_item(r) for r in self.db.q("SELECT * FROM items WHERE event_id=? ORDER BY published_at", eid)]
        ev["keys"] = sorted(set(json.loads(ev.get("keys_json") or "[]")) | {k for i in ev["items"] for k in i["keys"]})
        return ev

    def evaluate(self, eid: int, novelty_state: str, llm_result: dict | None = None) -> dict:
        ev = self.load_event(eid)
        items = ev["items"]
        prim_items = [i for i in items if i.get("source_role") == "primary"]
        pool = prim_items or items
        ev["doc_type"] = min((i["doc_type"] for i in pool),
                             key=lambda d: DOC_PRIORITY.index(d) if d in DOC_PRIORITY else 99)
        ev["doc_status"] = next(i["doc_status"] for i in pool if i["doc_type"] == ev["doc_type"])
        ev["title"] = (prim_items[0] if prim_items else items[0])["title"]
        edates = [i["event_date"] for i in pool if i.get("event_date")]
        ev["event_date"] = min(edates) if edates else None
        ev["primary"] = ({"url": prim_items[0]["url"], "verification": "primary source present in input"}
                         if prim_items else {"url": None, "verification": "no primary source found"})
        ev["novelty_state"] = novelty_state
        flags, cautions = compute_flags(ev, self.rules)
        sc = score_event(ev, self.rules, self.now, n_flags=len(flags), llm=llm_result)
        rec = recommendation(sc["total"], flags, self.rules)
        ev.update(flags=flags, cautions=cautions, score=sc, rec=rec, llm=llm_result)
        self._save_claims(ev)
        self.db.ex(
            """UPDATE events SET title=?, updated_at=?, event_date=?, doc_type=?, doc_status=?, keys_json=?,
               primary_url=?, primary_json=?, score_total=?, score_json=?, flags_json=?, recommendation=?,
               llm_json=? WHERE id=?""",
            ev["title"], iso(self.now), ev["event_date"], ev["doc_type"], ev["doc_status"], json.dumps(ev["keys"]),
            ev["primary"].get("url"), json.dumps(ev["primary"]), sc["total"], json.dumps(sc),
            json.dumps({"flags": flags, "cautions": cautions}), json.dumps(rec),
            json.dumps(llm_result, ensure_ascii=False) if llm_result else None, eid)
        for i in items:
            self.db.ex("UPDATE items SET verification_status=? WHERE id=?", ev["primary"]["verification"], i["id"])
        self.db.log("event", eid, "scored", {"total": sc["total"], "flags": [f["code"] for f in flags],
                                             "novelty": novelty_state, "llm": bool(llm_result)})
        return ev

    def _save_claims(self, ev):
        """'Established' means attributed statements with a link, nothing else."""
        claims = []
        for i in ev["items"]:
            if i.get("source_role") == "primary":
                claims.append(("primary", f"{i.get('outlet')}: \"{i['title']}\"", i["url"], i["id"]))
            else:
                claims.append(("attributed", f"{i.get('outlet')} reports: \"{i['title']}\"", i["url"], i["id"]))
        if ev.get("llm"):
            for c in ev["llm"].get("established", []):
                claims.append(("llm_attributed", c["text"], c["source_url"], None))
        for kind, text, url, item_id in claims:
            self.db.ex("""INSERT OR IGNORE INTO claims(event_id, item_id, text, source_url, kind, via, created_at)
                          VALUES (?,?,?,?,?,?,?)""", ev["id"], item_id, text, url, kind,
                       "llm" if kind == "llm_attributed" else "rules", iso(self.now))
        ev["claims"] = [{"kind": k, "text": t, "url": u} for k, t, u, _ in claims]


def last_sent(db: DB):
    return db.one("SELECT * FROM issues WHERE status='sent' ORDER BY created_at DESC LIMIT 1")


def run_pipeline(cfg: Config, *, mode: str = "dry_run", issue_key: str | None = None,
                 now: datetime | None = None, http: Http | None = None, force: bool = False,
                 only_sources: list[str] | None = None, log=print) -> dict:
    """mode: dry_run - build and save a local issue without sending;
             send - the same, then email it (repeat sends of an issue key are blocked)."""
    db = DB(cfg.db_path)
    db.sync_sources(cfg.sources)
    r = Run(cfg, db, http or make_http(cfg), now=now, log=log)
    now = r.now
    issue_key = issue_key or f"manual-{now:%Y%m%dT%H%M%SZ}"
    r.run_id = db.ex("INSERT INTO runs(started_at, mode, issue_key) VALUES (?,?,?)",
                     iso(now), mode, issue_key).lastrowid
    result = {"issue_key": issue_key, "mode": mode, "sent": False}

    existing = db.one("SELECT status FROM issues WHERE issue_key=?", issue_key)
    if mode == "send" and existing and existing["status"] == "sent" and not force:
        log(f"Issue {issue_key} was already sent; repeat send blocked (use --force).")
        result["skipped"] = "already_sent"
        db.close()
        return result

    log("Fetching sources:")
    r.ingest(r.collect(only_sources))

    # Events for this issue: items discovered since the last sent issue, within the window.
    dcfg = cfg.rules["digest"]
    prev = last_sent(db)
    since = max(parse_dt(prev["created_at"]) if prev else now - timedelta(hours=dcfg["window_hours"]),
                now - timedelta(hours=dcfg["window_hours"]))
    fresh = iso(now - timedelta(hours=dcfg.get("max_item_age_hours", 96)))
    cand = [row["event_id"] for row in db.q(
        """SELECT DISTINCT event_id FROM items WHERE discovered_at >= ? AND event_id IS NOT NULL
           AND (published_at IS NULL OR published_at >= ?)""", iso(since), fresh)]
    log(f"Scoring {len(cand)} events...")
    evaluated = []
    for eid in cand:
        state = r.touched.get(eid, "nothing_new")
        if state == "nothing_new":
            # found after the last sent issue but in an earlier (dry) run: still new for the editor
            ev_row = db.one("SELECT created_at FROM events WHERE id=?", eid)
            state = "new_event" if parse_dt(ev_row["created_at"]) >= since else "new_outlet_on_known_event"
        evaluated.append(r.evaluate(eid, state))

    top, others = digest_mod.select(evaluated, cfg.rules)
    llm_note = None
    if top and llm_mod.available(cfg.rules):
        n = min(len(top), cfg.rules["llm"]["max_events"])
        log(f"LLM enrichment of {n} events ({llm_mod.model_name()})...")
        res, err = llm_mod.enrich(top[:n], cfg.rules)
        if err:
            db.error(r.run_id, "llm", "llm", err)
            log("  " + err)
            llm_note = err
        for i, ev in enumerate(top):
            if ev["id"] in res:
                top[i] = r.evaluate(ev["id"], ev["novelty_state"], llm_result=res[ev["id"]])
        top, others = digest_mod.select(top + others, cfg.rules)
    elif top:
        llm_note = "LLM enrichment off (no ANTHROPIC_API_KEY): rule-based scores only."

    health = digest_mod.source_health(db, cfg)
    errors = [dict(x) for x in db.q("SELECT source_id, kind, message FROM errors WHERE run_id=?", r.run_id)]
    subject, html, text = digest_mod.render(issue_key, now, top, others, health, errors, cfg, r.stats, mode, llm_note)
    content_hash = hashlib.sha256(json.dumps([[e["id"], e["score"]["total"]] for e in top + others]).encode()).hexdigest()
    html_path = cfg.out_dir / f"digest-{issue_key}.html"
    text_path = cfg.out_dir / f"digest-{issue_key}.txt"
    html_path.write_text(html, encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    result.update(subject=subject, html_path=str(html_path), text_path=str(text_path), stats=r.stats,
                  top=[e["id"] for e in top], others=[e["id"] for e in others], errors=errors)

    status = "dry_run"
    if mode == "send":
        miss = mailer.missing_settings(cfg.env)
        if not top and not others and dcfg.get("skip_if_nothing_new"):
            status = "skipped_empty"
            log("Nothing new: email not sent (skip_if_nothing_new).")
        elif prev and prev["content_hash"] == content_hash and not force:
            status = "skipped_duplicate"
            log("Same content as the last sent issue: email not sent.")
        elif miss:
            status = "failed"
            db.error(r.run_id, "mail", "config", "not set: " + ", ".join(miss))
            log("Mail not configured, missing: " + ", ".join(miss))
            result["missing_mail_settings"] = miss
        else:
            try:
                info = mailer.send(subject, html, text, issue_key, cfg.env)
                status = "sent"
                result["sent"] = True
                result["recipients"] = info["recipients"]
                log(f"Email sent: {', '.join(info['recipients'])}")
                for addr, why in (info.get("refused") or {}).items():
                    db.error(r.run_id, "mail", "refused", f"{addr}: {why}")
                    log(f"  x server refused {addr}: {why}")
            except Exception as e:  # noqa: BLE001
                status = "failed"
                db.error(r.run_id, "mail", "smtp", f"{type(e).__name__}: {e}")
                log(f"Send failed: {type(e).__name__}: {e}")

    if mode == "send" or not existing or existing["status"] != "sent":
        db.ex("""INSERT INTO issues(issue_key, created_at, status, sent_at, subject, html_path, text_path,
                 recipients, content_hash, event_ids_json, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(issue_key) DO UPDATE SET status=excluded.status, sent_at=excluded.sent_at,
                 subject=excluded.subject, html_path=excluded.html_path, text_path=excluded.text_path,
                 recipients=excluded.recipients, content_hash=excluded.content_hash,
                 event_ids_json=excluded.event_ids_json, run_id=excluded.run_id""",
              issue_key, iso(now), status, iso(utcnow()) if status == "sent" else None, subject, str(html_path),
              str(text_path), ",".join(result.get("recipients", [])), content_hash,
              json.dumps({"top": result["top"], "others": result["others"]}), r.run_id)
    if status == "sent":
        for e in top:
            db.ex("INSERT OR IGNORE INTO event_issue VALUES (?,?,?)", e["id"], issue_key, "top")
        for e in others:
            db.ex("INSERT OR IGNORE INTO event_issue VALUES (?,?,?)", e["id"], issue_key, "other")
    db.ex("UPDATE runs SET finished_at=?, stats_json=? WHERE id=?", iso(utcnow()),
          json.dumps({**r.stats, "status": status}), r.run_id)
    result["status"] = status
    db.close()
    return result

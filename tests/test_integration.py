"""End-to-end run with no network: a local HTTP server serves synthetic feeds
from tests/fixtures (not copies of real articles) and the pipeline runs whole."""
import copy
import gzip
import json
import sqlite3
import threading
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ROOT
from newstriage import llm as llm_mod
from newstriage import mailer
from newstriage.config import Config, load_rules
from newstriage.fetchers import fetch_rss
from newstriage.pipeline import make_http, run_pipeline

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


NOW = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    for f in FIXTURES.glob("*.xml"):
        (root / f.name).write_bytes(f.read_bytes())
    # gzip body with no Content-Encoding header, as some CDNs intermittently send
    (root / "lab-gz.xml").write_bytes(gzip.compress((FIXTURES / "lab.xml").read_bytes()))
    handler = partial(QuietHandler, directory=str(root))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def cfg(tmp_path, server):
    rules = copy.deepcopy(load_rules(ROOT / "config"))
    rules["http"]["default_crawl_delay"] = 0
    sources = [
        {"id": "test_news", "name": "Tech Daily", "url": f"{server}/news.xml", "type": "news",
         "role": "reporting", "method": "rss", "enabled": True, "relevance": "keywords"},
        {"id": "test_preprints", "name": "Preprints", "url": f"{server}/preprints.xml", "type": "preprint",
         "role": "primary", "method": "rss", "enabled": True, "relevance": "strong"},
        {"id": "test_lab", "name": "Example Lab", "url": f"{server}/lab.xml", "type": "lab",
         "role": "primary", "method": "rss", "enabled": True, "relevance": "all"},
        {"id": "broken", "name": "Broken feed", "url": f"{server}/missing.xml", "type": "news",
         "role": "reporting", "method": "rss", "enabled": True, "relevance": "keywords"},
        {"id": "off", "name": "Not connected", "url": "https://x.example", "method": "none",
         "enabled": False, "status_note": "not connected in this test"},
    ]
    data, out = tmp_path / "data", tmp_path / "out"
    data.mkdir()
    out.mkdir()
    return Config(sources=sources, rules=rules, root=ROOT, data_dir=data, out_dir=out, env={})


def test_full_pipeline_dry_run_then_send_guard(cfg, monkeypatch):
    logs = []
    res = run_pipeline(cfg, mode="dry_run", issue_key="2026-09-29-0800", now=NOW, log=logs.append)
    assert res["status"] == "dry_run" and not res["sent"]
    st = res["stats"]
    assert st["fetched"] == 9 and st["relevant"] == 7   # bakery and the off-topic preprint filtered out
    assert st["source_errors"] == 1                      # broken -> error log, run continues
    text = open(res["text_path"], encoding="utf-8").read()
    html = open(res["html_path"], encoding="utf-8").read()
    assert "DRY RUN" in res["subject"]
    assert "Nova-2" in text and "Broken feed" in text and "not connected in this test" in text
    assert "LLM enrichment off" in text
    assert "https://other.example/nova-2" in html and "bakery" not in text.lower()

    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    events = db.execute("SELECT id, title, score_json, flags_json FROM events").fetchall()
    assert len(events) == 5
    nova = db.execute("SELECT event_id, COUNT(*) n FROM items WHERE url IN (?, ?) GROUP BY event_id",
                      ("https://news.example/nova-2?utm_source=rss", "https://other.example/nova-2")).fetchall()
    assert len(nova) == 1 and nova[0]["n"] == 2               # two reports, one event (title similarity)
    paper = db.execute("SELECT event_id, COUNT(*) n FROM items WHERE keys_json LIKE '%2610.01234%' GROUP BY event_id").fetchall()
    assert len(paper) == 1 and paper[0]["n"] == 2             # paper + report about it, one event (arXiv key)
    assert db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] >= 7
    assert db.execute("SELECT COUNT(*) FROM history").fetchone()[0] >= 12
    assert ("broken", "http") in [tuple(e) for e in db.execute("SELECT source_id, kind FROM errors")]
    assert db.execute("SELECT consecutive_failures FROM sources WHERE id='broken'").fetchone()[0] == 1
    flags = {e["title"]: {f["code"] for f in json.loads(e["flags_json"])["flags"]} for e in events}
    assert "pii_risk" in flags["AI training dataset exposed personal records"]
    leak = next(v for k, v in flags.items() if k.startswith("Researchers find leaked"))
    assert "speculation_as_fact" in leak
    assert "no_primary_source" in next(v for k, v in flags.items() if "Nova-2 open-weight" in k or "open-weight Nova-2" in k)
    for e in events:
        sc = json.loads(e["score_json"])
        assert sc["total"] == sum(c["points"] for c in sc["criteria"].values())

    # second run: everything is a duplicate
    res2 = run_pipeline(cfg, mode="dry_run", issue_key="2026-09-29-0801", now=NOW, log=logs.append)
    assert res2["stats"]["new_items"] == 0 and res2["stats"]["duplicates"] == 7

    # send without mail settings: everything else works, no email goes out
    res3 = run_pipeline(cfg, mode="send", issue_key="2026-09-29-1700", now=NOW, log=logs.append)
    assert res3["status"] == "failed" and "NEWSTRIAGE_SMTP_HOST" in res3["missing_mail_settings"]

    # send with a stubbed SMTP, then the idempotency guard
    sent = []
    monkeypatch.setattr(mailer, "missing_settings", lambda env=None: {})
    monkeypatch.setattr(mailer, "send", lambda subject, html, text, key, env=None:
                        sent.append(key) or {"message_id": "<x>", "recipients": ["editor@example.org"]})
    res4 = run_pipeline(cfg, mode="send", issue_key="2026-09-29-1700", now=NOW, log=logs.append)
    assert res4["status"] == "sent" and sent == ["2026-09-29-1700"]
    res5 = run_pipeline(cfg, mode="send", issue_key="2026-09-29-1700", now=NOW, log=logs.append)
    assert res5.get("skipped") == "already_sent" and sent == ["2026-09-29-1700"]
    assert any("repeat send blocked" in m for m in logs)
    # --force bypasses the issue-key guard; identical content is still not re-sent
    res6 = run_pipeline(cfg, mode="send", issue_key="2026-09-29-1700", now=NOW, force=True, log=logs.append)
    assert res6["status"] == "sent" and sent == ["2026-09-29-1700", "2026-09-29-1700"]


def test_pipeline_with_llm_drops_uncited_claims(cfg, monkeypatch):
    """LLM path through the whole pipeline, with a fake SDK client."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen = {}

    def fake_enrich(events, rules, client=None):
        answers = []
        for ev in events:
            answers.append({
                "event_id": ev["id"], "headline": f"LLM headline {ev['id']}", "why_it_matters": "because",
                "established": [{"text": "cited claim", "source_url": ev["items"][0]["url"]},
                                {"text": "invented claim", "source_url": "https://invented.example/x"}],
                "unknown": ["insufficient data"], "document_status": "insufficient data",
                "misstatement_risk": "low", "editor_next_action": "verify",
                "scores": {"impact": {"points": 999, "reason": "too high", "insufficient_data": False},
                           "clarity": {"points": 11, "reason": "clear", "insufficient_data": False}}})
        fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"events": answers}))])))
        seen["n"] = len(events)
        return real_enrich(events, rules, client=fake)

    real_enrich = llm_mod.enrich
    monkeypatch.setattr(llm_mod, "enrich", fake_enrich)
    res = run_pipeline(cfg, mode="dry_run", issue_key="llm-run", now=NOW, log=lambda *_: None)
    assert seen["n"] >= 3
    text = open(res["text_path"], encoding="utf-8").read()
    assert "LLM headline" in text and "cited claim" in text
    assert "invented claim" not in text and "invented.example" not in text
    assert "uncited claim(s) dropped" in text
    db = sqlite3.connect(cfg.db_path)
    for (score_json,) in db.execute("SELECT score_json FROM events WHERE llm_json IS NOT NULL"):
        crit = json.loads(score_json)["criteria"]
        assert crit["impact"]["via"] == "rules"              # 999 was out of range -> rules
        assert crit["clarity"]["via"] == "llm" and crit["clarity"]["points"] == 11


def test_rss_fetch_handles_atom_and_unlabelled_gzip(cfg, server):
    http = make_http(cfg)
    src = {"id": "lab", "name": "Example Lab", "url": f"{server}/lab-gz.xml", "role": "primary", "type": "lab"}
    items = fetch_rss(src, http, NOW)
    assert [i["title"] for i in items] == ["Nova-2 technical report and model card"]
    assert items[0]["url"] == "https://lab.example/nova-2-report"

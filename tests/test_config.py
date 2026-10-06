import os

import pytest
import yaml

from conftest import ROOT
from newstriage.config import load_config, load_dotenv, load_rules


def test_dotenv_strips_inline_comments_and_keeps_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("NEWSTRIAGE_SMTP_PORT=587         # 587 = STARTTLS\n"
                   "NEWSTRIAGE_MAIL_FROM='bot@example.org'\n"
                   "NEWSTRIAGE_MAIL_TO=keep-me\n# comment\n\n", encoding="utf-8")
    monkeypatch.delenv("NEWSTRIAGE_SMTP_PORT", raising=False)
    monkeypatch.delenv("NEWSTRIAGE_MAIL_FROM", raising=False)
    monkeypatch.setenv("NEWSTRIAGE_MAIL_TO", "already-set")
    load_dotenv(env)
    assert os.environ["NEWSTRIAGE_SMTP_PORT"] == "587"
    assert os.environ["NEWSTRIAGE_MAIL_FROM"] == "bot@example.org"
    assert os.environ["NEWSTRIAGE_MAIL_TO"] == "already-set"


def test_rules_and_settings_must_not_overlap(tmp_path):
    (tmp_path / "rules.yaml").write_text("schedule: {timezone: UTC}\n")
    (tmp_path / "settings.yaml").write_text("schedule: {timezone: UTC}\n")
    with pytest.raises(ValueError, match="schedule"):
        load_rules(tmp_path)


def test_duplicate_source_ids_rejected(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    for f in ("rules.yaml", "settings.yaml"):
        (cfg / f).write_text((ROOT / "config" / f).read_text())
    (cfg / "sources.yaml").write_text("sources:\n  - {id: a, name: A, url: x}\n  - {id: a, name: B, url: y}\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_config(cfg, tmp_path / "d", tmp_path / "o")


def test_shipped_sources_are_well_formed():
    sources = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())["sources"]
    enabled = [s for s in sources if s.get("enabled")]
    assert 4 <= len(enabled) <= 8
    for s in enabled:
        assert s["method"] == "rss" and s["url"].startswith("https://")
        assert s["role"] in ("primary", "reporting", "signal")
        assert s.get("relevance", "keywords") in ("all", "keywords", "strong")


def test_shipped_doc_types_cover_llm_status_enum(rules):
    from newstriage.llm import STATUS_ENUM
    assert rules["doc_types"][-1] == {"type": "news", "status": "media report"}   # catch-all last
    assert "media report" in STATUS_ENUM
    assert set(rules["editorial"]["angle"]) >= {d["type"] for d in rules["doc_types"]}

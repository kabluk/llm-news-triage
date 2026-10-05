"""The guards around the model, tested without the network: a fake client
stands in for the Anthropic SDK."""
import json
from types import SimpleNamespace

import pytest

from conftest import make_event, make_item
from newstriage import llm
from newstriage.scoring import score_event


def _event(eid=1):
    ev = make_event([make_item("Acme releases Nova 2", url="https://news.example/nova-2"),
                     make_item("Introducing Nova 2", url="https://acme.example/nova-2", role="primary", stype="company")],
                    primary={"url": "https://acme.example/nova-2"})
    ev["id"] = eid
    return ev


def _answer(eid=1, **over):
    a = {"event_id": eid, "headline": "Acme ships Nova 2", "why_it_matters": "New open-weight model.",
         "established": [{"text": "Nova 2 is released", "source_url": "https://acme.example/nova-2"},
                         {"text": "It beats every rival", "source_url": "https://evil.example/made-up"},
                         {"text": "", "source_url": "https://news.example/nova-2"}],
         "unknown": ["pricing"], "document_status": "official announcement",
         "misstatement_risk": "none", "editor_next_action": "check the model card",
         "scores": {"impact": {"points": 20, "reason": "major release", "insufficient_data": False},
                    "clarity": {"points": 12, "reason": "clear", "insufficient_data": False}}}
    a.update(over)
    return a


class FakeClient:
    def __init__(self, text=None, stop_reason="end_turn", exc=None):
        self.calls = []
        self._text, self._stop, self._exc = text, stop_reason, exc
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        if self._exc:
            raise self._exc
        return SimpleNamespace(stop_reason=self._stop,
                               content=[SimpleNamespace(type="text", text=self._text)])


def test_claims_with_foreign_or_empty_urls_are_dropped(rules):
    out = llm.validate({"events": [_answer()]}, [_event()], rules)
    assert [c["source_url"] for c in out[1]["established"]] == ["https://acme.example/nova-2"]
    assert out[1]["dropped_claims"] == 2


def test_out_of_range_scores_fall_back_to_rules(rules, now):
    bad = _answer(scores={"impact": {"points": 99, "reason": "huge", "insufficient_data": False},
                          "clarity": {"points": -1, "reason": "?", "insufficient_data": False}})
    out = llm.validate({"events": [bad]}, [_event()], rules)
    s = out[1]["scores"]
    assert s["impact"]["points"] is None and s["impact"]["insufficient_data"]
    assert "out-of-range" in s["clarity"]["reason"]
    crit = score_event(_event(), rules, now, llm=out[1])["criteria"]
    assert crit["impact"]["via"] == "rules" and crit["clarity"]["via"] == "rules"


def test_score_range_comes_from_rules(rules):
    custom = {**rules, "scoring": {**rules["scoring"], "impact": {**rules["scoring"]["impact"], "max": 10}}}
    out = llm.validate({"events": [_answer()]}, [_event()], custom)
    assert out[1]["scores"]["impact"]["points"] is None       # 20 > 10


def test_insufficient_data_is_accepted(rules, now):
    na = _answer(established=[], scores={
        "impact": {"points": None, "reason": "insufficient data", "insufficient_data": True},
        "clarity": {"points": None, "reason": "insufficient data", "insufficient_data": True}})
    out = llm.validate({"events": [na]}, [_event()], rules)
    assert out[1]["established"] == [] and out[1]["dropped_claims"] == 0
    assert score_event(_event(), rules, now, llm=out[1])["criteria"]["impact"]["via"] == "rules"


def test_unknown_event_ids_are_ignored(rules):
    assert llm.validate({"events": [_answer(eid=42)]}, [_event()], rules) == {}


def test_schema_is_strict_and_status_enum_is_generic():
    item = llm.SCHEMA["properties"]["events"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["established"]["items"]["required"] == ["text", "source_url"]
    assert "insufficient data" in item["properties"]["document_status"]["enum"]
    assert set(item["properties"]["scores"]["required"]) == {"impact", "clarity"}


def test_off_without_key(rules):
    client = FakeClient(text="{}")
    assert llm.available(rules) is False
    assert llm.enrich([_event()], rules, client=client) == ({}, None)
    assert client.calls == []


def test_enrich_with_fake_client_validates_and_uses_model_from_env(rules, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = FakeClient(text=json.dumps({"events": [_answer()]}))
    out, err = llm.enrich([_event()], rules, client=client)
    assert err is None and out[1]["dropped_claims"] == 2
    call = client.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"]["format"]["schema"] is llm.SCHEMA
    assert "https://acme.example/nova-2" in call["messages"][0]["content"]
    monkeypatch.setenv("NEWSTRIAGE_MODEL", "claude-opus-5-5")
    llm.enrich([_event()], rules, client=client)
    assert client.calls[1]["model"] == "claude-opus-5-5"


@pytest.mark.parametrize("text,stop,expect", [
    ("not json", "end_turn", "failed validation"),
    ("{}", "refusal", "refused"),
    ('{"events": [', "max_tokens", "truncated"),
])
def test_bad_responses_degrade_to_rules(rules, monkeypatch, text, stop, expect):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    out, err = llm.enrich([_event()], rules, client=FakeClient(text=text, stop_reason=stop))
    assert out == {} and expect in err


def test_api_errors_do_not_raise(rules, monkeypatch):
    import anthropic
    try:                        # anthropic 1.x ships on httpx2; older releases on httpx
        import httpx2 as httpx
    except ImportError:
        import httpx
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APIConnectionError(request=req)
    out, err = llm.enrich([_event()], rules, client=FakeClient(exc=exc))
    assert out == {} and "connection" in err

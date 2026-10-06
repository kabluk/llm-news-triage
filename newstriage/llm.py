"""Optional LLM enrichment (Claude API) with strictly structured output.
Without ANTHROPIC_API_KEY the module is silently off and the pipeline runs on
rules alone.

Guards against made-up content:
- the model sees only metadata and short excerpts, never full articles or the web;
- every "established" claim must cite a source_url that was in the input;
  claims citing any other URL are dropped in code;
- every field allows "insufficient data", so the model is never forced to guess;
- scores outside the criterion's range are discarded and the rule-based score stays."""
from __future__ import annotations

import json
import os

from .scoring import LLM_CRITERIA
from .util import anthropic_key

DEFAULT_MODEL = "claude-sonnet-5-5"
# Models that accept the server-side refusal fallback ("fallbacks": "default").
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")

STATUS_ENUM = ["official announcement", "release notes", "research paper", "blog post",
               "media report", "insufficient data"]

_score = {
    "type": "object",
    "properties": {
        "points": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "reason": {"type": "string"},
        "insufficient_data": {"type": "boolean"},
    },
    "required": ["points", "reason", "insufficient_data"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer"},
                    "headline": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "established": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}, "source_url": {"type": "string"}},
                            "required": ["text", "source_url"],
                            "additionalProperties": False,
                        },
                    },
                    "unknown": {"type": "array", "items": {"type": "string"}},
                    "document_status": {"type": "string", "enum": STATUS_ENUM},
                    "misstatement_risk": {"type": "string"},
                    "editor_next_action": {"type": "string"},
                    "scores": {
                        "type": "object",
                        "properties": {name: _score for name in LLM_CRITERIA},
                        "required": list(LLM_CRITERIA),
                        "additionalProperties": False,
                    },
                },
                "required": ["event_id", "headline", "why_it_matters", "established", "unknown",
                             "document_status", "misstatement_risk", "editor_next_action", "scores"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}

SYSTEM = """You help the editor of a news digest about {topic}.
You work only with the metadata provided in the user message. Rules:
- Do not invent facts, quotes, documents or links. Every item in `established` must be supported by one of the input materials and must copy that material's url verbatim into `source_url`.
- If the input does not support an answer, say "insufficient data" in text fields, or set insufficient_data=true and points=null in scores. That is always an acceptable answer.
- Keep these apart: announced vs. available; a preprint vs. a peer-reviewed result; a claim by the publisher vs. an independent measurement; a rumor or leak vs. a confirmed fact.
- Do not name private individuals in `headline`.
- Scores: {score_help}.
- Write in plain, short English."""


def model_name() -> str:
    return os.environ.get("NEWSTRIAGE_MODEL") or DEFAULT_MODEL


def available(rules: dict) -> bool:
    return bool(rules.get("llm", {}).get("enabled_if_key") and anthropic_key())


def score_ranges(rules: dict) -> dict[str, int]:
    return {name: rules["scoring"][name]["max"] for name in LLM_CRITERIA}


def system_prompt(rules: dict) -> str:
    help_ = "; ".join(f"{n}: 0-{mx}, {rules['scoring'][n].get('label', n)}" for n, mx in score_ranges(rules).items())
    return SYSTEM.format(topic=rules.get("topic", "the configured topic"), score_help=help_)


def _payload(events: list[dict]) -> list[dict]:
    out = []
    for ev in events:
        out.append({
            "event_id": ev["id"],
            "doc_status_by_rules": ev.get("doc_status"),
            "primary_url": (ev.get("primary") or {}).get("url"),
            "materials": [{"url": i["url"], "outlet": i.get("outlet"), "title": i["title"],
                           "published_at": i.get("published_at"), "snippet": i.get("snippet")}
                          for i in ev["items"][:8]],
        })
    return out


def validate(result: dict, events: list[dict], rules: dict) -> dict[int, dict]:
    """Enforces the guards in code. Unknown event ids are ignored; claims whose
    source_url is not among the event's input URLs are dropped; out-of-range
    scores become "insufficient data" so the rule-based score stays."""
    by_id = {ev["id"]: ev for ev in events}
    ranges = score_ranges(rules)
    out = {}
    for e in result.get("events", []):
        ev = by_id.get(e.get("event_id"))
        if not ev:
            continue
        allowed = {i["url"] for i in ev["items"]} | {(ev.get("primary") or {}).get("url")}
        allowed.discard(None)
        kept, dropped = [], 0
        for c in e.get("established", []):
            if c.get("source_url") in allowed and (c.get("text") or "").strip():
                kept.append(c)
            else:
                dropped += 1
        e["established"] = kept
        e["dropped_claims"] = dropped
        for name, mx in ranges.items():
            s = e.get("scores", {}).get(name)
            if s is None:
                continue
            if s.get("points") is not None and not (0 <= s["points"] <= mx):
                s.update(points=None, insufficient_data=True,
                         reason=s.get("reason", "") + " [out-of-range score discarded]")
        out[ev["id"]] = e
    return out


def enrich(events: list[dict], rules: dict, client=None) -> tuple[dict[int, dict], str | None]:
    """Returns ({event_id: validated result}, error | None). Never raises on API errors."""
    if not events or not available(rules):
        return {}, None
    import anthropic

    cfg = rules["llm"]
    model = model_name()
    client = client or anthropic.Anthropic(api_key=anthropic_key())
    kwargs = {}
    if model in FALLBACK_MODELS:
        kwargs = {"extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
                  "extra_body": {"fallbacks": "default"}}
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            system=system_prompt(rules),
            messages=[{"role": "user", "content": "Events:\n" + json.dumps(_payload(events), ensure_ascii=False, default=str)}],
            output_config={"effort": cfg.get("effort", "medium"),
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            **kwargs,
        )
    except anthropic.RateLimitError as e:
        return {}, f"LLM: rate limited ({e})"
    except anthropic.APIStatusError as e:
        return {}, f"LLM: HTTP {e.status_code}: {str(e)[:200]}"
    except anthropic.APIConnectionError as e:
        return {}, f"LLM: connection error ({e})"
    if resp.stop_reason == "refusal":
        return {}, "LLM: model refused; rule-based scores used"
    if resp.stop_reason == "max_tokens":
        return {}, "LLM: response truncated at max_tokens; rule-based scores used"
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return validate(json.loads(text), events, rules), None
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return {}, f"LLM: response failed validation ({type(e).__name__}: {e})"

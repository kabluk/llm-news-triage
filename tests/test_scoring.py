from conftest import make_event, make_item
from newstriage.scoring import CRITERIA, recommendation, score_event


def test_rubric_maxima_sum_to_100(rules):
    assert sum(rules["scoring"][k]["max"] for k in CRITERIA) == 100
    assert set(rules["scoring"]) == set(CRITERIA)


def test_all_criteria_within_bounds_and_sum(rules, now):
    ev = make_event([make_item("Lab releases open-weight reasoning model with state-of-the-art benchmark results",
                               snippet="The 70B model is available via API; millions of developers can try it")])
    s = score_event(ev, rules, now)
    assert list(s["criteria"]) == CRITERIA
    for k, c in s["criteria"].items():
        assert c["max"] == rules["scoring"][k]["max"]
        assert 0 <= c["points"] <= c["max"]
        assert c["reason"]
    assert s["total"] == sum(c["points"] for c in s["criteria"].values())
    assert s["total"] <= 100


def test_impact_capped_at_max(rules, now):
    ev = make_event([make_item("release benchmark safety pricing dataset GPU launch jailbreak deprecation weights")])
    assert score_event(ev, rules, now)["criteria"]["impact"]["points"] == rules["scoring"]["impact"]["max"]


def test_irrelevant_topic_gets_zero_impact(rules, now):
    ev = make_event([make_item("Company announces new office furniture procurement schedule")])
    assert score_event(ev, rules, now)["criteria"]["impact"]["points"] == 0


def test_low_signal_format_is_downweighted(rules, now):
    plain = make_event([make_item("Lab releases open-weight model with state-of-the-art results")])
    webinar = make_event([make_item("Webinar: lab releases open-weight model with state-of-the-art results")])
    a = score_event(plain, rules, now)["criteria"]["impact"]["points"]
    b = score_event(webinar, rules, now)["criteria"]["impact"]["points"]
    assert b < a


def test_primary_source_order(rules, now):
    signal = make_event([make_item("x", extra={"signal_only": True})])
    one = make_event([make_item("x", outlet="Wire")])
    two = make_event([make_item("x", outlet="Wire"), make_item("y", outlet="Daily", url="https://e.org/2")])
    prim = make_event([make_item("x", role="primary", stype="company", outlet="Lab")])
    both = make_event([make_item("x", role="primary", stype="company", outlet="Lab"),
                       make_item("y", outlet="Wire", url="https://e.org/2")])
    v = [score_event(e, rules, now)["criteria"]["primary_source"]["points"] for e in (signal, one, two, prim, both)]
    assert v == sorted(v) and v[0] < v[-1] and v[-1] == 20
    assert score_event(signal, rules, now)["criteria"]["primary_source"]["insufficient_data"]


def test_freshness_steps(rules, now):
    def f(published):
        return score_event(make_event([make_item("x", published=published)]), rules, now)["criteria"]["freshness"]
    assert f("2026-09-29T06:00:00+00:00")["points"] == 10     # 10 h
    assert f("2026-09-27T20:00:00+00:00")["points"] == 8      # 44 h
    assert f("2026-09-20T00:00:00+00:00")["points"] == 0      # 9 days
    unknown = score_event(make_event([make_item("x", published=None)]), rules, now)["criteria"]["freshness"]
    assert unknown["points"] == 0 and unknown["insufficient_data"]


def test_event_date_preferred_over_publication(rules, now):
    ev = make_event([make_item("x", published="2026-09-29T12:00:00+00:00")], event_date="2026-09-15T00:00:00+00:00")
    c = score_event(ev, rules, now)["criteria"]["freshness"]
    assert c["points"] == 0 and "event date" in c["reason"]


def test_flags_reduce_clarity(rules, now):
    ev = make_event([make_item("x")])
    a = score_event(ev, rules, now, n_flags=0)["criteria"]["clarity"]["points"]
    b = score_event(ev, rules, now, n_flags=2)["criteria"]["clarity"]["points"]
    assert b < a


def test_thresholds_and_flags_block_ready(rules):
    assert recommendation(80, [], rules) == {"tier": "feature", "label": "feature in the digest", "ready": True}
    assert recommendation(75, [], rules)["tier"] == "feature"
    assert recommendation(74, [], rules)["tier"] == "editor_decision"
    assert recommendation(55, [], rules)["tier"] == "editor_decision"
    assert recommendation(54, [], rules)["tier"] == "log"
    flagged = recommendation(95, [{"code": "pii_risk"}], rules)
    assert flagged["ready"] is False and "stop flags" in flagged["label"]


def test_thresholds_are_configurable(rules):
    custom = {**rules, "thresholds": {"feature": 90, "editor_decision": 40}}
    assert recommendation(80, [], custom)["tier"] == "editor_decision"
    assert recommendation(45, [], custom)["tier"] == "editor_decision"


def test_llm_scores_used_only_when_sufficient(rules, now):
    ev = make_event([make_item("Company office furniture")])
    llm_ok = {"scores": {"impact": {"points": 20, "reason": "LLM", "insufficient_data": False},
                         "clarity": {"points": 12, "reason": "LLM", "insufficient_data": False}}}
    llm_na = {"scores": {"impact": {"points": None, "reason": "insufficient data", "insufficient_data": True},
                         "clarity": {"points": None, "reason": "insufficient data", "insufficient_data": True}}}
    ok = score_event(ev, rules, now, llm=llm_ok)["criteria"]
    assert ok["impact"]["points"] == 20 and ok["impact"]["via"] == "llm"
    assert ok["clarity"]["points"] == 12 and ok["clarity"]["via"] == "llm"
    na = score_event(ev, rules, now, llm=llm_na)["criteria"]
    assert na["impact"]["via"] == "rules" and na["clarity"]["via"] == "rules"


def test_reach_capped_when_no_impact(rules, now):
    ev = make_event([make_item("Company picnic draws millions of visitors", outlet="A"),
                     make_item("Company picnic recap", outlet="B", url="https://b.org/1"),
                     make_item("Company picnic photos", outlet="C", url="https://c.org/1")])
    c = score_event(ev, rules, now)["criteria"]
    assert c["impact"]["points"] == 0
    assert c["reach"]["points"] <= rules["scoring"]["reach"]["low_impact_cap"]


def test_reach_grows_with_independent_outlets(rules, now):
    def reach(n):
        items = [make_item("Lab releases model", outlet=f"O{i}", url=f"https://o{i}.org/1") for i in range(n)]
        return score_event(make_event(items), rules, now)["criteria"]["reach"]["points"]
    assert reach(1) < reach(2) < reach(3)

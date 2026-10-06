from datetime import timedelta

from conftest import make_item
from newstriage.classify import extract_keys, is_relevant
from newstriage.cluster import EventRef, dedupe, match_event
from newstriage.util import canonical_url, tokens


def test_canonical_url_strips_tracking_www_fragment_slash_amp():
    a = canonical_url("http://www.Example.com/news/story/?utm_source=x&id=5#top")
    b = canonical_url("https://example.com/news/story?id=5&fbclid=abc")
    c = canonical_url("https://m.example.com/news/story/amp?id=5")
    assert a == b == c == "https://example.com/news/story?id=5"


def test_canonical_url_keeps_meaningful_query():
    assert canonical_url("https://x.org/p?id=1") != canonical_url("https://x.org/p?id=2")


def test_dedupe_against_known_and_within_run():
    known = {canonical_url("https://example.com/a")}
    items = [
        make_item("One", url="https://www.example.com/a/?utm_medium=rss"),     # already in the DB
        make_item("Two", url="https://example.com/b"),
        make_item("Two", url="https://example.com/b#comments"),               # same URL
        make_item("Lab releases new coding model", url="https://example.com/c", outlet="Wire"),
        make_item("New coding model releases from lab", url="https://example.com/c2", outlet="Wire"),  # same title, same outlet
    ]
    new, dup = dedupe(items, known)
    assert [i["url"] for i in new] == ["https://example.com/b", "https://example.com/c"]
    assert len(dup) == 3


def test_same_title_different_outlets_is_not_url_duplicate():
    items = [make_item("Lab releases model", url="https://a.com/1", outlet="A"),
             make_item("Lab releases model", url="https://b.com/1", outlet="B")]
    new, dup = dedupe(items, set())
    assert len(new) == 2 and not dup


def test_cluster_by_arxiv_key(rules, now):
    it = make_item("A study of tokenizer drift", snippet="Preprint arXiv:2610.01234 shows ...")
    it["keys"] = extract_keys(it, rules)
    assert "arxiv:2610.01234" in it["keys"]
    paper = make_item("Tokenizer drift in multilingual models", url="https://arxiv.org/abs/2610.01234")
    assert extract_keys(paper, rules) == ["arxiv:2610.01234"]
    ev = EventRef(7, {"arxiv:2610.01234"}, [tokens("Unrelated wording")], now)
    eid, why = match_event(it, [ev], rules, now)
    assert eid == 7 and "arxiv:2610.01234" in why


def test_cluster_by_title_similarity(rules, now):
    ev = EventRef(3, set(), [tokens("Acme AI releases Nova open-weight language model for developers")], now)
    it = make_item("Acme AI releases open-weight Nova language model")
    eid, _ = match_event(it, [ev], rules, now)
    assert eid == 3


def test_cluster_does_not_merge_different_stories(rules, now):
    ev = EventRef(3, set(), [tokens("Acme AI releases Nova open-weight language model for developers")], now)
    it = make_item("Study finds benchmark contamination in code evaluation datasets")
    eid, _ = match_event(it, [ev], rules, now)
    assert eid is None


def test_cluster_respects_window(rules, now):
    old = EventRef(3, {"arxiv:2601.00001"}, [], now - timedelta(days=30))
    it = make_item("Follow-up on an old paper")
    it["keys"] = ["arxiv:2601.00001"]
    eid, _ = match_event(it, [old], rules, now)
    assert eid is None


def test_cluster_keeps_versions_with_different_numbers_apart(rules, now):
    ev = EventRef(5, set(), [tokens("Acme releases Nova 3 language model with longer context window")], now, [{"3"}])
    it = make_item("Acme releases Nova 4 language model with longer context window")
    assert match_event(it, [ev], rules, now)[0] is None
    same = make_item("Acme releases Nova 3 language model with longer context window")
    assert match_event(same, [ev], rules, now)[0] == 5


def test_relevance_strong_core_context_and_exclude(rules):
    assert is_relevant(make_item("New open-weight model from a small lab"), rules)[0]          # strong
    assert is_relevant(make_item("AI agent benchmark released"), rules)[0]                      # core + context
    assert not is_relevant(make_item("AI is everywhere"), rules)[0]                             # core only
    assert not is_relevant(make_item("Business model of a bakery launches a research dataset"), rules)[0]  # exclude
    assert is_relevant(make_item("Local bakery wins award"), rules, "all")[0]


def test_relevance_case_sensitive_terms(rules):
    # "AI" must not match inside "aid"/"said"; LLM counts only as a whole word
    assert not is_relevant(make_item("Charity aid training program launches"), rules)[0]
    assert is_relevant(make_item("An LLM for chemistry"), rules, "strong")[0]
    assert not is_relevant(make_item("LLMNR protocol vulnerability"), rules, "strong")[0]

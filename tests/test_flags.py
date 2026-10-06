from conftest import make_event, make_item
from newstriage.flags import compute_flags


def codes(ev, rules):
    return {f["code"] for f in compute_flags(ev, rules)[0]}


def test_clean_primary_event_has_no_flags(rules):
    ev = make_event([make_item("Lab releases Nova 2 model card and technical report", role="primary", stype="lab")],
                    doc_type="blog_post")
    assert codes(ev, rules) == set()


def test_single_unverified_source(rules):
    ev = make_event([make_item("Startup claims breakthrough model beats GPT on every benchmark")])
    assert "single_unverified_source" in codes(ev, rules)
    # a second independent outlet is a basis
    ev2 = make_event([make_item("Startup claims breakthrough model", outlet="A"),
                      make_item("Startup model tops leaderboard", outlet="B", url="https://b.org/1")])
    assert "single_unverified_source" not in codes(ev2, rules)
    # the publisher's own page is a basis too
    ev3 = make_event([make_item("Our breakthrough model", role="primary", stype="company")])
    assert "single_unverified_source" not in codes(ev3, rules)


def test_no_primary_source_for_reported_release(rules):
    ev = make_event([make_item("Acme launches Nova 2 for enterprise customers")])
    assert "no_primary_source" in codes(ev, rules)
    with_primary = make_event([make_item("Acme launches Nova 2", outlet="Wire"),
                               make_item("Introducing Nova 2", role="primary", stype="company", outlet="Acme",
                                         url="https://acme.example/nova-2")])
    assert "no_primary_source" not in codes(with_primary, rules)


def test_speculation_as_fact(rules):
    ev = make_event([make_item("Next flagship model reportedly due in March, leaked memo shows")])
    f = compute_flags(ev, rules)[0]
    assert any(x["code"] == "speculation_as_fact" for x in f)
    confirmed = make_event([make_item("Next flagship model reportedly due in March", outlet="Wire"),
                            make_item("Flagship model launch date", role="primary", stype="company", url="https://p.org/1")])
    assert "speculation_as_fact" not in codes(confirmed, rules)


def test_pii(rules):
    assert "pii_risk" in codes(make_event([make_item("Dataset exposed", snippet="It listed 555-123-4567 and more")]), rules)
    assert "pii_risk" in codes(make_event([make_item("Dataset exposed", snippet="Contact jane.doe@example.org")]), rules)
    assert "pii_risk" not in codes(make_event([make_item("Dataset of 12,000 images released")]), rules)


def test_number_mismatch(rules):
    ev = make_event([make_item("Nova 2 has 70B parameters", outlet="A"),
                     make_item("Nova 2 has 40B parameters", outlet="B", url="https://b.org/2")])
    assert "date_or_number_mismatch" in codes(ev, rules)
    close = make_event([make_item("Nova 2 scores 81% on the benchmark", outlet="A"),
                        make_item("Nova 2 scores 80 percent on the benchmark", outlet="B", url="https://b.org/2")])
    assert "date_or_number_mismatch" not in codes(close, rules)
    pct = make_event([make_item("Error rate down 50%", outlet="A"),
                      make_item("Error rate down 20 percent", outlet="B", url="https://b.org/2")])
    assert "date_or_number_mismatch" in codes(pct, rules)     # "%" and "percent" are one unit


def test_date_mismatch(rules):
    ev = make_event([make_item("x", event_date="2026-09-20T00:00:00+00:00"),
                     make_item("y", url="https://b.org/2", event_date="2026-09-27T00:00:00+00:00")])
    assert "date_or_number_mismatch" in codes(ev, rules)


def test_preprint_gets_caution_not_flag(rules):
    ev = make_event([make_item("Scaling laws for small models", role="primary", stype="preprint")],
                    doc_type="research_paper")
    flags, cautions = compute_flags(ev, rules)
    assert not flags and any("peer-reviewed" in c for c in cautions)

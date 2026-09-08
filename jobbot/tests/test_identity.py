"""Canonical URL / fingerprint matching across sources."""

from jobbot.identity import ats_key, canonical_url, fingerprint, is_ats_url, prefer_url


def test_canonical_url_strips_tracking():
    raw = "https://boards.greenhouse.io/acme/jobs/123?utm_source=jobot&utm_medium=email"
    assert canonical_url(raw) == "https://boards.greenhouse.io/acme/jobs/123"


def test_ats_key_greenhouse():
    assert ats_key("https://job-boards.greenhouse.io/acme/jobs/99") == "greenhouse:99"


def test_prefer_ats_over_tracker():
    tracker = "https://e.jobot.com/click?x=1"
    ats = "https://jobs.lever.co/acme/aaaaaaaaaaaaaaaa"
    assert prefer_url(tracker, ats) == ats
    assert prefer_url(ats, tracker) == ats


def test_fingerprint_ignores_punctuation_and_case():
    a = fingerprint("Acme, Inc.", "Data Engineer!", "Los Angeles, CA")
    b = fingerprint("acme inc", "data engineer", "los angeles ca")
    assert a == b
    assert a


def test_fingerprint_requires_company_and_title():
    assert fingerprint("", "Data Engineer", "LA") == ""
    assert fingerprint("Acme", "", "LA") == ""


def test_is_ats_url():
    assert is_ats_url("https://boards.greenhouse.io/x/jobs/1")
    assert not is_ats_url("https://www.linkedin.com/jobs/view/1")


def test_a_malformed_url_cannot_take_down_the_run():
    """One bad link in one marketing email used to abort the whole inbox pass.

    urlsplit raises ValueError("Invalid IPv6 URL") on a stray bracket, and this
    is called while classifying every message, so nothing got ingested.
    """
    # These genuinely raise inside urlsplit and must come back empty.
    for bad in ("http://[bad", "https://exa[mple.com/x", "http://["):
        assert canonical_url(bad) == ""
        assert is_ats_url(bad) is False
        assert ats_key(bad) == ""

    # These parse without raising. The guarantee is only that nothing blows up
    # and nothing is mistaken for an ATS link.
    for junk in ("][", "not a url", "mailto:x@y.z", "javascript:void(0)"):
        canonical_url(junk)
        assert is_ats_url(junk) is False
        assert ats_key(junk) == ""


def test_hardening_did_not_break_ordinary_urls():
    url = "https://boards.greenhouse.io/acme/jobs/1?gh_src=x&utm_source=y"
    assert canonical_url(url) == "https://boards.greenhouse.io/acme/jobs/1?gh_src=x"
    assert is_ats_url(url) is True

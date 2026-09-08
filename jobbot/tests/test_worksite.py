"""Tests for work-arrangement detection and skill overlap.

Both were added after the review queue's top-ranked job turned out to be a
Seattle hybrid role requiring commutable distance with no relocation
assistance, for a candidate in Los Angeles — scored 84 because the location
string happened to contain "CA".
"""

from __future__ import annotations

import pytest

from jobbot import config, skills
from jobbot.scoring import score_posting
from jobbot.worksite import Worksite, classify


# ── Work arrangement ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "This role is fully remote.",
        "We are a remote-first company.",
        "100% remote position.",
        "Remote (US) — work from anywhere in the United States.",
        "This role is completely remote friendly within the United States.",
    ],
)
def test_remote_detected(text):
    assert classify(text).worksite is Worksite.REMOTE


@pytest.mark.parametrize(
    "text",
    [
        "needs to be in a commutable distance from one of our offices",
        "This is a hybrid role.",
        "This role will need to be in the office for in-person collaboration [1x per week]",
        "Employees are expected to be in the office 3 days a week in office.",
    ],
)
def test_hybrid_detected(text):
    assert classify(text).worksite is Worksite.HYBRID


def test_hybrid_beats_remote_label():
    """The real regression: "Remote - US" in the header, commute in the body."""
    verdict = classify(
        "This role will need to be in the office for in-person collaboration "
        "[1x per week] and therefore needs to be in a commutable distance from "
        "one of the following offices in Seattle.",
        location="Seattle, WA, US; San Francisco, CA, US",
    )
    assert verdict.worksite is Worksite.HYBRID
    assert verdict.days_in_office == 1


def test_days_in_office_parsed():
    assert classify("in the office [2x per week]").days_in_office == 2
    assert classify("hybrid, 3 days a week in office").days_in_office == 3


def test_relocation_not_offered():
    v = classify("This position is not eligible for relocation assistance. Hybrid role.")
    assert v.relocation_offered is False


def test_relocation_offered():
    v = classify("Hybrid role. Relocation assistance is provided for this position.")
    assert v.relocation_offered is True


def test_unclear_when_nothing_stated():
    assert classify("Build great software with a great team.").worksite is Worksite.UNCLEAR


def test_label_carries_the_terms():
    v = classify(
        "commutable distance from our Seattle office [1x per week]. "
        "This position is not eligible for relocation assistance."
    )
    assert "1x/week" in v.label
    assert "no relocation" in v.label


# ── Skill overlap ──────────────────────────────────────────────────────────────

@pytest.fixture
def profile_skills():
    return skills.canonicalize(
        ["Python", "SQL", "PostgreSQL", "FastAPI", "LightGBM", "XGBoost",
         "PyTorch", "pandas", "NumPy", "GitHub Actions", "Django"]
    )


def test_canonicalize_maps_profile_skills(profile_skills):
    assert "python" in profile_skills
    assert "lightgbm" in profile_skills
    assert "swift" not in profile_skills


def test_overlap_rewarded(profile_skills):
    match = skills.compare(
        "Machine Learning Engineer",
        "Strong Python. Gradient-boosted trees like LightGBM/XGBoost. Production code.",
        profile_skills,
    )
    assert match.points > 0
    assert "lightgbm" in match.overlap


def test_foreign_core_tech_penalized(profile_skills):
    match = skills.compare(
        "Software Engineer, iOS",
        "Build Pinner-facing features in iOS. Swift experience required. iOS frameworks.",
        profile_skills,
    )
    assert match.points < 0
    assert "ios" in match.foreign_core or "swift" in match.foreign_core


def test_passing_mention_not_penalized(profile_skills):
    """One nice-to-have mention of Kubernetes must not sink a good match."""
    match = skills.compare(
        "Data Engineer",
        "Python, SQL, and PostgreSQL required. Kubernetes a plus.",
        profile_skills,
    )
    assert match.points > 0
    assert not match.foreign_core


def test_the_regression_ios_no_longer_outranks_ml(profile_skills):
    """The exact inversion that prompted this module."""
    ios, _, _ = score_posting(
        "Software Engineer, iOS",
        "Build Pinner-facing frontend features in iOS. Swift. iOS. iOS frameworks.",
        "San Francisco, CA, US; Remote, US",
        profile_skills=profile_skills,
    )
    ml, _, _ = score_posting(
        "Machine Learning Engineer",
        "Strong Python skills and production code. Tabular classification with "
        "gradient-boosted decision trees like LightGBM/XGBoost/CatBoost. "
        "Experience with LLM APIs. This role is fully remote.",
        "Remote US",
        profile_skills=profile_skills,
    )
    assert ml.total > ios.total


# ── Location interaction ───────────────────────────────────────────────────────

def test_home_metro_not_penalized_for_being_onsite():
    """A five-day onsite role in Santa Monica costs nothing to take."""
    local, _, _ = score_posting(
        "Software Engineer",
        "Python. This is an on-site role based out of our Santa Monica office.",
        "Santa Monica, CA",
    )
    assert not local.rejected
    assert any("no move needed" in r for r in local.reasons)


def test_home_metro_outranks_remote_elsewhere():
    local, _, _ = score_posting("Software Engineer", "Python.", "Los Angeles, CA")
    remote, _, _ = score_posting(
        "Software Engineer", "Python. This role is fully remote.", "Remote - US"
    )
    assert local.total > remote.total


def test_hybrid_out_of_state_ranked_below_remote():
    hybrid, _, _ = score_posting(
        "Software Engineer",
        "Python. Must be within commutable distance of our Seattle office. "
        "This position is not eligible for relocation assistance.",
        "Seattle, WA, US",
    )
    remote, _, _ = score_posting(
        "Software Engineer", "Python. This role is fully remote.", "Remote - US"
    )
    assert remote.total > hybrid.total


@pytest.mark.parametrize(
    "location",
    [
        "Remote, Ontario",
        "Remote - Ontario, Canada",
        "Vancouver, British Columbia, Canada",
        "Toronto, Ontario, Canada",
        "British Columbia; Calgary",
        "Kitchener-Waterloo, ON",
    ],
)
def test_canadian_locations_flagged(location):
    """"British Columbia, Canada" contains ", ca" — matching the California
    state code without a word boundary let 11 Canadian jobs into the queue."""
    assert config.looks_non_us(location), location


@pytest.mark.parametrize(
    "location",
    [
        "San Francisco, CA",
        "Los Angeles, CA, US",
        "Remote, US (EST) OR Remote, Ontario, Canada",
        "San Francisco, CA, US; Remote, US",
        "Remote - United States",
    ],
)
def test_us_locations_not_flagged(location):
    assert not config.looks_non_us(location), location


def test_canadian_job_ranks_below_us_equivalent():
    ca, _, _ = score_posting("Software Engineer", "Python. Fully remote.", "Remote, Ontario")
    us, _, _ = score_posting("Software Engineer", "Python. Fully remote.", "Remote, US")
    assert us.total > ca.total


def test_remote_only_preference_knocks_out_hybrid(monkeypatch):
    monkeypatch.setattr(config, "RELOCATION", "remote_only")
    score, _, _ = score_posting(
        "Software Engineer",
        "Python. Hybrid role, commutable distance from our Seattle office.",
        "Seattle, WA",
    )
    assert score.rejected
    assert "work arrangement" in score.knockout


def test_relocation_offered_is_credited_not_just_penalised():
    """Scoring had the penalty for "no relocation assistance" but no mirror
    for postings that explicitly fund the move, so an out-of-metro role that
    pays to relocate ranked identically to one that never mentions it. For
    someone in LA who would move but cannot self-fund it, that is the
    distinction that decides whether a San Francisco role is reachable."""
    onsite = "This role is onsite in our San Francisco office five days a week."
    offers = onsite + " We offer relocation assistance for this position."
    denies = onsite + " Relocation assistance is not provided."

    plain_score, _, plain_site = score_posting("Data Engineer", onsite, "San Francisco, CA")
    offer_score, _, offer_site = score_posting("Data Engineer", offers, "San Francisco, CA")
    deny_score, _, deny_site = score_posting("Data Engineer", denies, "San Francisco, CA")

    assert offer_site.relocation_offered is True
    assert deny_site.relocation_offered is False
    assert plain_site.relocation_offered is None

    assert offer_score.total > plain_score.total > deny_score.total
    assert any("relocation assistance offered" in r for r in offer_score.reasons)


@pytest.mark.parametrize("text,expected", [
    # Real phrasings from the corpus. 870 of 1,296 postings mentioning
    # relocation were missed before; the dominant miss was "...and offer
    # relocation assistance", where "we" is not adjacent to "offer".
    ("We use an in-person work model and offer relocation assistance to new "
     "employees", True),
    ("Relocation expense coverage to NYC or SF (if needed)", True),
    ("Relocation assistance may also be provided for eligible candidates", True),
    ("Full relocation support", True),
    # The refusals must keep winning: NO_RELOCATION is checked first, which is
    # what makes widening the offered list safe. "will not be provided" was
    # itself missed by the old "(?:is )?not" form -- 58 postings phrase it that
    # way, and each would have flipped to a false offer.
    ("Relocation assistance will not be provided for this role", False),
    ("Relocation assistance is not provided.", False),
    ("We do not offer relocation.", False),
    ("No relocation package is available.", False),
    ("Unable to offer relocation for this position.", False),
    # Silence stays silence -- not an offer, not a refusal.
    ("This role is onsite in our San Francisco office.", None),
])
def test_relocation_detection_both_directions(text, expected):
    assert classify(text, "San Francisco, CA").relocation_offered is expected


def test_a_posting_that_offers_then_refuses_reads_as_a_refusal():
    """Order is load-bearing, so it is pinned."""
    both = ("We offer relocation assistance for most roles. Relocation "
            "assistance will not be provided for this role.")
    assert classify(both, "San Francisco, CA").relocation_offered is False

"""Tests for eligibility gate detection.

Gate classification decides which postings ever reach the review queue, so a
false ENROLLMENT hides a job that was actually reachable, and a false OPEN
wastes an application. Both directions are worth pinning down.

Run:  python -m pytest jobbot/tests/ -q
"""

from __future__ import annotations

import pytest

from datetime import date

from jobbot.gating import Gate, classify, deadline_passed, min_years_required
from jobbot.scoring import score_posting


# ── Enrollment gates ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "Must be currently enrolled in an accredited university.",
        "Candidates should be actively enrolled in a degree program.",
        "You are currently pursuing a Bachelor's in Computer Science.",
        "Open to rising juniors and rising seniors.",
        "Must be graduating in Spring 2027.",
        "Class of 2026 only.",
        "You will return to school following the internship.",
        "Must be a current student in good standing.",
        "Available for a 12-week summer internship.",
    ],
)
def test_enrollment_detected(text):
    assert classify(text).gate is Gate.ENROLLMENT


def test_enrollment_beats_equivalent_language():
    """An enrollment requirement is absolute — it outranks friendlier wording."""
    text = (
        "Bachelor's degree or equivalent practical experience. "
        "Must be currently enrolled for the duration of the internship."
    )
    assert classify(text).gate is Gate.ENROLLMENT


# ── Equivalent-experience openings ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "BS in Computer Science or equivalent practical experience.",
        "Degree or comparable experience.",
        "We do not require a college degree.",
        "No degree is required for this role.",
        "Bootcamp graduates are welcome to apply.",
        "Self-taught engineers are welcome.",
        "Relevant experience in lieu of a degree.",
    ],
)
def test_equivalent_detected(text):
    assert classify(text).gate is Gate.EQUIVALENT_OK


def test_equivalent_beats_hard_degree_in_same_posting():
    """The pairing that keeps a posting open despite a degree line."""
    text = (
        "Requirements: Bachelor's degree required in Computer Science, "
        "or equivalent practical experience."
    )
    assert classify(text).gate is Gate.EQUIVALENT_OK


# ── Degree gates ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Bachelor's degree required.", Gate.HARD_DEGREE),
        ("Bachelor's required.", Gate.HARD_DEGREE),
        ("Requires a Bachelor's in a technical field.", Gate.HARD_DEGREE),
        ("A 4-year degree required.", Gate.HARD_DEGREE),
        ("PhD required in machine learning.", Gate.ADVANCED),
        ("Master's degree required.", Gate.ADVANCED),
        ("Bachelor's degree preferred.", Gate.SOFT_DEGREE),
        ("A degree is a plus.", Gate.SOFT_DEGREE),
        ("Strong Python and SQL skills. Ship good software.", Gate.OPEN),
    ],
)
def test_degree_gates(text, expected):
    assert classify(text).gate is expected


def test_advanced_outranks_bachelors():
    text = "Bachelor's degree required; PhD required for the research track."
    assert classify(text).gate is Gate.ADVANCED


# ── Evidence ───────────────────────────────────────────────────────────────────

def test_evidence_is_captured():
    """The queue shows why a call was made, so evidence must be non-empty."""
    verdict = classify("Must be currently enrolled in a degree program.")
    assert verdict.evidence
    assert "enrolled" in verdict.evidence[0].lower()


def test_empty_description_is_open_not_blocked():
    """Missing text must not silently hide a job."""
    assert classify("").gate is Gate.OPEN
    assert classify("", "Data Engineer").gate is Gate.OPEN


def test_worth_applying_partition():
    reachable = {Gate.OPEN, Gate.EQUIVALENT_OK, Gate.SOFT_DEGREE}
    for gate in Gate:
        assert gate.worth_applying == (gate in reachable)


# ── Years of experience ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("2+ years building distributed services", 2),
        ("3-5 years of experience", 3),
        ("at least 4 years", 4),
        ("minimum of 6 years in analytics", 6),
        ("3 years experience with Python", 3),
        ("5+ years Python, 2+ years SQL", 2),  # minimum is the floor to clear
        ("No numeric requirement here.", None),
        ("", None),
    ],
)
def test_min_years(text, expected):
    assert min_years_required(text) == expected


def test_absurd_year_counts_ignored():
    assert min_years_required("Founded 1998 years of heritage") is None


# ── Scoring integration ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Staff Data Scientist",
        "Engineering Manager",
        "Director of Analytics",
        "Head of Investor Relations",
        "Principal Architect",
    ],
)
def test_seniority_knocked_out(title):
    score, _, _ = score_posting(title, "Great role.")
    assert score.rejected


@pytest.mark.parametrize(
    "title",
    [
        "Commercial Counsel",
        "Compensation Analyst",
        "Account Executive, Majors",
        "Didn't See What You Are Looking For?",
    ],
)
def test_non_technical_knocked_out(title):
    """These are exactly the rows that polluted the old scraper's output."""
    score, _, _ = score_posting(title, "Join our team.")
    assert score.rejected


def test_equivalent_experience_outranks_degree_required():
    """The ranking that makes the queue useful for a non-graduate."""
    open_score, _, _ = score_posting(
        "Software Engineer",
        "BS or equivalent practical experience. Python.",
        "Los Angeles, CA",
    )
    gated_score, _, _ = score_posting(
        "Software Engineer",
        "Bachelor's degree required. Python.",
        "Los Angeles, CA",
    )
    assert open_score.total > gated_score.total


def test_score_carries_reasons():
    score, _, _ = score_posting(
        "Data Engineer", "Python and SQL. No degree required.", "Remote"
    )
    assert not score.rejected
    assert score.reasons
    assert any("gate:" in r for r in score.reasons)


def test_no_role_match_is_rejected():
    score, _, _ = score_posting("Warehouse Associate", "Lift boxes.")
    assert score.rejected


# ── Regressions found against 12k real postings ────────────────────────────────

def test_curly_apostrophe_does_not_defeat_matching():
    """Real postings write "Bachelor’s" with U+2019, not a straight quote.

    This silently misclassified 204 of 400 sampled postings as having no degree
    language at all.
    """
    curly = "This position requires a Bachelor’s degree in a related field."
    straight = curly.replace("’", "'")
    assert classify(curly).gate is classify(straight).gate
    assert classify(curly).gate is not Gate.OPEN


def test_curly_apostrophe_in_hard_requirement():
    assert classify("Bachelor’s degree required.").gate is Gate.HARD_DEGREE


def test_en_dash_year_range():
    assert min_years_required("3–5 years of experience") == 3


def test_reversed_equivalent_phrasing():
    """"either equivalent practical experience or a Bachelor's degree"."""
    text = (
        "This position requires either equivalent practical experience or a "
        "Bachelor’s degree in a related field."
    )
    assert classify(text).gate is Gate.EQUIVALENT_OK


def test_bare_degree_in_field_is_not_open():
    text = "Degree in Data Science, Computer Science, Economics, or a related field."
    assert classify(text).gate is Gate.SOFT_DEGREE


@pytest.mark.parametrize(
    "text",
    [
        "Ability to thrive with a high degree of autonomy and ownership.",
        "Tune and polish features to a high degree of excellence.",
        "Work with a high degree of independence.",
    ],
)
def test_high_degree_of_autonomy_is_not_degree_language(text):
    """The most common false positive in real postings — "degree" as a noun
    of quantity, not education."""
    assert classify(text).gate is Gate.OPEN


def test_advanced_degree_as_preference_is_soft():
    text = "A relevant advanced degree (Masters or PhD) in Machine Learning."
    assert classify(text).gate is Gate.SOFT_DEGREE


def test_generic_engineer_titles_are_not_dropped():
    """These scored zero and vanished before "engineer" was a role term."""
    for title in [
        "Network Security Engineer",
        "Cloud Operations Engineer",
        "Deployment Engineer",
    ]:
        score, _, _ = score_posting(title, "Build and operate systems. Python.")
        assert not score.rejected, title


@pytest.mark.parametrize(
    "title",
    ["Solutions Engineer, UK", "Sales Engineer", "Retail Key Holder", "Barista"],
)
def test_customer_facing_engineer_titles_still_knocked_out(title):
    score, _, _ = score_posting(title, "Work with customers.")
    assert score.rejected, title


def test_entity_encoded_html_is_stripped():
    """Greenhouse returns entity-encoded markup; tags must not survive as text."""
    from jobbot.boards import html_to_text

    raw = "&lt;p&gt;Build things.&lt;/p&gt;&lt;li&gt;Bachelor&#8217;s degree required&lt;/li&gt;"
    text = html_to_text(raw)
    assert "<p>" not in text and "&lt;" not in text
    assert "Build things." in text
    assert classify(text).gate is Gate.HARD_DEGREE


def test_plain_html_still_stripped():
    from jobbot.boards import html_to_text

    assert html_to_text("<p>Hello</p><li>World</li>").split() == ["Hello", "World"]


@pytest.mark.parametrize(
    "location",
    ["Remote - Spain", "Remote - Ireland", "London", "Bangalore, India", "Toronto"],
)
def test_non_us_locations_penalized(location):
    """"Remote - Spain" contains "remote" and used to collect the full bonus."""
    score, _, _ = score_posting("Software Engineer", "Python.", location)
    us_score, _, _ = score_posting("Software Engineer", "Python.", "Remote - US")
    assert score.total < us_score.total, location


def test_multi_location_including_us_is_kept():
    """A posting listing London and San Francisco is still reachable."""
    score, _, _ = score_posting(
        "Software Engineer", "Python.", "London, UK; San Francisco, CA, US"
    )
    assert not score.rejected
    assert not any("non-US" in r for r in score.reasons)


def test_los_angeles_outranks_generic_remote():
    la, _, _ = score_posting("Software Engineer", "Python.", "Los Angeles, CA")
    remote, _, _ = score_posting("Software Engineer", "Python.", "Remote - US")
    assert la.total > remote.total


def test_high_experience_ask_is_penalized():
    low, _, _ = score_posting("Software Engineer", "2+ years experience. Python.")
    high, _, _ = score_posting("Software Engineer", "10+ years experience. Python.")
    assert low.total > high.total


# ── Deadline knockout ───────────────────────────────────────────────────────────
# The Braven board writes "Application deadline: YYYY-MM-DD" into a posting's
# own description text. The MLSC Data Science Internship stayed in the queue at
# score 45 with a cover letter written for it a full day after that exact date
# had passed — nothing had ever read the line back out.

def test_a_passed_deadline_is_detected():
    text = "Application deadline: 2026-08-21"
    assert deadline_passed(text, today=date(2026, 8, 22)) is True


def test_a_future_deadline_is_not_a_knockout():
    text = "Application deadline: 2026-09-18"
    assert deadline_passed(text, today=date(2026, 8, 22)) is False


def test_the_deadline_itself_is_still_open():
    """The last day should still be applyable, not knocked out at 00:00."""
    text = "Application deadline: 2026-08-22"
    assert deadline_passed(text, today=date(2026, 8, 22)) is False


def test_no_deadline_line_is_never_a_knockout():
    assert deadline_passed("No degree required. Fully remote.") is False
    assert deadline_passed("") is False


def test_score_posting_knocks_out_an_expired_deadline():
    score, _, _ = score_posting(
        "Data Science Intern",
        "Application deadline: 2020-01-01. No degree required.",
    )
    assert score.rejected
    assert "deadline" in score.knockout


# -- Enrollment gates found in real postings, 2026-09-07 -----------------------

def test_pursuing_a_phd_is_an_enrollment_gate():
    """Databricks' "PhD GenAI Research Scientist Intern" said "Pursuing a PhD
    in computer science" and was classified OPEN — the alternation listed
    bachelor, master and BS/MS, but not the one degree that rules him out
    hardest."""
    verdict = classify(
        "Pursuing a PhD in computer science or related fields.",
        "PhD GenAI Research Scientist Intern",
    )
    assert verdict.gate is Gate.ENROLLMENT


def test_a_qualified_graduation_window_is_an_enrollment_gate():
    """Samsara: "expected graduation no earlier than Summer 2028". The pattern
    allowed a single word between the phrase and the year, so any qualifier at
    all slipped past it."""
    verdict = classify(
        "BS in Computer Science, with expected graduation no earlier than "
        "Summer 2028. This internship is hybrid.",
        "Software Engineering Internship",
    )
    assert verdict.gate is Gate.ENROLLMENT


def test_a_finished_degree_is_still_not_an_enrollment_gate():
    """The widened patterns must not swallow ordinary degree language, which
    is a different verdict with a different consequence."""
    verdict = classify(
        "Bachelor's degree in Computer Science or equivalent practical "
        "experience.",
        "Software Engineer",
    )
    assert verdict.gate is Gate.EQUIVALENT_OK

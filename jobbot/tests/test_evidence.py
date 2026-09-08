"""Measured skill evidence, and the one direction it may answer in."""

from __future__ import annotations

import pytest

from jobbot import evidence


def build(**lines) -> evidence.Evidence:
    return evidence.Evidence(
        scanned_at="2026-08-21T00:00:00+00:00",
        skills={k: evidence.Skill(name=k, lines=v, files=max(1, v // 100))
                for k, v in lines.items()},
    )


SUPABASE_OPTIONS = [
    "Neither - I haven't used Go or TypeScript professionally.",
    "One only - I'm proficient in one but have little or no experience with the other.",
    "Both, developing - I've used both professionally but wouldn't call myself strong.",
    "Both, strong - I'm confident and productive in both Go and TypeScript.",
]


# ── The asymmetry ──────────────────────────────────────────────────────────────

def test_zero_lines_answers_the_question():
    """"I have never used this" is a fact a line count can establish."""
    ev = build(Python=375000)
    answer, note = evidence.answer_level(
        ev, "Rate your proficiency working with Go and TypeScript:", SUPABASE_OPTIONS
    )
    assert answer == SUPABASE_OPTIONS[0]
    assert "no measured usage" in note


def test_experience_never_claims_a_level():
    """375k lines of Python is not proof of skill, so it stays your call."""
    ev = build(Python=375000)
    answer, note = evidence.answer_level(
        ev, "Rate your proficiency with Python", ["No experience", "Expert"]
    )
    assert answer is None
    assert "375,227" in note or "375,000" in note
    assert "you choose" in note


def test_mixed_evidence_is_not_answered():
    """Zero Go but plenty of Python — "Neither" would be a lie."""
    ev = build(Python=375000, Go=0)
    answer, _ = evidence.answer_level(
        ev, "Rate your proficiency with Go and Python", SUPABASE_OPTIONS
    )
    assert answer is None


def test_zero_lines_but_no_none_option_is_left_alone():
    ev = build(Python=1)
    answer, note = evidence.answer_level(
        ev, "Rate your proficiency with Go", ["Intermediate", "Advanced", "Expert"]
    )
    assert answer is None
    assert "no 'never used' option" in note


# ── Naming languages ───────────────────────────────────────────────────────────

def test_single_letter_languages_need_word_boundaries():
    """"C" and "R" are languages and also letters.

    A substring test found both inside "Rate your proficiency" and reported
    thousands of lines of C as evidence about Go.
    """
    named = evidence.languages_in("Rate your proficiency working with Go and TypeScript:")
    assert named == ["TypeScript", "Go"] or set(named) == {"TypeScript", "Go"}
    assert "C" not in named
    assert "R" not in named


def test_a_real_mention_of_c_is_still_found():
    assert "C" in evidence.languages_in("Experience with C and C++ required")
    assert "C++" in evidence.languages_in("Experience with C and C++ required")


def test_lowercase_go_in_prose_is_not_the_language():
    assert "Go" not in evidence.languages_in("Ready to go fast and ship?")


def test_a_question_naming_nothing_known_returns_nothing():
    assert evidence.languages_in("Why are you interested in working here?") == []


def test_answer_level_ignores_questions_about_no_known_skill():
    answer, note = evidence.answer_level(
        build(Python=1000), "Why do you want to join Figma?", ["a", "b"]
    )
    assert answer is None
    assert note == ""


# ── Levels and overlap ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "lines,level",
    [(0, "none"), (1, "trace"), (499, "trace"), (500, "working"),
     (5000, "strong"), (25000, "primary")],
)
def test_levels_are_thresholds_not_opinions(lines, level):
    assert evidence.Skill(name="X", lines=lines).level == level


def test_stack_overlap_separates_what_you_have_written():
    ev = build(Python=375000, SQL=2000, Go=0, TypeScript=0)
    have, missing = evidence.stack_overlap(
        ev, "Engineer strong in Go, TypeScript and Python for auth infrastructure."
    )
    assert have == ["Python"]
    assert missing == ["Go", "TypeScript"]


def test_unknown_skill_reads_as_zero_not_as_an_error():
    assert build().get("Haskell").lines == 0
    assert build().get("Haskell").level == "none"

"""Tests for custom question answers and weighted EEO randomization.

The randomization exists because more than one answer is genuinely true of the
user. It must therefore only ever choose *between answers they have declared
true* — never widen the pool, and never reach a field that certifies anything.
"""

from __future__ import annotations

import collections

import pytest

from jobbot import autofill
from jobbot.profile import Profile


@pytest.fixture
def profile():
    return Profile(
        first_name="Quintin",
        eeo={"gender": "Male", "hispanic_ethnicity": "No", "veteran_status": "No"},
        eeo_random={
            "race": [["Black or African American", 60], ["Two or more races", 40]],
            "disability": [["Yes", 50], ["decline", 50]],
        },
        custom_answers={
            "preferred name": "Quintin",
            "pronoun": "He/Him",
            "sponsorship": "No",
            "state or canadian province|which u.s. state": "California",
            "how did you first learn|how did you hear": "Other",
            "previously been employed": "No",
        },
    )


# ── Custom answers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "label,expected",
    [
        ("Preferred Name", "Quintin"),
        ("Pronouns", "He/Him"),
        ("Do you require immigration sponsorship to work for Affirm?", "No"),
        ("Will you require immigration sponsorship at any point in the future?", "No"),
        ("Which U.S. State or Canadian Province do you reside in?", "California"),
        ("How did you first learn about Affirm as an employer?", "Other"),
        ("Have you previously been employed at Affirm?", "No"),
    ],
)
def test_custom_questions_answered(profile, label, expected):
    assert autofill.custom_answer(profile, label) == expected


@pytest.mark.parametrize(
    "label",
    ["Current Company", "Name Pronunciation", "Twitter", "Other Links",
     "Why do you want to work here?"],
)
def test_unconfigured_questions_left_alone(profile, label):
    assert autofill.custom_answer(profile, label) is None


@pytest.mark.parametrize(
    "label",
    ["I agree to the terms", "Signature", "Social Security Number",
     "Date of Birth", "Desired salary", "Acknowledge the Privacy Policy",
     "I affirm this reflects my own work and experience"],
)
def test_custom_answers_cannot_reach_never_fill(label):
    """A pattern that would otherwise match must not unlock these.

    Consent is included: it became fillable via `profile.agreements`, but a
    catch-all custom pattern still must not be able to agree to anything.
    """
    sneaky = Profile(custom_answers={".*": "yes", "signature": "Quintin Fletcher"})
    assert autofill.custom_answer(sneaky, label) is None


def test_blank_custom_answers_are_skipped(profile):
    profile.custom_answers["current company"] = ""
    assert autofill.custom_answer(profile, "Current Company") is None


def test_malformed_pattern_falls_back_to_substring():
    p = Profile(custom_answers={"what is your ([unclosed": "x", "pronoun": "He/Him"})
    assert autofill.custom_answer(p, "Pronouns") == "He/Him"


# ── Weighted randomization ─────────────────────────────────────────────────────

def test_random_race_only_picks_declared_options(profile):
    seen = {autofill.eeo_answer(profile, "Race")[1] for _ in range(200)}
    assert seen <= {"Black or African American", "Two or more races"}
    assert len(seen) == 2  # both actually occur


def test_random_weighting_is_roughly_honoured(profile):
    counts = collections.Counter(
        autofill.eeo_answer(profile, "Race")[1] for _ in range(3000)
    )
    share = counts["Black or African American"] / 3000
    assert 0.53 < share < 0.67, share  # 60/40, allowing for noise


def test_disability_pool_never_contains_a_denial(profile):
    """The user has ADHD, so "No" would be false on a form they certify.

    The pool is {Yes, decline} — both honest, both preserving the choice.
    """
    seen = {autofill.eeo_answer(profile, "disability_status")[1] for _ in range(200)}
    assert seen <= {"Yes", "decline"}
    assert "No" not in seen


def test_random_overrides_a_static_answer():
    p = Profile(
        eeo={"race": "Two or more races"},
        eeo_random={"race": [["Black or African American", 100]]},
    )
    assert autofill.eeo_answer(p, "Race")[1] == "Black or African American"


def test_zero_weight_options_never_chosen():
    p = Profile(eeo_random={"race": [["Black or African American", 1], ["Other", 0]]})
    seen = {autofill.eeo_answer(p, "Race")[1] for _ in range(100)}
    assert seen == {"Black or African American"}


def test_empty_random_list_falls_back_to_blank():
    p = Profile(eeo_random={"race": []}, eeo={"race": ""})
    assert autofill.eeo_answer(p, "Race")[1] is None


def test_randomization_cannot_touch_non_eeo_fields(profile):
    """eeo_random keys only apply to recognized EEO questions."""
    p = Profile(eeo_random={"salary": [["100000", 1]]})
    assert autofill.eeo_answer(p, "Desired salary")[0] is None


# ── Option wording for the randomized answers ──────────────────────────────────

@pytest.mark.parametrize(
    "option,answer",
    [
        ("Black or African American", "Black or African American"),
        ("Black or African American (Not Hispanic or Latino)", "Black or African American"),
        ("Two or More Races", "Two or more races"),
        ("Two or more races (Not Hispanic or Latino)", "Two or more races"),
        ("I don't want to answer", "decline"),
        ("I do not want to answer", "decline"),
        ("Yes, I have a disability, or have had one in the past", "Yes"),
    ],
)
def test_randomized_answers_match_form_wording(option, answer):
    assert autofill._eeo_option_matches(option, answer), option


def test_disability_yes_does_not_match_the_no_option():
    """The single worst failure available here."""
    no_option = "No, I do not have a disability and have not had one in the past"
    assert not autofill._eeo_option_matches(no_option, "Yes")
    assert autofill._eeo_option_matches(no_option, "No")

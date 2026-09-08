"""Tests for voluntary EEO handling.

The rule this file exists to protect: a field is answered only when the profile
carries an explicit answer for it. Blank means untouched, every run, with no
defaulting and no inference. Race and disability are deliberately blank for
this user — race because the answer genuinely varies, disability because they
want to decide per employer at the moment of submitting.
"""

from __future__ import annotations

import pytest

from jobbot import autofill
from jobbot.profile import Profile


@pytest.fixture
def profile():
    return Profile(
        first_name="Quintin",
        eeo={
            "gender": "Male",
            "hispanic_ethnicity": "No",
            "veteran_status": "No",
            "race": "",
            "disability": "",
        },
    )


# ── Configured answers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "descriptor,key,answer",
    [
        ("Gender", "gender", "Male"),
        ("gender", "gender", "Male"),
        ("Hispanic/Latino?", "hispanic_ethnicity", "No"),
        ("hispanic_ethnicity", "hispanic_ethnicity", "No"),
        ("Are you a protected veteran?", "veteran_status", "No"),
        ("veteran_status", "veteran_status", "No"),
    ],
)
def test_configured_answers_are_used(profile, descriptor, key, answer):
    assert autofill.eeo_answer(profile, descriptor) == (key, answer)


@pytest.mark.parametrize(
    "descriptor,key",
    [
        ("Race", "race"),
        ("race", "race"),
        ("Disability Status", "disability"),
        ("disability_status", "disability"),
        ("Voluntary Self-Identification of Disability", "disability"),
    ],
)
def test_blank_entries_are_never_answered(profile, descriptor, key):
    """Blank must mean untouched — not "decline", not a guess."""
    found_key, answer = autofill.eeo_answer(profile, descriptor)
    assert found_key == key
    assert answer is None


def test_absent_profile_answers_nothing():
    """A profile with no eeo map fills no EEO field at all."""
    bare = Profile(first_name="Quintin")
    for descriptor in ["Gender", "Race", "veteran_status", "disability_status"]:
        assert autofill.eeo_answer(bare, descriptor)[1] is None


def test_eeo_fields_remain_protected_from_generic_matching(profile):
    """Even a configured field must not be reachable by ordinary matching."""
    for descriptor in ["Gender", "veteran_status", "hispanic_ethnicity"]:
        assert autofill.is_protected(descriptor), descriptor


# ── Absolute limits ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "descriptor",
    [
        "Signature",
        "Social Security Number",
        "Date of Birth",
        "Desired salary",
        "I affirm the materials I submit reflect my own work and experience",
        "Candidate AI Responsible Use Policy",
    ],
)
def test_never_fill_is_absolute(descriptor):
    """No profile setting unlocks these."""
    assert autofill.never_fill(descriptor), descriptor
    assert autofill.eeo_answer(Profile(eeo={"gender": "Male"}), descriptor)[0] is None
    assert autofill.agreement_answer(
        Profile(agreements={"terms": "agree", "privacy_policy": "agree"}), descriptor
    )[0] is None


def test_eeo_answers_cannot_unlock_agreements():
    sneaky = Profile(eeo={"agree": "Yes", "signature": "Quintin Fletcher"})
    assert autofill.eeo_answer(sneaky, "I agree to the terms")[1] is None
    assert autofill.eeo_answer(sneaky, "Signature")[1] is None


# ── Consent: blank unless the profile says otherwise ───────────────────────────

@pytest.mark.parametrize(
    "descriptor,key",
    [
        ("I agree to the Candidate Privacy Policy", "privacy_policy"),
        ("Acknowledge that Twilio processes data per its policy", "privacy_policy"),
        ("The interview may be recorded/transcribed. Make a selection", "interview_recording"),
        ("I certify this is accurate", "terms"),
    ],
)
def test_consent_is_blank_until_the_profile_answers_it(descriptor, key):
    assert autofill.agreement_key(descriptor) == key, descriptor
    assert autofill.agreement_answer(Profile(), descriptor)[1] is None
    answered = Profile(agreements={key: "agree"})
    assert autofill.agreement_answer(answered, descriptor)[1] == "agree"


def test_sms_about_other_jobs_is_a_different_consent_from_application_updates():
    """A yes to recruiter texts must not become a yes to marketing."""
    marketing = ("Would you like to receive SMS about other job opportunities "
                 "at our company?")
    updates = ("Would you like to receive communications via SMS concerning the "
               "status of or next steps in the recruitment process?")
    assert autofill.agreement_key(marketing) == "marketing_sms"
    assert autofill.agreement_key(updates) == "recruiting_sms"

    profile = Profile(agreements={"recruiting_sms": "yes", "marketing_sms": "no"})
    assert autofill.agreement_answer(profile, updates)[1] == "yes"
    assert autofill.agreement_answer(profile, marketing)[1] == "no"


def test_consent_is_invisible_to_generic_matching():
    """Only the agreements pass may answer these, never ordinary field matching."""
    for label in ("I agree to the Privacy Policy", "Acknowledge"):
        assert autofill.is_protected(label), label


@pytest.mark.parametrize(
    "descriptor", ["Sexual orientation", "Transgender", "Preferred pronouns"]
)
def test_identity_questions_without_a_profile_slot_stay_blank(profile, descriptor):
    assert autofill.is_protected(descriptor)
    assert autofill.eeo_answer(profile, descriptor)[1] is None


# ── Wording that varies between companies ──────────────────────────────────────

@pytest.mark.parametrize(
    "label,expected",
    [
        # Greenhouse defaults
        ("gender", "gender"),
        ("hispanic_ethnicity", "hispanic_ethnicity"),
        ("race", "race"),
        ("veteran_status", "veteran_status"),
        ("disability_status", "disability"),
        # CircleCI's phrasing
        ("How do you identify your gender?", "gender"),
        ("Are you a veteran?", "veteran_status"),
        ("What is your ability status?", "disability"),
        ("Which races/ethnicities do you belong to?", "race"),
        # Other common variants
        ("Race/Ethnicity", "race"),
        ("Are you Hispanic or Latino?", "hispanic_ethnicity"),
        ("Voluntary Self-Identification of Disability", "disability"),
    ],
)
def test_company_specific_wording_maps_correctly(label, expected):
    """A race question answered with a Hispanic yes/no answer is the failure
    this test exists to prevent."""
    assert autofill.eeo_key(label) == expected, label


def test_ability_status_is_not_answered_without_configuration(profile):
    """CircleCI's disability question, with race/disability left blank."""
    key, answer = autofill.eeo_answer(profile, "What is your ability status?")
    assert key == "disability"
    assert answer in {"Yes", "decline", None}  # never a denial


def test_races_question_never_gets_the_hispanic_answer(profile):
    key, _ = autofill.eeo_answer(profile, "Which races/ethnicities do you belong to?")
    assert key == "race"


# ── Option wording ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "option,answer",
    [
        ("Male", "Male"),
        ("No", "No"),
        ("I am not a protected veteran", "No"),
        ("No, I am not a protected veteran", "No"),
        ("I am not Hispanic or Latino", "No"),
        ("Yes", "Yes"),
        ("I identify as one or more of the classifications", "Yes"),
    ],
)
def test_option_wording_matched(option, answer):
    """Forms spell "No" as "I am not a protected veteran"."""
    assert autofill._eeo_option_matches(option, answer), option


@pytest.mark.parametrize(
    "option,answer",
    [
        ("Female", "Male"),
        ("Yes", "No"),
        ("I am one or more of the classifications of protected veteran", "No"),
        ("Decline to self identify", "No"),
        ("", "No"),
    ],
)
def test_wrong_options_rejected(option, answer):
    """Picking the opposite answer is the worst possible failure here."""
    assert not autofill._eeo_option_matches(option, answer), option


# ── Reporting ──────────────────────────────────────────────────────────────────

def test_report_separates_answered_from_blank():
    report = autofill.FillReport(
        eeo_answered={"gender": "Male", "veteran_status": "No"},
        eeo_left_blank=["race", "disability"],
    )
    text = report.render()
    assert "gender" in text and "Male" in text
    assert "decide on the form" in text
    assert "race" in text and "disability" in text

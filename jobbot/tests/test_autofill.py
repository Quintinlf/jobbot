"""Tests for form autofill.

The protective behaviour matters more than the filling here. A field this
module declines to touch is a field the user answers themselves; a field it
wrongly fills is personal data submitted under their name without them
choosing it.
"""

from __future__ import annotations

import pytest

from jobbot import autofill
from jobbot.profile import Profile


@pytest.fixture
def profile():
    return Profile(
        first_name="Quintin",
        last_name="Fletcher",
        email="q@example.com",
        phone="(323) 555-0100",
        city="Los Angeles",
        state="CA",
        linkedin_url="https://linkedin.com/in/example",
        github_url="https://github.com/example",
    )


# ── Never touch ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "label",
    [
        "Gender",
        "Race / Ethnicity",
        "Are you a protected veteran?",
        "Voluntary Self-Identification of Disability",
        "Disability Status",
        "Hispanic or Latino",
        "Sexual orientation",
        "Preferred pronouns",
        "Date of Birth",
        "Social Security Number",
        "Desired salary",
        "Compensation expectations",
        "I agree to the terms and conditions",
        "I consent to the privacy policy",
        "Please acknowledge you have read this",
        "Signature",
        "I certify the above is true",
    ],
)
def test_protected_fields_recognized(label):
    assert autofill.is_protected(label), label


@pytest.mark.parametrize(
    "field_name",
    [
        "veteran_status",
        "disability_status",
        "hispanic_ethnicity",
        "gender_identity",
        "self-identify",
        "date_of_birth",
        "veteran-status",
    ],
)
def test_snake_case_field_names_are_protected(field_name):
    r"""Real forms name fields "veteran_status". An underscore is a word
    character, so `\bveteran\b` never matched it and the field was reported as
    ordinary rather than protected."""
    assert autofill.is_protected(field_name), field_name


def test_disability_question_is_protected():
    """The disclosure decision stays with the user, at every layer."""
    assert autofill.is_protected("Voluntary Self-Identification of Disability")
    assert autofill.is_protected("", "disability_status", "", "")
    assert autofill.is_protected("Do you have a disability?")


@pytest.mark.parametrize(
    "label",
    ["First Name", "Email", "Phone", "LinkedIn Profile", "GitHub", "City", "Website"],
)
def test_ordinary_fields_not_protected(label):
    assert not autofill.is_protected(label), label


def test_agreement_checkbox_language_is_protected():
    for text in [
        "I agree to receive communications",
        "By submitting I acknowledge",
        "I certify that all information is accurate",
    ]:
        assert autofill.is_protected(text), text


# ── Field matching ─────────────────────────────────────────────────────────────

def test_specs_map_to_common_labels(profile):
    specs = {s.key: s for s in autofill.build_specs(profile)}

    assert specs["first_name"].matches("First Name")
    assert specs["first_name"].matches("", "first_name")
    assert specs["last_name"].matches("Last Name")
    assert specs["last_name"].matches("Family Name")
    assert specs["email"].matches("Email")
    assert specs["email"].matches("E-mail Address")
    assert specs["phone"].matches("Phone")
    assert specs["linkedin"].matches("LinkedIn Profile URL")
    assert specs["github"].matches("GitHub")


def test_first_name_does_not_match_last_name_field(profile):
    specs = {s.key: s for s in autofill.build_specs(profile)}
    assert not specs["first_name"].matches("Last Name")
    assert not specs["last_name"].matches("First Name")


def test_specs_carry_real_values(profile):
    specs = {s.key: s for s in autofill.build_specs(profile)}
    assert specs["first_name"].value == "Quintin"
    assert specs["location"].value == "Los Angeles, CA"


def test_empty_profile_values_are_not_offered():
    """A blank profile field must never be filled in as an empty string."""
    thin = Profile(first_name="Quintin", email="q@example.com")
    specs = {s.key: s for s in autofill.build_specs(thin)}
    assert specs["phone"].value == ""
    assert specs["linkedin"].value == ""


# ── Reporting ──────────────────────────────────────────────────────────────────

def test_report_states_what_was_left_alone():
    report = autofill.FillReport(
        filled={"first_name": "Quintin"},
        skipped_protected=["Voluntary Self-Identification of Disability"],
        unmatched=["Why do you want to work here?"],
    )
    text = report.render()
    assert "Quintin" in text
    assert "Self-Identification" in text
    assert "Why do you want to work here?" in text
    assert "NOT attached" in text  # resume_attached defaults False


def test_report_flags_missing_resume():
    assert "NOT attached" in autofill.FillReport().render()
    assert "attached" in autofill.FillReport(resume_attached=True).render()


# ── The submit boundary ────────────────────────────────────────────────────────

def test_module_never_clicks_submit():
    """A structural guarantee, not a behavioural one: there is no submit call."""
    import inspect

    lowered = inspect.getsource(autofill).lower()
    for banned in ["submit()", '.press("enter")', "click_submit", "form.submit"]:
        assert banned not in lowered, banned


def test_only_consent_and_group_passes_check_a_checkbox():
    """Ticking is confined to the two passes whose answers come from the profile.

    This replaced a blanket ban. Forms that will not submit without a consent
    box needed it fillable, but it must not become something any pass can do
    incidentally — nor may `set_checked`, which would also be able to UNtick
    something you had already answered by hand.
    """
    import inspect

    assert "set_checked" not in inspect.getsource(autofill)

    allowed = {"_fill_checkbox_groups", "_fill_agreements", "_fill_radio_groups"}
    for name in dir(autofill):
        fn = getattr(autofill, name)
        if not callable(fn) or getattr(fn, "__module__", "") != autofill.__name__:
            continue
        try:
            source = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if ".check(" in source:
            assert name in allowed, f"{name} ticks a checkbox"


def test_radio_groups_still_leave_eeo_blank_by_default(profile):
    """The radio pass may tick, but not decide. EEO stays opt-in.

    Ashby renders gender/race/veteran as radios rather than selects, so the
    pass that answers them has to be able to tick. What must not change is
    where the answer comes from.
    """
    import inspect

    source = inspect.getsource(autofill._fill_radio_groups)
    assert "eeo_answer" in source, "EEO radios must route through the profile"
    assert "eeo_left_blank" in source, "an unanswered EEO radio must be reported"
    assert "custom_answer" in source


def test_an_already_ticked_box_is_never_touched(profile):
    """Whatever you answered by hand survives a re-fill."""
    import inspect

    for name in ("_fill_checkbox_groups", "_fill_agreements"):
        source = inspect.getsource(getattr(autofill, name))
        assert "is_checked()" in source, name


# ── Dropdowns ──────────────────────────────────────────────────────────────────

def test_only_unambiguous_dropdowns_are_answered(profile):
    """Greenhouse renders 14 comboboxes per form, most of them demographic."""
    patterns = [p for p, _ in autofill._unambiguous_choices(profile)]
    assert len(patterns) == 3  # country, work authorization, sponsorship

    import re as _re
    for label in ["Gender", "Disability Status", "Veteran Status", "Race"]:
        assert not any(_re.search(p, label, _re.I) for p in patterns), label


def test_country_and_authorization_answers(profile):
    choices = dict(autofill._unambiguous_choices(profile))
    assert choices[r"\bcountry\b"] == "United States"

    authorized = [v for p, v in autofill._unambiguous_choices(profile)
                  if "authorized" in p]
    assert authorized == ["Yes"]

    needs_sponsor = Profile(requires_sponsorship=True, work_authorized=False)
    sponsor = [v for p, v in autofill._unambiguous_choices(needs_sponsor)
               if "sponsorship" in p]
    assert sponsor == ["Yes"]

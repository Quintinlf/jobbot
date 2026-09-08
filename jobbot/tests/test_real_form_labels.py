"""The exact question wording seen on real postings.

Every label below was copied off a form that jobbot had already filled and
left blank or wrong: Figma, Calendly, Reddit, Twilio, Thumbtack. Patterns
written from imagination match imaginary forms, so these are pinned verbatim.

Loads the real profile when it is present, because the point is to check the
answers actually configured, not a fixture that agrees with the patterns.
"""

from __future__ import annotations

import pytest

from jobbot import autofill, config
from jobbot import profile as profile_mod
from jobbot.profile import Profile


@pytest.fixture(scope="module")
def prof() -> Profile:
    if config.PROFILE_PATH.exists():
        loaded = profile_mod.load()
        if loaded.custom_answers:
            return loaded
    pytest.skip("no real profile on this machine")


# ── Questions that should now get an answer ────────────────────────────────────

@pytest.mark.parametrize(
    "label,expected",
    [
        # Figma
        ("From where do you intend to work?", None),          # handled by a FieldSpec
        ("Have you ever worked for Figma before, as an employee or a "
         "contractor/consultant?", "No"),
        # Calendly
        ("Calendly is registered as an employer in many, but not all, states. "
         "Please select the state where you will reside and work.", "California"),
        ("Zip Code of Residence:", None),                     # handled by a FieldSpec
        # Reddit
        ("Please provide the name of your current (or most recent) company",
         "Dynamic Active"),
        ("How did you hear about this job?", "Other"),
        # Twilio
        ("How did you hear about Twilio?", "Other"),
        ("Please indicate whether you are either a citizen or resident of any of "
         "the following countries: Cuba, Iran, North Korea, Syria or Crimea "
         "Region of Ukraine.", "No"),
        # Skydio
        ("Prior US Government Employment?", "No"),
        # Instacart — left blank 2026-08-27 despite an existing "previously
        # worked at/for" pattern: "previously, worked for" (comma, "for" not
        # "at") matched none of the old alternatives.
        ("Are you currently, or have you previously, worked for Instacart?", "No"),
        ("Are you legally entitled to work in Canada?", "No"),
        # Also left blank 2026-08-27: "state or province...currently live in"
        # matched none of the old state-question wordings either.
        ("Which state or province do you currently live in?", "California"),
        # Pinterest — "first hear" broke the old "how did you hear" pattern.
        ("How did you first hear about this opportunity?", "Other"),
        # "Current/Previous Employer" (slash, no space) broke the old
        # contiguous "current employer" phrase.
        ("Work Experience: Current/Previous Employer", "Dynamic Active"),
    ],
)
def test_custom_questions_are_answered(prof, label, expected):
    answer = autofill.custom_answer(prof, label)
    if expected is None:
        return  # covered by test_field_specs_cover_the_rest
    assert answer == expected, f"{label!r} -> {answer!r}"


@pytest.mark.parametrize(
    "label,key,expected",
    [
        ("Zip Code of Residence:", "postal_code", "90036"),
        ("From where do you intend to work? Please list city and state.",
         "work_location", "Los Angeles, CA"),
    ],
)
def test_field_specs_cover_the_rest(prof, label, key, expected):
    specs = autofill.build_specs(prof)
    match = next((s for s in specs if s.matches(label)), None)
    assert match is not None, f"nothing matches {label!r}"
    assert match.key == key
    assert match.value == expected


def test_work_location_is_not_swallowed_by_the_generic_location_spec(prof):
    """`location` matches the substring "work location" too; order decides."""
    specs = autofill.build_specs(prof)
    first = next(s for s in specs if s.matches("From where do you intend to work?"))
    assert first.key == "work_location"


# ── Consent ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "label,key",
    [
        ("If you are invited to an interview, the interview may be "
         "recorded/transcribed. Please make a selection:", "interview_recording"),
        ("In addition to email and phone communications, would you also like to "
         "receive communications via SMS from Calendly concerning the status of "
         "or next steps in the recruitment process?", "recruiting_sms"),
        ('By selecting "I agree," I understand that the information I have '
         "provided as part of this job application will be processed in "
         "accordance with Reddit's Candidate Privacy Policy.", "privacy_policy"),
        ('By clicking the "Acknowledge" button, you acknowledge that Twilio '
         "processes data in accordance with the Twilio Applicant Privacy Policy.",
         "privacy_policy"),
    ],
)
def test_consent_questions_route_to_the_right_key(prof, label, key):
    assert autofill.agreement_key(label) == key
    if key == "privacy_policy":
        # Deliberately blank: acknowledging a privacy policy is a per-application
        # decision he wants to make himself, not something answered at scale.
        assert autofill.agreement_answer(prof, label)[1] is None
    else:
        assert autofill.agreement_answer(prof, label)[1], f"no answer configured for {key}"


def test_the_ai_authorship_affirmation_is_never_answered(prof):
    """Twilio's second checkbox. Not the tool's claim to make.

    The letters are LLM-drafted, so whether everything submitted "reflects your
    own work" depends on how much of the draft survived your editing. Nobody
    but the applicant can answer that, so it stays blank on every form.
    """
    label = (
        "By checking this box, I confirm I have read, reviewed and understood "
        "the guidelines outlined in the Candidate AI Responsible Use Policy. I "
        "affirm that all the information and materials I submit throughout my "
        "application and candidacy will reflect my own work and experience."
    )
    assert autofill.never_fill(label)
    assert autofill.agreement_key(label) is None
    assert autofill.agreement_answer(prof, label)[1] is None
    assert autofill.custom_answer(prof, label) is None


def test_marketing_sms_stays_off_while_updates_stay_on(prof):
    updates = ("would you also like to receive communications via SMS concerning "
               "the status of or next steps in the recruitment process?")
    marketing = ("Can we text you about other job opportunities that may "
                 "interest you?")
    assert autofill.agreement_answer(prof, updates)[1] == "yes"
    assert autofill.agreement_answer(prof, marketing)[1] == "no"


# ── Self-identification ────────────────────────────────────────────────────────

def test_sexual_orientation_is_answered_from_the_profile(prof):
    key, answer = autofill.eeo_answer(prof, "Voluntary Self-Identification of "
                                            "Sexual Orientation")
    assert key == "sexual_orientation"
    assert answer == "Bisexual"


def test_an_unanswered_identity_question_is_still_left_alone():
    """The mechanism stays opt-in: no profile entry, no answer."""
    blank = Profile()
    assert autofill.eeo_answer(blank, "Sexual Orientation")[1] is None
    assert autofill.eeo_answer(blank, "Do you identify as transgender?")[1] is None


def test_lgbt_community_question_resolves_to_yes(prof):
    """Mixpanel — 2026-08-27. Phrased as a plain yes/no, not "sexual
    orientation" at all, and the option list is Yes/No, not a list of
    orientations — so the configured "Bisexual" has to resolve to "Yes"."""
    label = "Do you identify as a member of the LGBT2QIA+ community?"
    key, answer = autofill.eeo_answer(prof, label)
    assert key == "sexual_orientation"
    assert answer == "Bisexual"
    assert autofill._eeo_option_matches("yes", answer) is True
    assert autofill._eeo_option_matches("no", answer) is False


# ── More real blanks, 2026-08-27 (Stripe, OpenAI, LaunchDarkly) ─────────────────

def test_sponsor_you_for_a_work_permit_is_recognized(prof):
    """Stripe phrases sponsorship as "sponsor you for a work permit", not
    "require sponsorship" or "visa sponsorship" — the old wording list."""
    label = ("Will you require Stripe to sponsor you for a work permit now or "
             "in the future for the location(s) you selected?")
    sponsor = [v for p, v in autofill._unambiguous_choices(prof) if "sponsor" in p]
    assert sponsor == ["No"]


def test_currently_located_question_is_answered(prof):
    """OpenAI: "Where are you currently located?" — the old pattern required
    the exact substring "where are you located", which "currently" breaks."""
    specs = autofill.build_specs(prof)
    match = next((s for s in specs if s.matches("Where are you currently located?")),
                 None)
    assert match is not None
    assert match.value == "Los Angeles, CA"


def test_whatsapp_recruiting_optin_routes_like_recruiting_sms(prof):
    label = "Do you opt-in to receive WhatsApp messages from Stripe Recruiting?"
    assert autofill.agreement_key(label) == "recruiting_sms"
    assert autofill.agreement_answer(prof, label)[1] == "yes"


def test_referrer_name_is_never_filled_with_the_candidates_own_name(prof):
    """LaunchDarkly — 2026-08-27. The generic full-name matcher's
    `full[\\s_-]*name` pattern is a substring of this question, so it filled
    the *referrer's* name field with the candidate's own name. Must stay
    blank regardless of profile settings — a real referral is a one-off worth
    typing by hand, not a default."""
    label = ("If yes, please provide the full name and work email of the "
             "LaunchDarkly employee who referred you.")
    # is_protected() is what the real fill loop checks before ever consulting
    # a FieldSpec (autofill.py:1032) — the full_name spec's own pattern still
    # matches this label (it's a substring match on "full name"), but the
    # protected check short-circuits before that spec is ever tried.
    assert autofill.is_protected(label)
    assert autofill.custom_answer(prof, "Were you referred to this role by a "
                                        "current employee?") == "No"


def test_start_date_is_computed_fresh_not_a_stored_string(prof):
    """A fixed profile string would go stale; this has to be computed at fill
    time from whatever "today" actually is."""
    import datetime as dt

    expected = (dt.date.today() + dt.timedelta(days=45)).strftime("%B %d, %Y")
    for label in ("When can you start a new role?",
                  "What is your earliest possible start date?"):
        assert autofill.custom_answer(prof, label) == expected


# ── Education (School/Degree/Discipline/dates) ──────────────────────────────────

def test_education_school_choices_match_what_was_confirmed_live(prof):
    """Live-verified 2026-08-27 against Stripe's actual Greenhouse school
    list: "Santa Monica College" returns zero options ("No options"); "San
    Jose State University" returns exactly one, itself. Preference order and
    the paired dates are his own answer, not invented -- get either wrong and
    an application says he was somewhere he wasn't."""
    choices = autofill.EDUCATION_SCHOOL_CHOICES
    assert choices == (
        ("Santa Monica College", "August", "2023", "January", "2026"),
        ("San Jose State University", "August", "2022", "May", "2023"),
    )


def test_degree_and_discipline_answer_from_the_profile(prof):
    """"Some College" was the honest instinct but isn't one of Stripe's real
    options (Associate's/Bachelor's/Doctor of Medicine/PhD/Engineer's/High
    School/JD/MBA/Master's/Other, confirmed live) -- "Other" is the closest
    truthful pick that doesn't claim a credential he doesn't have."""
    assert autofill.custom_answer(prof, "Degree") == "Other"
    # "Chemical Engineering" is the actual coursework, but LaunchDarkly's
    # Discipline list is generic engineering fields with no chemical entry;
    # his call 2026-08-29 was the broader "Engineering".
    assert autofill.custom_answer(prof, "Discipline") == "Engineering"


# ── Third live run, 2026-08-29 (Chime, Pinterest, GitLab, Benchling, Harvey) ────

def test_self_describe_is_not_treated_as_declining(prof):
    """A disability dropdown came back reading "I prefer to self-describe"
    with an empty required "Please specify" box underneath. Self-describing is
    not declining — it trades one answered question for a new blank one."""
    assert autofill._eeo_option_matches("I prefer to self-describe", "decline") is False
    assert autofill._eeo_option_matches("I don't wish to answer", "decline") is True
    assert autofill._eeo_option_matches("I prefer not to say", "decline") is True


def test_bare_i_identify_as_is_the_trans_question_not_gender_or_race(prof):
    """Greenhouse labels the trans/cis question just "I identify as:" while
    asking gender and race as "I identify my gender/race as" on the same
    form. An unanchored match would answer those two with a cis/trans value."""
    assert autofill.eeo_key("I identify as:") == "transgender"
    assert autofill.eeo_key("I identify my gender as:") == "gender"
    assert autofill.eeo_key("I identify my race/ethnicity as:") == "race"
    assert autofill.eeo_answer(prof, "I identify as:")[1] == "Cisgender"
    assert autofill._eeo_option_matches("Cisgender", "Cisgender") is True


def test_one_orientation_value_answers_both_question_shapes(prof):
    """An orientation list wants "Bisexual"; a binary LGBT-community question
    wants "Yes". Both come from the single profile value."""
    assert autofill._eeo_option_matches("Bisexual", "Bisexual") is True
    assert autofill._eeo_option_matches("Yes", "Bisexual") is True


def test_specific_questions_win_over_generic_substring_patterns(prof):
    """custom_answer returns the first matching pattern, so ordering is
    load-bearing. Both of these were answered wrongly by a generic pattern
    whose text is a substring of the specific question."""
    assert autofill.custom_answer(
        prof, "Are you subject to any employment agreements and/or "
              "post-employment restrictions with your current employer?") == "No"
    assert autofill.custom_answer(
        prof, "U.S. Work Authorization Status") == "Authorized to work in the US"
    # ...without breaking the generic answers they now precede.
    assert autofill.custom_answer(
        prof, "Who is your current or previous employer?") == "Dynamic Active"
    assert autofill.custom_answer(
        prof, "Are you authorized to work in the United States?") == "Yes"


def test_gitlab_and_timezone_questions_are_answered(prof):
    assert autofill.custom_answer(
        prof, "Do you have over 3 years over professional software "
              "engineering experience?") == "No"
    assert autofill.custom_answer(
        prof, "What timezone do you reside in?") == "Pacific Time (US & Canada)"
    assert autofill.custom_answer(
        prof, "Are you currently residing or willing to relocate to one of "
              "the following states?") == "California"


# ── Fourth live run, 2026-08-31 (Benchling, Supabase, LaunchDarkly) ─────────────

def test_ethnic_background_wording_reaches_the_race_field(prof):
    """LaunchDarkly asks "ethnic background", which contains neither "race",
    "racial", nor the full word "ethnicity" — so it matched no EEO pattern at
    all and the required field was left blank."""
    assert autofill.eeo_key("What is your ethnic background?") == "race"
    assert autofill.eeo_key("Ethnic origin") == "race"
    # The wordings that already worked must keep working.
    assert autofill.eeo_key("I identify my race/ethnicity as:") == "race"
    assert autofill.eeo_key("Are you Hispanic or Latino?") == "hispanic_ethnicity"


def test_pronouns_and_gender_identity_are_answered(prof):
    assert autofill.custom_answer(prof, "Pronouns (the pronouns you use)") == "He/Him"


def test_free_text_essay_questions_are_answered(prof):
    """Supabase and Benchling gate submission on long-form questions. Blank
    ones block the form entirely, so these carry a real prepared answer."""
    remote = autofill.custom_answer(
        prof, "Tell us about your experience working in an async and/or remote "
              "environment. What practices or approaches have worked well for "
              "you? What challenges have you faced?")
    assert remote and len(remote.split()) > 80
    assert "detriment" in remote          # his own framing, kept verbatim

    ai = autofill.custom_answer(
        prof, "What AI tools are you currently using today and how are you using them?")
    assert ai and "Claude Code" in ai and "scikit-learn" in ai

    oss = autofill.custom_answer(
        prof, "Have you made any open source contributions in the past that "
              "you'd like to share with us?")
    # Says "no" honestly rather than padding — the claim, not just non-empty.
    assert oss and "Nothing merged upstream yet" in oss


# ── Fifth live run, 2026-09-05 (Chime, GitLab, Supabase) ───────────────────────

def test_i_identify_as_matches_despite_the_field_id_being_appended(prof):
    """eeo_key is handed label, name, id and placeholder joined together, so
    the blob for Chime's trans/cis question is "I identify as:  4024621002".
    The previous pattern anchored with a trailing `$`, which can never match
    that -- the field sat at "Select..." on a real submitted application while
    every other EEO question filled. Verified live against Chime's form."""
    assert autofill.eeo_key("I identify as:", "", "4024621002", "") == "transgender"
    assert autofill.eeo_answer(prof, "I identify as:", "", "4024621002", "")[1] == "Cisgender"
    # Chime lists "Cisgender" verbatim as an option.
    assert autofill._eeo_option_matches("Cisgender", "Cisgender") is True


def test_the_anchor_still_keeps_it_off_the_other_identify_questions(prof):
    """Chime asks four "I identify ..." questions on one form. Matching the
    wrong one answers a demographic question with a cis/trans value."""
    assert autofill.eeo_key("I identify my gender as:", "", "1", "") == "gender"
    assert autofill.eeo_key("I identify my race/ethnicity as:", "", "2", "") == "race"
    assert autofill.eeo_key(
        "I identify my sexual orientation as:", "", "3", "") == "sexual_orientation"


def test_age_eighteen_question_is_answered(prof):
    """Supabase gates submission on it; it had no pattern at all."""
    for label in ("Are you over the age of 18?",
                  "Are you at least 18 years of age?",
                  "Are you 18 years of age or older?"):
        assert autofill.custom_answer(prof, label) == "Yes", label

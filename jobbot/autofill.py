"""Fill an application form, then hand the browser to you.

This is autofill, not auto-apply. It opens the posting in a real browser,
fills the fields you would otherwise retype for the fiftieth time, attaches
your resume, pastes the cover letter, and then stops with the browser open so
you can check the result and press Submit yourself.

Three things it will never do:

  * Submit. There is no code path here that clicks a submit button.
  * Tick a checkbox. Agreements, consents, and acknowledgements are yours.
  * Fill anything in NEVER_FILL_PATTERNS — signatures, certifications, SSN,
    date of birth, salary expectations. No profile setting unlocks those.

Demographic questions are answered only from the profile's `eeo` and
`eeo_random` maps, and only where the user has written an answer. A blank entry
means the field is left untouched and reported, every run. `eeo_random` picks
between weighted options for questions where more than one answer is genuinely
true of the user; every option there is one they have declared honest, so it
chooses among truths rather than inventing one.

Field matching is heuristic. Greenhouse, Lever, and Ashby all render forms
differently and companies add custom questions, so expect it to fill most of
a form and miss some of it. Every run prints what it filled and what it
skipped, and you are looking at the page anyway before you send it.
"""

from __future__ import annotations

import datetime as _dt
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from jobbot.profile import Profile

logger = logging.getLogger(__name__)


# ── What we never touch ────────────────────────────────────────────────────────

# Three tiers of protection.
#
# NEVER_FILL is absolute. No profile setting unlocks these — a script must not
# be signing anything or typing a social security number.
#
# The last entry is the one worth explaining. Several boards now ask you to
# affirm that everything you submit "reflects your own work and experience".
# jobbot drafts cover letters with an LLM, so whether that affirmation is true
# depends on how much of the draft survived your editing — which only you know.
# A script ticking it on your behalf would be making a claim about authorship
# that the script is in no position to make.
NEVER_FILL_PATTERNS = [
    r"date of birth|birth ?date|\bdob\b",
    r"social security|\bssn\b",
    r"salary|compensation expect|desired pay",
    r"signature|\bsign\b",
    r"own work and experience|reflect my own work|responsible use polic|"
    r"\bai\b.{0,30}polic|generative ai",
    # Legal attestations, not form fields — checking these is a decision about
    # what he is swearing to, never a default.
    r"arbitration agreement",
    r"hereby certify|certify that (?:i|the)",
    # "Please provide the full name ... of the employee who referred you" — a
    # false positive here doesn't leave the field blank, it fills it wrong: the
    # generic full-name matcher (`full[\s_-]*name` is a substring of this
    # question) previously answered a referrer's name with the candidate's own
    # name. Blank is correct whether or not there was a referral; a real one
    # is a one-off worth typing by hand.
    r"employee who referred|person who referred you",
]

# AGREEMENTS are the consent boxes a form requires before it will accept an
# application at all: privacy policies, interview-recording opt-ins, SMS
# preferences. Blank by default and invisible to generic field matching, filled
# only when `profile.agreements` carries an explicit answer — the same bargain
# EEO gets. An entry there is you deciding once instead of twenty times; an
# empty one leaves the box for you.
AGREEMENT_FIELDS: dict[str, str] = {
    "interview_recording": r"record(?:ed|ing)?\b.{0,60}(?:interview|transcri)|"
                           r"interview.{0,40}record(?:ed|ing)|transcrib",
    # "Can we text you about other job opportunities" never says "SMS" or
    # "text message", so matching only those wordings let a marketing opt-in
    # fall through to the recruiting-updates answer, which is a yes.
    "marketing_sms": r"(?:sms|text(?:ing)?(?: message)?)\b.{0,80}"
                     r"(?:other|future|new|additional)\s+(?:job|role|opportunit|position)|"
                     r"(?:marketing|promotional).{0,30}(?:sms|text)",
    "recruiting_sms": r"\bsms\b|\btext message|whatsapp message",
    "privacy_policy": r"privacy polic|candidate privacy|applicant privacy|"
                      r"data protection notice|\backnowledg|processed in accordance",
    "terms": r"\bagree\b|consent|\bterms\b|certif",
}

# EEO questions. Left blank by default, and filled only when the profile's
# `eeo` map carries an explicit answer for that field. The point is that
# answering these is the user's decision — the default protects the decision,
# and an entry in the profile is them making it.
# Order matters: eeo_key returns the first match, so the more specific
# questions are checked first. "Which races/ethnicities do you belong to?" must
# resolve to `race` — matching it as `hispanic_ethnicity` would answer a
# multi-choice race question with a Hispanic yes/no answer.
EEO_FIELDS: dict[str, str] = {
    "gender": r"\bgender\b",
    "veteran_status": r"\bveteran\b|military",
    # "What is your ability status?" is CircleCI's wording for the disability
    # question. It contains neither "disability" nor "self-identify".
    "disability": r"disabilit|ability status|\bability\b.{0,12}status",
    # "What is your ethnic background?" matched nothing at all: it contains
    # neither "race"/"racial" nor the full word "ethnicity", so it fell past
    # this pattern AND past hispanic_ethnicity below, and the field was left
    # blank on every form that words it that way (LaunchDarkly).
    "race": r"\braces?\b|racial|race\s*/\s*ethnicit|ethnicit(?:y|ies)\b(?=.*\brace)"
            r"|ethnic background|ethnic origin",
    "hispanic_ethnicity": r"\bhispanic\b|\blatino\b|\bethnicit",
    # Voluntary self-identification, same bargain as the rest: blank unless the
    # profile answers it. Kept in the EEO map rather than in a general answers
    # map so it can only ever reach a self-identification field.
    # Some ATS phrase this as a plain "member of the LGBT2QIA+ community?"
    # yes/no rather than "sexual orientation" at all.
    "sexual_orientation": r"sexual orientation|lgbt",
    # Anchored on purpose. Greenhouse labels the trans/cis question just
    # "I identify as:" on some forms, with gender and race asked separately
    # as "I identify my gender as" / "I identify my race as". An unanchored
    # "i identify as" would swallow those two and answer the wrong
    # demographic question.
    # Greenhouse labels the trans/cis question just "I identify as:" on forms
    # that ask gender, race and orientation separately as "I identify my
    # <thing> as". Distinguishing them is what the anchor is for.
    #
    # It must anchor at the START only. eeo_key is handed label, name, id and
    # placeholder joined together, so the blob is "I identify as:  4024621002"
    # — a trailing `$` can never match, which is why this field sat at
    # "Select..." on Chime's form while every other EEO question filled. The
    # negative lookahead is what keeps it off the "I identify my ..." labels.
    "transgender": r"transgender|^i identify as\b(?!\s*my)",
}

# Identity questions with no profile slot at all — always left alone.
OTHER_IDENTITY_PATTERNS = [
    r"pronoun",
    r"self[- ]?identif",
]

PROTECTED_PATTERNS = (
    NEVER_FILL_PATTERNS
    + list(EEO_FIELDS.values())
    + list(AGREEMENT_FIELDS.values())
    + OTHER_IDENTITY_PATTERNS
)

_NEVER_FILL = [re.compile(p, re.IGNORECASE) for p in NEVER_FILL_PATTERNS]
_EEO = {k: re.compile(v, re.IGNORECASE) for k, v in EEO_FIELDS.items()}
_AGREEMENTS = {k: re.compile(v, re.IGNORECASE) for k, v in AGREEMENT_FIELDS.items()}
_PROTECTED = [re.compile(p, re.IGNORECASE) for p in PROTECTED_PATTERNS]


def agreement_key(*texts: str) -> str | None:
    """Which consent question this field is, if it is one.

    Order matters as it does for EEO. "SMS about other opportunities" has to
    resolve to `marketing_sms` before the bare `\\bsms\\b` in `recruiting_sms`
    claims it, or a blanket yes to recruiter texts would also opt you into
    marketing you said no to.
    """
    blob = _normalize(*texts)
    if never_fill(blob):
        return None
    for key, pattern in _AGREEMENTS.items():
        if pattern.search(blob):
            return key
    return None


def agreement_answer(profile: Profile, *texts: str) -> tuple[str | None, str | None]:
    """Return (agreement_key, answer), where None means leave it alone."""
    key = agreement_key(*texts)
    if not key:
        return None, None
    answer = str((getattr(profile, "agreements", None) or {}).get(key, "")).strip()
    return key, (answer or None)


def _normalize(*texts: str) -> str:
    blob = " ".join(t or "" for t in texts)
    return re.sub(r"[_\-.]+", " ", blob)


def never_fill(*texts: str) -> bool:
    """Absolutely off limits, regardless of profile settings."""
    blob = _normalize(*texts)
    return any(p.search(blob) for p in _NEVER_FILL)


def eeo_key(*texts: str) -> str | None:
    """Which EEO question this field is, if it is one."""
    blob = _normalize(*texts)
    for key, pattern in _EEO.items():
        if pattern.search(blob):
            return key
    return None


def eeo_answer(profile: Profile, *texts: str) -> tuple[str | None, str | None]:
    """Return (eeo_key, answer) for a field.

    An answer of None means leave it alone: either the profile has no entry,
    or the entry is deliberately blank because the user wants to decide on the
    form itself.

    `eeo_random` takes precedence and picks between weighted options. Every
    option there is an answer the user has said is true of them — this chooses
    among truths, it never manufactures one.
    """
    key = eeo_key(*texts)
    if not key:
        return None, None

    weighted = (profile.eeo_random or {}).get(key)
    if weighted:
        options, weights = [], []
        for entry in weighted:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                option, weight = entry
            else:
                option, weight = entry, 1
            option = str(option).strip()
            try:
                weight = float(weight)
            except (TypeError, ValueError):
                weight = 1.0
            if option and weight > 0:
                options.append(option)
                weights.append(weight)
        if options:
            return key, random.choices(options, weights=weights, k=1)[0]

    answer = str((profile.eeo or {}).get(key, "")).strip()
    return key, (answer or None)


# Answers computed fresh at fill time rather than stored as a fixed string —
# "when can you start" dated 2026-08-25 and read on 2026-09-30 would be a lie.
# Checked ahead of the profile map so no custom_answers entry can shadow it.
_COMPUTED_ANSWERS: tuple[tuple[str, Callable[[], str]], ...] = (
    (r"when (?:can|could) you start|earliest (?:possible )?start date|"
     r"available to start|^start date$|when would you be available",
     lambda: (_dt.date.today() + _dt.timedelta(days=45)).strftime("%B %d, %Y")),
)
_COMPUTED = [(re.compile(p, re.IGNORECASE), fn) for p, fn in _COMPUTED_ANSWERS]


def custom_answer(profile: Profile, *texts: str) -> str | None:
    """Answer for a recurring custom question, from the profile's map.

    Consent questions are refused here even when a pattern would match them.
    They are answerable only through `profile.agreements`, so that a broad
    custom pattern (someone's `".*"` catch-all) can never quietly become a yes
    to a privacy policy.
    """
    blob = _normalize(*texts)
    if never_fill(blob) or agreement_key(blob):
        return None
    for pattern, compute in _COMPUTED:
        if pattern.search(blob):
            return compute()
    for pattern, answer in (profile.custom_answers or {}).items():
        answer = str(answer).strip()
        if not answer:
            continue
        try:
            if re.search(pattern, blob, re.IGNORECASE):
                return answer
        except re.error:
            if pattern.lower() in blob.lower():
                return answer
    return None


def is_protected(*texts: str) -> bool:
    """Whether a field is off limits to the ordinary field-matching passes.

    Separators are normalized to spaces first. Form fields are named things
    like "veteran_status", and `\\bveteran\\b` does not match that — an
    underscore is a word character, so there is no boundary after "veteran".
    That let a protected EEO field through as an ordinary unmatched one.

    EEO fields stay protected here even when the profile configures an answer;
    they are filled by the dedicated EEO pass, never by generic matching.
    """
    return any(p.search(_normalize(*texts)) for p in _PROTECTED)


# ── What we fill ───────────────────────────────────────────────────────────────

@dataclass
class FieldSpec:
    """One value and the ways a form might ask for it."""

    key: str
    value: str
    patterns: list[str]

    def matches(self, *texts: str) -> bool:
        blob = " ".join(t or "" for t in texts).lower()
        return any(re.search(p, blob, re.IGNORECASE) for p in self.patterns)


def build_specs(profile: Profile) -> list[FieldSpec]:
    """Map profile values onto the labels forms actually use."""
    return [
        FieldSpec("first_name", profile.first_name,
                  [r"first[\s_-]*name", r"^given name", r"\bfname\b"]),
        FieldSpec("last_name", profile.last_name,
                  [r"last[\s_-]*name", r"family name", r"surname", r"\blname\b"]),
        FieldSpec("full_name", profile.full_name,
                  [r"^full[\s_-]*name", r"^name$", r"your name"]),
        # `matches` searches label+name+id+placeholder joined together, so an
        # anchored "^name$" never fires. The label leads the blob, so "^name\b"
        # picks out Ashby's single Name field without also claiming "First Name".
        FieldSpec("full_name", profile.full_name,
                  [r"^name\b", r"full[\s_-]*name", r"legal name",
                   r"_systemfield_name"]),
        FieldSpec("email", profile.email, [r"e-?mail"]),
        FieldSpec("phone", profile.phone, [r"phone", r"mobile", r"telephone"]),
        FieldSpec("linkedin", profile.linkedin_url, [r"linked ?in"]),
        FieldSpec("github", profile.github_url, [r"git ?hub"]),
        FieldSpec("website", profile.portfolio_url or profile.github_url,
                  [r"website", r"portfolio", r"personal site", r"\burl\b"]),
        # Ahead of `city` and `state` on purpose. Figma asks "From where do you
        # intend to work? Please list city and state (ie: San Francisco, CA)",
        # which contains the word "city" — so a later position loses the label
        # to the bare city spec and answers "Los Angeles" to a question that
        # wants "Los Angeles, CA".
        FieldSpec("work_location", f"{profile.city}, {profile.state}",
                  [r"where do you (?:intend|plan|expect) to work",
                   r"where will you (?:be )?work", r"work location",
                   r"city and state", r"where are you (?:currently )?located"]),
        FieldSpec("city", profile.city, [r"^city", r"\bcity\b"]),
        FieldSpec("state", profile.state, [r"^state", r"province", r"region"]),
        FieldSpec("postal_code", getattr(profile, "postal_code", "") or "",
                  [r"zip ?code", r"\bzip\b", r"postal ?code", r"post ?code"]),
        FieldSpec("location", f"{profile.city}, {profile.state}",
                  [r"location", r"where are you based", r"current city"]),
    ]


@dataclass
class FillReport:
    filled: dict[str, str] = field(default_factory=dict)
    skipped_protected: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    eeo_answered: dict[str, str] = field(default_factory=dict)
    eeo_left_blank: list[str] = field(default_factory=list)
    agreements_ticked: dict[str, str] = field(default_factory=dict)
    agreements_left: list[str] = field(default_factory=list)
    revealed_by: str = ""
    resume_attached: bool = False
    cover_letter_pasted: bool = False
    submit_label: str | None = None
    scroll_note: str | None = None
    in_frame: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def blocked_by_overlay(self) -> bool:
        """Whether something on top of the page swallowed our clicks.

        Cookie and consent banners sit above the form and intercept pointer
        events, so every dropdown times out. Dismissing one is the user's
        decision, so this is reported rather than clicked away.
        """
        return any(
            "intercepts pointer events" in e or "cookiebanner" in e.lower()
            for e in self.errors
        )

    def render(self) -> str:
        lines = []
        if self.filled:
            lines.append("Filled:")
            for k, v in self.filled.items():
                shown = v if len(v) <= 48 else v[:45] + "…"
                lines.append(f"  {k:14} {shown}")
        lines.append(
            f"  {'resume':14} "
            + ("attached" if self.resume_attached else "NOT attached — do it yourself")
        )
        lines.append(
            f"  {'cover letter':14} "
            + ("pasted" if self.cover_letter_pasted else "not pasted — paste it yourself")
        )

        if self.eeo_answered:
            lines.append("\nEEO answers from your profile:")
            for k, v in self.eeo_answered.items():
                lines.append(f"  {k:20} {v}")

        if self.eeo_left_blank:
            lines.append("\nEEO left blank — decide on the form:")
            for k in self.eeo_left_blank:
                lines.append(f"  - {k}")

        if self.agreements_ticked:
            lines.append("\nConsents, per your profile — check these before you submit:")
            for k, v in self.agreements_ticked.items():
                lines.append(f"  {k:20} {v}")

        if self.agreements_left:
            lines.append("\nConsents with no answer in your profile — yours to tick:")
            for k in self.agreements_left:
                lines.append(f"  - {k}")

        if self.skipped_protected:
            lines.append("\nLeft alone on purpose (yours to answer):")
            for name in self.skipped_protected:
                lines.append(f"  - {name}")

        if self.unmatched:
            lines.append("\nCouldn't match these — fill them by hand:")
            for name in self.unmatched[:15]:
                lines.append(f"  - {name}")

        if self.submit_label:
            lines.append(f"\nScrolled to the \"{self.submit_label}\" button — it should be "
                         "on screen now.")
        else:
            lines.append(
                "\nCouldn't find a submit button to scroll to."
                + (" The form is inside an embedded frame, which scrolls "
                   "separately from the page around it — click inside the form "
                   "first, then scroll." if self.in_frame else "")
            )

        if self.scroll_note:
            lines.append(
                f"\n⚠  Scrolling looks blocked: {self.scroll_note}.\n"
                "   Press [u] to restore scrolling without dismissing the banner."
            )

        if self.blocked_by_overlay:
            lines.append(
                "\n⚠  A cookie/consent banner is covering the form and blocking "
                "the dropdowns.\n   Dismiss it however you prefer, then press "
                "[r] to fill again."
            )

        if self.errors:
            lines.append("\nProblems:")
            for e in self.errors:
                first = e.split("\n")[0]
                lines.append(f"  ! {first[:120]}")

        return "\n".join(lines)


# ── Filling ────────────────────────────────────────────────────────────────────

# Resolved in the page rather than in Python: an ElementHandle has no `.page`
# attribute, so document lookups from here raise AttributeError and get
# swallowed. That silently disabled both the aria-labelledby and the
# label[for=…] paths, leaving every custom question anonymous.
_DESCRIBE_JS = """e => {
  const attr = (a) => e.getAttribute(a) || '';
  let label = attr('aria-label');

  if (!label) {
    const ids = attr('aria-labelledby').split(/\\s+/).filter(Boolean);
    for (const id of ids) {
      const n = document.getElementById(id);
      if (n && n.innerText && n.innerText.trim()) { label = n.innerText.trim(); break; }
    }
  }
  if (!label && e.id) {
    const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
    if (l && l.innerText) label = l.innerText.trim();
  }
  if (!label) {
    const l = e.closest('label');
    if (l && l.innerText) label = l.innerText.trim();
  }
  // Ashby renders the question as a sibling label inside a wrapper div: no
  // for=, no aria-label, and the name/id are uuids. Without this the label
  // comes back empty and every field falls through to its placeholder, which
  // on Ashby is the literal string "Type here..." for all of them — so
  // nothing matched and the whole form was reported unfilled.
  if (!label) {
    let cur = e;
    for (let depth = 0; cur && depth < 5; depth++) {
      cur = cur.parentElement;
      if (!cur) break;
      const cand = cur.querySelector('label, legend, [class*=label], [class*=Label]');
      if (cand && cand.innerText) {
        const t = cand.innerText.replace(/\\s+/g, ' ').trim();
        // Long text means we climbed into the form body, not a field label.
        if (t.length >= 3 && t.length <= 150) { label = t; break; }
      }
    }
  }
  return {
    label: (label || '').replace(/\\s+/g, ' ').replace(/\\s*\\*$/, '').trim(),
    name: attr('name'),
    id: e.id || '',
    placeholder: attr('placeholder'),
  };
}"""


def _describe(el) -> tuple[str, str, str, str]:
    """Best-effort (label, name, id, placeholder) for a form control."""
    try:
        info = el.evaluate(_DESCRIBE_JS)
    except Exception:
        return "", "", "", ""
    return info["label"], info["name"], info["id"], info["placeholder"]


# Boards that show the posting first and keep the form behind a button or a
# second tab. Ashby does this on every listing: the page opens on "Overview"
# with an "Application" tab beside it, so a fill pass that starts immediately
# runs against a page with no form on it, matches nothing, and reports every
# field as unmatched — which reads as the matcher being broken when the form
# was simply never on screen.
# "Submit application" is deliberately absent. It is the label on Greenhouse's
# actual Submit button, and no amount of surrounding context makes clicking it
# safe — a page that merely looked form-less would send the application.
_REVEAL_TEXT = re.compile(
    r"^\s*(?:apply(?:\s+(?:for\s+this\s+job|now|here|manually|to\s+this\s+role))?"
    r"|application"
    r"|start\s+(?:your\s+)?application"
    r"|continue\s+to\s+application)\s*$",
    re.IGNORECASE,
)

# Workday's "Start Your Application" dialog offers four ways in, stacked in
# this DOM order: Autofill with Resume, Apply Manually, Use My Last
# Application, Apply With LinkedIn. Taking the first match would take the
# first one, so the wanted route gets a pass of its own before the general
# sweep.
#
# "Apply Manually" is the wanted route deliberately. "Autofill with Resume"
# hands Workday the PDF and lets its parser populate the form, overwriting
# fields with whatever it believes it read — and the bargain this whole module
# rests on is that a field is either filled from the profile or left for a
# human to answer.
_REVEAL_PREFERRED = re.compile(r"^\s*apply\s+manually\s*$", re.IGNORECASE)

# Anything that would take us off the posting instead of opening its form, or
# that opens it the wrong way. "Use my last application" replays an earlier
# submission wholesale, which is a different application than the one being
# prepared here.
_REVEAL_EXCLUDE = re.compile(
    r"linkedin|indeed|glassdoor|sign\s*in|log\s*in|create|share|refer|"
    r"other\s+jobs|all\s+jobs|back\b|"
    r"autofill|use\s+my\s+last",
    re.IGNORECASE,
)


def form_is_present(ctx, minimum: int = 3) -> bool:
    """Whether this context already shows something worth filling."""
    try:
        fields = ctx.query_selector_all(
            "input[type=text], input[type=email], input[type=tel], "
            "input:not([type]), textarea, input[type=file]"
        )
    except Exception:
        return False
    visible = 0
    for el in fields:
        try:
            if el.is_visible():
                visible += 1
        except Exception:
            continue
        if visible >= minimum:
            return True
    return visible >= minimum


def open_application_form(page, settle_ms: int = 2500) -> str:
    """Click through to the application form when it is behind a tab or button.

    Returns the label that was clicked, or "" if the form was already showing.
    Clicks nothing that looks like it leads off the posting, and never touches
    a Submit control — the only thing being opened here is the form itself.
    """
    for ctx in (page, *(f for f in getattr(page, "frames", []) or [])):
        if form_is_present(ctx):
            return ""

    # Deliberately includes a bare "a" tag. Lever's own "Apply for this job"
    # control is a plain <a class="postings-btn ...987 href=".../apply">
    # with no role=button and no "button" class — the earlier selector list
    # (a[role=button], a.button) never matched it, so the click silently did
    # nothing and the form stayed hidden with the tab looking stuck.
    candidates = (
        "button", "a", "[role=button]", "[role=tab]", "[data-ui=apply-button]",
    )
    # Two passes. The first takes only the preferred route, so a dialog that
    # lists several ways in does not get answered by whichever happens to sit
    # highest in the DOM.
    for preferred_only in (True, False):
        for ctx in (page, *(f for f in getattr(page, "frames", []) or [])):
            for selector in candidates:
                try:
                    elements = ctx.query_selector_all(selector)
                except Exception:
                    continue
                for el in elements[:60]:
                    try:
                        if not el.is_visible():
                            continue
                        text = (el.inner_text() or "").strip()
                    except Exception:
                        continue
                    if not text or len(text) > 40:
                        continue
                    if preferred_only:
                        if not _REVEAL_PREFERRED.match(text):
                            continue
                    elif _REVEAL_EXCLUDE.search(text) or not _REVEAL_TEXT.match(text):
                        continue
                    # Unconditional. Anything wearing the Submit button's label
                    # is left alone whatever else it looks like.
                    if _SUBMIT_TEXT.search(text):
                        continue
                    try:
                        el.click(timeout=5000)
                        # Lever's control is a real link, not a same-page
                        # toggle — clicking it navigates. A toggle only needs
                        # the settle wait; a navigation needs load_state too,
                        # and asking for both costs nothing either way.
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=8000)
                        except Exception:
                            pass
                        page.wait_for_timeout(settle_ms)
                    except Exception:
                        continue
                    for check in (page, *(f for f in getattr(page, "frames", []) or [])):
                        if form_is_present(check):
                            return text
    return ""


def form_context(page, settle_ms: int = 2500):
    """Return whichever frame actually holds the application form.

    Plenty of companies serve a branded careers page that embeds the ATS form
    in an iframe — CircleCI's page has five inputs in the main document and
    twenty-five inside frames. Searching only the main frame finds nothing to
    fill and reports the form as empty. Note the canonical
    job-boards.greenhouse.io URL redirects back to the branded page, so this
    cannot be sidestepped by rewriting the link.
    """
    def count(ctx) -> int:
        try:
            return len(ctx.query_selector_all("input, textarea, select"))
        except Exception:
            return 0

    best, best_count = page, count(page)

    for _ in range(3):
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            n = count(frame)
            if n > best_count:
                best, best_count = frame, n
        if best_count >= 5:
            break
        # Embedded forms often arrive after the host page settles.
        try:
            page.wait_for_timeout(settle_ms)
        except Exception:
            break

    if best is not page:
        logger.info("form found in an embedded frame (%d controls)", best_count)
    return best


# The control that sends the application. We find it to scroll it into view and
# to name it in the report. Nothing here clicks it — see the test that asserts
# no click or press happens anywhere in this module's submit handling.
_SUBMIT_SELECTORS = (
    "button#submit_app",
    "button[type=submit]",
    "input[type=submit]",
    "[data-ui=submit-button]",
)

_SUBMIT_TEXT = re.compile(r"submit application|submit|send application", re.IGNORECASE)


def find_submit(ctx):
    """The submit control in this context, without touching it."""
    for selector in _SUBMIT_SELECTORS:
        try:
            el = ctx.query_selector(selector)
        except Exception:
            continue
        if el:
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue

    # Some forms use a plain <button> with no type attribute.
    try:
        for el in ctx.query_selector_all("button, [role=button]"):
            try:
                if el.is_visible() and _SUBMIT_TEXT.fullmatch((el.inner_text() or "").strip()):
                    return el
            except Exception:
                continue
    except Exception:
        pass
    return None


def scroll_note(page) -> str | None:
    """Why the page might refuse to scroll to the bottom.

    Two causes, both of which look identical from the user's side — the page
    simply stops before the submit button:

      * a modal or consent banner setting `overflow: hidden` on html/body,
        which kills scrolling everywhere on the page
      * the form living in an iframe, where the outer page has nothing left to
        scroll and the wheel never reaches the inner document
    """
    try:
        locked = page.evaluate(
            """() => {
                const cs = (e) => getComputedStyle(e);
                const de = document.documentElement, b = document.body;
                const hidden = (e) => ['hidden', 'clip'].includes(cs(e).overflowY)
                                   || ['hidden', 'clip'].includes(cs(e).overflow);
                if (hidden(de) || hidden(b)) return 'overflow';
                if (cs(b).position === 'fixed') return 'fixed-body';
                // A viewport taller than the entire browser window means the
                // page is being rendered into an emulated rectangle that does
                // not fit on screen. The page then scrolls to the bottom of a
                // viewport you cannot fully see, stranding the last strip of
                // the form below the window.
                if (window.outerHeight && innerHeight > window.outerHeight) {
                    return 'viewport:' + innerHeight + 'x' + window.outerHeight;
                }
                return '';
            }"""
        )
    except Exception:
        locked = ""

    if locked == "overflow":
        return ("something on the page has locked scrolling (overflow: hidden) — "
                "usually a cookie or consent modal that is still open")
    if locked == "fixed-body":
        return "the page body is pinned (position: fixed), which blocks scrolling"
    if isinstance(locked, str) and locked.startswith("viewport:"):
        inner, outer = locked.split(":", 1)[1].split("x")
        return (
            f"the page is rendered into a {inner}px-tall viewport inside a "
            f"{outer}px window, so its bottom edge is off screen. That is a "
            "browser launch setting, not the page — restart with a fresh "
            "`python -m jobbot apply`, which no longer fixes the viewport size"
        )
    return None


def close_open_dropdowns(page) -> int:
    """Close any react-select menu left open, and return how many.

    An open menu sits over the form and eats the wheel, so the page stops
    scrolling wherever the menu happens to be — which on Greenhouse is the
    demographic section, a long way above the submit button. The filler opens
    one of these per dropdown it answers, and a menu whose option was clicked
    does not always close on its own.

    Escape is the only key pressed here, and only on an expanded combobox. It
    closes a menu; it does not choose from one.
    """
    closed = 0
    for ctx in (page, *(f for f in getattr(page, "frames", []) or [])):
        try:
            controls = ctx.query_selector_all("[role=combobox][aria-expanded=true]")
        except Exception:
            continue
        for el in controls:
            try:
                el.press("Escape")
                closed += 1
            except Exception:
                continue
    return closed


def reveal_submit(page) -> tuple[str | None, str | None]:
    """Scroll the submit button into view. Returns (label, note).

    Does not click it. This exists because a filled form is useless if you
    cannot get to the bottom of it, and "scroll further" is not always
    something the user can do — an embedded form scrolls independently of the
    page around it, a consent modal can lock the page entirely, and an open
    dropdown menu swallows the wheel wherever it happens to be.
    """
    close_open_dropdowns(page)
    note = scroll_note(page)
    ctx = form_context(page)

    el = find_submit(ctx)
    if el is None and ctx is not page:
        el = find_submit(page)
    if el is None:
        return None, note

    try:
        el.scroll_into_view_if_needed(timeout=5000)
    except Exception as exc:
        return None, note or f"could not scroll to the submit button: {str(exc)[:80]}"

    try:
        label = (el.inner_text() or el.get_attribute("value") or "Submit").strip()
    except Exception:
        label = "Submit"
    return label[:40], note


def release_scroll_lock(page) -> bool:
    """Clear an overflow lock so the page scrolls again.

    Only ever called when the user asks for it, because the thing that set the
    lock is usually a consent modal — and this deliberately does not dismiss
    that modal or answer it. It restores scrolling and leaves the banner
    exactly where it is, still yours to decide about.
    """
    try:
        return bool(page.evaluate(
            """() => {
                let changed = false;
                for (const e of [document.documentElement, document.body]) {
                    const s = getComputedStyle(e);
                    if (['hidden', 'clip'].includes(s.overflow)
                        || ['hidden', 'clip'].includes(s.overflowY)) {
                        e.style.setProperty('overflow', 'auto', 'important');
                        changed = true;
                    }
                    if (s.position === 'fixed') {
                        e.style.setProperty('position', 'static', 'important');
                        changed = true;
                    }
                }
                return changed;
            }"""
        ))
    except Exception:
        return False


def posting_closed(page) -> str | None:
    """Whether the loaded page says this posting is no longer open.

    Worth checking before filling anything. A closed posting serves a page with
    no form on it, so every field goes unmatched, no file input exists to
    attach the resume to, and no textarea exists to paste the letter into — the
    run then reports "resume NOT attached / cover letter not pasted" as though
    something had gone wrong with the filling, when the posting simply is not
    there any more.

    Reads the real browser's rendered text, which is the only way to see this
    on the many boards that serve a closed posting as HTTP 200, or that put the
    listing behind a bot check the HTTP enricher cannot get past.
    """
    from jobbot.enrich import looks_closed

    for ctx in (page, *(f for f in getattr(page, "frames", []) or [])):
        try:
            text = ctx.inner_text("body")
        except Exception:
            continue
        if evidence := looks_closed(text):
            return evidence
    return None


# ── Did it actually go through? ────────────────────────────────────────────────
# Typing "y" used to be the whole proof that an application was sent. That
# records applied_at from an intention, not an event, and the two come apart in
# exactly the case that matters: you press Submit, a required question you
# never saw fails validation, the page scrolls back up, and the tab still looks
# submitted at a glance. That row then sits in the funnel as an application
# that was never received, and later teaches the model from an outcome that
# could not have happened.

_CONFIRM_URL = re.compile(
    r"/(?:confirmation|confirm|thanks|thank[-_]?you|applied|success|submitted)\b"
    r"|[?&](?:submitted|success|applied)=(?:true|1|yes)",
    re.IGNORECASE,
)

_CONFIRM_TEXT = (
    "your application has been submitted",
    "application has been submitted",
    "application was submitted",
    "application submitted",
    "successfully submitted",
    "submission was successful",
    "we have received your application",
    "we've received your application",
    "application received",
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "thank you for your interest in joining",
)

_ERROR_TEXT = (
    "this field is required",
    "please complete",
    "please fill",
    "please enter",
    "please select",
    "required field",
    "is required",
    "cannot be blank",
    "can't be blank",
    "there was a problem",
    "there were problems",
    "something went wrong",
    "please correct",
    "invalid",
)

# States returned by `submission_state`.
CONFIRMED = "confirmed"      # the page says it landed
ERRORS = "errors"            # the form rejected it, visibly
FORM_OPEN = "form_open"      # form still sitting there, unsubmitted
UNKNOWN = "unknown"          # cannot tell from this page


def _contexts(page):
    yield page
    for frame in getattr(page, "frames", []) or []:
        if frame is not getattr(page, "main_frame", None):
            yield frame


def _visible_text(ctx) -> str:
    try:
        return (ctx.inner_text("body") or "").lower()
    except Exception:
        return ""


def _visible_errors(ctx) -> list[str]:
    """Validation messages the page is currently showing."""
    found: list[str] = []
    selectors = (
        "[aria-invalid=true]",
        "[role=alert]",
        ".error:not(:empty)",
        ".field-error",
        ".invalid-feedback",
        "[class*=error][class*=message]",
    )
    for selector in selectors:
        try:
            elements = ctx.query_selector_all(selector)
        except Exception:
            continue
        for el in elements[:12]:
            try:
                if not el.is_visible():
                    continue
                text = re.sub(r"\s+", " ", (el.inner_text() or "").strip())
            except Exception:
                continue
            if text and text.lower() not in (f.lower() for f in found):
                found.append(text[:120])
    return found


def submission_state(page) -> tuple[str, str]:
    """Whether the application on this page actually went through.

    Returns (state, evidence). Deliberately conservative in both directions:
    it will not claim CONFIRMED without the page saying so, and it will not
    claim failure just because it cannot recognise a confirmation. Every ATS
    words this differently and some do it in an iframe, so UNKNOWN is a real
    answer and the caller has to let you overrule it.
    """
    try:
        url = page.url or ""
    except Exception:
        url = ""

    if _CONFIRM_URL.search(url):
        return CONFIRMED, f"url says so: {url[:120]}"

    for ctx in _contexts(page):
        text = _visible_text(ctx)
        if not text:
            continue
        for phrase in _CONFIRM_TEXT:
            if phrase in text:
                # A confirmation phrase sitting above a still-open form is the
                # posting's own blurb, not a receipt.
                if find_submit(ctx) is None:
                    return CONFIRMED, f"page says: {phrase!r}"

    errors: list[str] = []
    for ctx in _contexts(page):
        errors.extend(_visible_errors(ctx))
    if errors:
        return ERRORS, "; ".join(errors[:4])

    for ctx in _contexts(page):
        text = _visible_text(ctx)
        if any(phrase in text for phrase in _ERROR_TEXT) and find_submit(ctx) is not None:
            hit = next(p for p in _ERROR_TEXT if p in text)
            return ERRORS, f"page still shows {hit!r}"

    for ctx in _contexts(page):
        if find_submit(ctx) is not None:
            return FORM_OPEN, "the form and its Submit button are still on screen"

    return UNKNOWN, "no confirmation and no form — cannot tell from this page"


def wait_for_submission(page, timeout_ms: int = 90000, poll_ms: int = 1000):
    """Watch a tab until it confirms, visibly fails, or the wait runs out.

    Polling the page beats asking you to report what happened: the answer is on
    screen either way, and the thing being recorded is whether the employer
    received it, not whether you meant to send it.
    """
    waited = 0
    last = (UNKNOWN, "")
    while waited < timeout_ms:
        try:
            if page.is_closed():
                return UNKNOWN, "tab was closed"
        except Exception:
            return UNKNOWN, "tab is gone"
        state, evidence = submission_state(page)
        last = (state, evidence)
        if state in (CONFIRMED, ERRORS):
            return state, evidence
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            return last
        waited += poll_ms
    return last


def fill_form(page, profile: Profile, cover_letter: str = "") -> FillReport:
    """Fill what we safely can on the currently loaded page.

    Operates on whichever frame holds the form, which is not always the page
    itself.
    """
    outer = page
    revealed = open_application_form(page)
    page = form_context(page)
    report = FillReport(in_frame=page is not outer, revealed_by=revealed)
    specs = build_specs(profile)
    used: set[str] = set()

    inputs = page.query_selector_all(
        "input[type=text], input[type=email], input[type=tel], input[type=url], "
        "input:not([type]), textarea"
    )

    for el in inputs:
        try:
            if not el.is_visible() or not el.is_editable():
                continue
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        descriptor = label or name or el_id or placeholder or "(unlabelled field)"

        if is_protected(label, name, el_id, placeholder):
            report.skipped_protected.append(descriptor.strip()[:70])
            continue

        try:
            if (el.input_value() or "").strip():
                continue  # already has content; never clobber
        except Exception:
            pass

        # Cover letter textareas are handled separately below.
        if re.search(r"cover ?letter", f"{label} {name} {el_id}", re.IGNORECASE):
            continue

        match = next(
            (s for s in specs
             if s.key not in used and s.value and s.matches(label, name, el_id, placeholder)),
            None,
        )
        if not match:
            report.unmatched.append(descriptor.strip()[:70])
            continue

        try:
            el.fill(match.value)
            report.filled[match.key] = match.value
            used.add(match.key)
        except Exception as exc:
            report.errors.append(f"{descriptor[:40]}: {exc}")

    _fill_selects(page, profile, report)
    _fill_comboboxes(page, profile, report)
    _fill_location(page, profile, report)
    _fill_custom(page, profile, report)
    _fill_checkbox_groups(page, profile, report)
    _fill_education(page, profile, report)
    _fill_employment(page, profile, report)
    _fill_typeahead(page, profile, report)
    _fill_button_pairs(page, profile, report)
    _fill_radio_groups(page, profile, report)
    _fill_agreements(page, profile, report)
    _fill_eeo(page, profile, report)
    _attach_resume(page, profile, report)
    if cover_letter:
        _paste_cover_letter(page, cover_letter, report)
        _fill_open_prompts(page, cover_letter, report)

    # The same control can be seen by more than one pass (a combobox is also a
    # text input), so report each field once.
    _report_blank_eeo(page, profile, report)

    # A control is seen by several passes (a combobox is also a text input), so
    # an earlier pass may report a field the later one went on to fill. Reduce
    # to one honest line per field, keyed on a prefix because `filled` stores
    # truncated labels.
    filled_prefixes = [k[:24].lower() for k in report.filled]

    def was_filled(descriptor: str) -> bool:
        d = descriptor[:24].lower()
        return any(d == f or d.startswith(f) or f.startswith(d) for f in filled_prefixes)

    report.eeo_left_blank = [
        k for k in dict.fromkeys(report.eeo_left_blank)
        if k not in report.eeo_answered
    ]
    report.skipped_protected = [
        s for s in dict.fromkeys(report.skipped_protected)
        if not was_filled(s)
        and eeo_key(s) not in report.eeo_answered
        and eeo_key(s) not in report.eeo_left_blank
    ]
    report.unmatched = [
        u for u in dict.fromkeys(report.unmatched)
        if not was_filled(u) and not is_protected(u)
    ]

    # Leave the browser showing the button you are about to press, rather than
    # at the top of a form you now have to scroll through to find it.
    report.submit_label, report.scroll_note = reveal_submit(outer)
    return report


# Real education history: attended without completing a degree. Preference
# order matters — Santa Monica College is where the résumé's coursework
# actually is, but confirmed live 2026-08-27 that Stripe's Greenhouse school
# list has no match for it at all ("No options"); San José State does exist
# there. Whichever school is actually selected picks its own paired date
# range — a date range attached to no school, or the wrong school's dates,
# would misrepresent when he was where.
EducationChoice = tuple[str, str, str, str, str]
EDUCATION_SCHOOL_CHOICES: tuple[EducationChoice, ...] = (
    # (search text, start month, start year, end month, end year)
    ("Santa Monica College", "August", "2023", "January", "2026"),
    ("San Jose State University", "August", "2022", "May", "2023"),
)


def _search_select(page, el, search_text: str) -> bool:
    """Open a react-select combobox, search it, and click an exact/contains
    match if one exists. Returns whether something was picked.

    Shared by school/degree/discipline/month fields — all four render as the
    identical `input[role=combobox]` widget on Greenhouse.
    """
    close_open_dropdowns(page)
    _open_dropdown(page, el)
    el.fill(search_text)
    page.wait_for_timeout(700)
    options = _options_for(page, el)
    chosen = None
    for option in options:
        text = (option.inner_text() or "").strip()
        if not text:
            continue
        if text.lower() == search_text.lower():
            chosen = option
            break
        if chosen is None and search_text.lower() in text.lower():
            chosen = option
    if chosen is None:
        return False
    chosen.click(timeout=6000)
    return True


def _fill_education(page, profile: Profile, report: FillReport) -> None:
    """The School (+ Degree/Discipline/dates) block Greenhouse renders for
    education history.

    Tries each entry in EDUCATION_SCHOOL_CHOICES in order against whatever
    THIS company's own school list actually contains — confirmed live that
    school lists vary by employer, not just by ATS — and only fills dates
    once a school has actually been selected.
    """
    try:
        boxes = page.query_selector_all("input[role=combobox]")
    except Exception:
        return

    school_el = None
    for el in boxes:
        try:
            if not el.is_visible() or (el.input_value() or "").strip():
                continue
        except Exception:
            continue
        label, name, el_id, placeholder = _describe(el)
        if re.search(r"^school\b", _normalize(label, name, el_id, placeholder), re.IGNORECASE):
            school_el = el
            break

    if school_el is None:
        return  # no education block on this form, or already filled

    chosen = None
    for name, start_month, start_year, end_month, end_year in EDUCATION_SCHOOL_CHOICES:
        try:
            if _search_select(page, school_el, name):
                chosen = (name, start_month, start_year, end_month, end_year)
                report.filled["school"] = name
                break
        except Exception as exc:
            report.errors.append(f"school ({name}): {exc}")

    if chosen is None:
        report.unmatched.append("School (no configured school found in this list)")
        return

    _, start_month, start_year, end_month, end_year = chosen

    for el in page.query_selector_all("input[role=combobox]"):
        try:
            if not el.is_visible() or (el.input_value() or "").strip():
                continue
        except Exception:
            continue
        label, name, el_id, placeholder = _describe(el)
        blob = _normalize(label, name, el_id, placeholder)
        target = None
        if re.search(r"^start.{0,10}month", blob, re.IGNORECASE):
            target = start_month
        elif re.search(r"^end.{0,10}month", blob, re.IGNORECASE):
            target = end_month
        if target is None:
            continue
        try:
            if _search_select(page, el, target):
                report.filled[(label or name or el_id)[:24]] = target
        except Exception as exc:
            report.errors.append(f"{(label or name or el_id)[:30]}: {exc}")

    # Year fields are plain <input type=number>, untouched by every other
    # pass (the main text-input loop explicitly excludes type=number).
    try:
        year_inputs = page.query_selector_all("input[type=number]")
    except Exception:
        year_inputs = []
    for el in year_inputs:
        try:
            if not el.is_visible() or (el.input_value() or "").strip():
                continue
        except Exception:
            continue
        label, name, el_id, placeholder = _describe(el)
        blob = _normalize(label, name, el_id, placeholder)
        target = None
        if re.search(r"^start.{0,10}year", blob, re.IGNORECASE):
            target = start_year
        elif re.search(r"^end.{0,10}year", blob, re.IGNORECASE):
            target = end_year
        if target is None:
            continue
        try:
            el.fill(target)
            report.filled[(label or name or el_id)[:24]] = target
        except Exception as exc:
            report.errors.append(f"{(label or name or el_id)[:30]}: {exc}")


def _fill_employment(page, profile: Profile, report: FillReport) -> None:
    """The Company/Title/start/end block some boards render for work history.

    Same shape as the education block and broken the same way: the month
    fields are `input[role=combobox]` and the year fields are
    `input[type=number]`, a type the main text-input pass does not select, so
    a required employment row stayed blank with everything around it filled.

    Fills only the most recent role — these blocks start with one row and an
    "Add another" link, and inventing extra rows is not this tool's call.
    """
    most_recent = (profile.work_experience or [None])[0]
    if not most_recent:
        return

    company = str(most_recent.get("company", "")).strip()
    # "(Remote)" is a worksite note, not part of the job title — it belongs in
    # a location field, not typed into "Title".
    title = re.sub(r"\s*\((?:remote|hybrid|on-?site)\)\s*$", "",
                   str(most_recent.get("title", "")).strip(), flags=re.IGNORECASE)
    start = str(most_recent.get("start_date", ""))
    end = str(most_recent.get("end_date", ""))
    if not company or not title:
        return

    def month_year(token: str) -> tuple[str, str]:
        """'2025-12' -> ('December', '2025'). Empty when unparseable."""
        token = token.strip()
        m = re.match(r"(\d{4})-(\d{1,2})", token)
        if not m:
            return "", ""
        year, month = m.group(1), int(m.group(2))
        if not 1 <= month <= 12:
            return "", year
        return _dt.date(2000, month, 1).strftime("%B"), year

    start_month, start_year = month_year(start)
    end_month, end_year = month_year(end)

    text_targets = (
        (r"company name|^company\b|employer name", company),
        (r"^title\b|job title", title),
    )
    for el in page.query_selector_all("input[type=text], input:not([type])"):
        try:
            if not el.is_visible() or (el.input_value() or "").strip():
                continue
        except Exception:
            continue
        label, name, el_id, placeholder = _describe(el)
        blob = _normalize(label, name, el_id, placeholder)
        if is_protected(label, name, el_id, placeholder):
            continue
        value = next(
            (v for p, v in text_targets if v and re.search(p, blob, re.IGNORECASE)),
            None,
        )
        if not value:
            continue
        try:
            el.fill(value)
            report.filled[(label or name or el_id)[:24]] = value
        except Exception as exc:
            report.errors.append(f"{(label or name or el_id)[:30]}: {exc}")

    for el in page.query_selector_all("input[role=combobox]"):
        try:
            if not el.is_visible() or (el.input_value() or "").strip():
                continue
        except Exception:
            continue
        label, name, el_id, placeholder = _describe(el)
        blob = _normalize(label, name, el_id, placeholder)
        target = None
        if re.search(r"^start.{0,10}month", blob, re.IGNORECASE):
            target = start_month
        elif re.search(r"^end.{0,10}month", blob, re.IGNORECASE):
            target = end_month
        if not target:
            continue
        try:
            if _search_select(page, el, target):
                report.filled[(label or name or el_id)[:24]] = target
        except Exception as exc:
            report.errors.append(f"{(label or name or el_id)[:30]}: {exc}")

    for el in page.query_selector_all("input[type=number]"):
        try:
            if not el.is_visible() or (el.input_value() or "").strip():
                continue
        except Exception:
            continue
        label, name, el_id, placeholder = _describe(el)
        blob = _normalize(label, name, el_id, placeholder)
        target = None
        if re.search(r"^start.{0,10}year", blob, re.IGNORECASE):
            target = start_year
        elif re.search(r"^end.{0,10}year", blob, re.IGNORECASE):
            target = end_year
        if not target:
            continue
        try:
            el.fill(target)
            report.filled[(label or name or el_id)[:24]] = target
        except Exception as exc:
            report.errors.append(f"{(label or name or el_id)[:30]}: {exc}")


def _fill_typeahead(page, profile: Profile, report: FillReport) -> None:
    """Answer a type-ahead combobox from the profile's custom answers.

    The existing combobox pass only handles a fixed, deliberately short list
    (country, work authorization, sponsorship) so that demographic dropdowns
    are never answered by accident. Ashby renders "How did you hear about
    Render?" the same way, and it is required, so an otherwise complete form
    could not be submitted. Same guard applies: an answer has to exist in the
    profile for this question, and the option has to exist in the list.
    """
    try:
        boxes = page.query_selector_all("input[role=combobox], [role=combobox] input")
    except Exception:
        return

    for el in boxes:
        try:
            if not el.is_visible() or (el.input_value() or "").strip():
                continue
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        question = label or _group_question(el)
        if not question or is_protected(question, name, el_id):
            continue

        answer = custom_answer(profile, question)
        if not answer:
            continue

        try:
            # Exactly one list open at a time, so the document-wide fallback
            # below cannot pick an option belonging to another dropdown.
            close_open_dropdowns(page)
            _open_dropdown(page, el)
            el.fill(answer)
            page.wait_for_timeout(700)
            options = _options_for(page, el)
            if not options:
                try:
                    options = [
                        o for o in page.query_selector_all("[role=option]")
                        if o.is_visible()
                    ]
                except Exception:
                    options = []
            chosen = None
            for option in options:
                text = (option.inner_text() or "").strip()
                if not text:
                    continue
                if text.lower() == answer.lower():
                    chosen = option
                    break
                if chosen is None and answer.lower() in text.lower():
                    chosen = option
            if chosen is None:
                report.unmatched.append(f"{question[:56]} (no option {answer!r})")
                continue
            chosen.click(timeout=6000)
            report.filled[question[:44]] = answer
        except Exception as exc:
            report.errors.append(f"{question[:40]}: {exc}")


def _fill_button_pairs(page, profile: Profile, report: FillReport) -> None:
    """Answer Yes/No questions rendered as a pair of buttons.

    Ashby draws work authorization and sponsorship as two styled <button>
    elements over a hidden checkbox. Nothing in the ordinary passes sees them:
    they are not inputs, not selects, and not comboboxes, so both questions
    came back required on a form that was otherwise complete.
    """
    try:
        buttons = page.query_selector_all("button")
    except Exception:
        return

    handled: set[str] = set()
    for el in buttons:
        try:
            if not el.is_visible():
                continue
            text = (el.inner_text() or "").strip()
        except Exception:
            continue
        if text.lower() not in ("yes", "no"):
            continue

        question = _group_question(el)
        if not question or question in handled:
            continue
        if never_fill(question):
            continue

        wanted = custom_answer(profile, question)
        if not wanted:
            key, answer = agreement_answer(profile, question)
            wanted = answer if key else None
        if not wanted or wanted.strip().lower() not in ("yes", "no"):
            continue
        if wanted.strip().lower() != text.lower():
            continue

        try:
            el.click(timeout=4000)
            handled.add(question)
            report.filled[question[:44]] = text
        except Exception as exc:
            report.errors.append(f"{question[:40]}: {exc}")


def _fill_radio_groups(page, profile: Profile, report: FillReport) -> None:
    """Answer radio-button questions, EEO ones included.

    Radios grouped by `name` — Ashby's EEO fields are named
    `..._systemfield_eeoc_gender` and friends, which is a more reliable signal
    than the visible text, since each option's label reads like its own
    question.
    """
    try:
        radios = page.query_selector_all("input[type=radio]")
    except Exception:
        return

    groups: dict[str, list] = {}
    for el in radios:
        try:
            if not el.is_visible():
                continue
            name = el.get_attribute("name") or ""
        except Exception:
            continue
        groups.setdefault(name, []).append(el)

    for name, options in groups.items():
        try:
            if any(o.is_checked() for o in options):
                continue
        except Exception:
            continue

        labels = [_own_label(o) for o in options]
        question = _group_question(options[0]) or name
        haystack = f"{name} {question}"

        answer = None
        key = eeo_key(haystack)
        if key:
            key, answer = eeo_answer(profile, haystack)
            if not answer:
                report.eeo_left_blank.append(key)
                continue
        else:
            if never_fill(haystack):
                continue
            answer = custom_answer(profile, question)

        if not answer and not key:
            # "Rate your proficiency with Go and TypeScript" is answerable from
            # what you have written, but only in one direction — see
            # evidence.answer_level.
            from jobbot import evidence as evidence_mod

            picked, note = evidence_mod.answer_level(
                evidence_mod.load(), question, [l for l in labels if l]
            )
            if picked:
                answer = picked
            elif note:
                report.unmatched.append(f"{question[:56]} — {note}")
                continue

        if not answer:
            # An unanswered radio group is usually required. Saying so beats
            # letting a silent skip look like a filled form.
            report.unmatched.append(f"{question[:70]} (radio, unanswered)")
            continue

        target = None
        for option, label in zip(options, labels):
            if not label:
                continue
            if label.strip().lower() == answer.strip().lower():
                target = option
                break
        if target is None:
            for option, label in zip(options, labels):
                if label and answer.strip().lower() in label.strip().lower():
                    target = option
                    break
        if target is None:
            report.unmatched.append(f"{question[:60]} (no option matches {answer!r})")
            continue

        try:
            target.check(timeout=4000)
            if key:
                report.eeo_answered[key] = answer
            else:
                report.filled[question[:44]] = answer
        except Exception as exc:
            report.errors.append(f"{question[:40]}: {exc}")


def _group_question(el) -> str:
    """The question a checkbox belongs to, not just its own label.

    A checkbox reading "Other" says nothing on its own. What matters is the
    fieldset it sits in — "How did you hear about Twilio?" — so the answer in
    the profile can be matched against the question rather than the option.
    """
    try:
        return el.evaluate(
            """(node) => {
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                let cur = node;
                for (let depth = 0; cur && depth < 6; depth++) {
                    cur = cur.parentElement;
                    if (!cur) break;
                    const legend = cur.querySelector('legend, [class*=label], label');
                    if (legend) {
                        const t = clean(legend.textContent);
                        if (t.length > 8 && t.length < 300) return t;
                    }
                    const aria = cur.getAttribute && cur.getAttribute('aria-label');
                    if (aria && aria.length > 8) return clean(aria);
                }
                return '';
            }"""
        ) or ""
    except Exception:
        return ""


def _own_label(el) -> str:
    try:
        return el.evaluate(
            """(node) => {
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                if (node.id) {
                    const l = document.querySelector(`label[for="${CSS.escape(node.id)}"]`);
                    if (l) return clean(l.textContent);
                }
                const wrap = node.closest('label');
                if (wrap) return clean(wrap.textContent);
                const sib = node.nextElementSibling;
                if (sib) return clean(sib.textContent);
                return clean(node.getAttribute('aria-label') || node.value || '');
            }"""
        ) or ""
    except Exception:
        return ""


def _fill_checkbox_groups(page, profile: Profile, report: FillReport) -> None:
    """Answer checkbox-list questions like "How did you hear about us?".

    The dropdown version of this question was already handled; the checkbox
    version was not, so a required "click all that apply" list blocked the
    whole submission with everything else on the form filled in.
    """
    try:
        boxes = page.query_selector_all("input[type=checkbox]")
    except Exception:
        return

    for el in boxes:
        try:
            if not el.is_visible() or el.is_checked():
                continue
        except Exception:
            continue

        option = _own_label(el)
        question = _group_question(el)
        if not option or not question:
            continue
        if never_fill(question, option) or agreement_key(question):
            continue  # consent boxes belong to the agreements pass

        wanted = custom_answer(profile, question)
        if not wanted:
            continue
        if option.strip().lower() != wanted.strip().lower():
            continue
        try:
            el.check(timeout=4000)
            report.filled[question[:40]] = option
        except Exception as exc:
            report.errors.append(f"{question[:40]}: {exc}")


def _fill_agreements(page, profile: Profile, report: FillReport) -> None:
    """Tick the consent boxes the profile has an explicit answer for.

    Nothing here is decided by the tool. A box is ticked only when
    `profile.agreements` names that kind of consent and says yes, which is you
    having made the decision once in a file you control rather than twenty
    times in a browser. Anything unanswered is reported and left.
    """
    answers = getattr(profile, "agreements", None) or {}

    try:
        boxes = page.query_selector_all("input[type=checkbox]")
    except Exception:
        boxes = []

    for el in boxes:
        try:
            if not el.is_visible() or el.is_checked():
                continue
        except Exception:
            continue

        label = f"{_own_label(el)} {_group_question(el)}".strip()
        if not label:
            continue
        key, answer = agreement_answer(profile, label)
        if not key:
            continue
        if not answer:
            report.agreements_left.append(f"{key}: {label[:70]}")
            continue
        if answer.strip().lower() not in {"yes", "agree", "i agree", "acknowledge", "true"}:
            # An explicit no on a checkbox means leaving it unticked.
            report.agreements_ticked[key] = f"left unticked ({answer})"
            continue
        try:
            el.check(timeout=4000)
            report.agreements_ticked[key] = f"ticked: {label[:60]}"
        except Exception as exc:
            report.errors.append(f"{label[:40]}: {exc}")

    _fill_agreement_selects(page, profile, report)

    for key in answers:
        if key not in AGREEMENT_FIELDS:
            report.errors.append(
                f"profile.agreements has unknown key {key!r}; "
                f"expected one of {sorted(AGREEMENT_FIELDS)}"
            )


def _fill_agreement_selects(page, profile: Profile, report: FillReport) -> None:
    """The same consents, when the form asks them as a dropdown instead."""
    try:
        selects = page.query_selector_all("select")
    except Exception:
        return

    for el in selects:
        try:
            if not el.is_visible():
                continue
            if (el.input_value() or "").strip():
                continue
        except Exception:
            continue

        label, name, el_id, _ = _describe(el)
        key, answer = agreement_answer(profile, label, name, el_id)
        if not key:
            continue
        if not answer:
            report.agreements_left.append(f"{key}: {(label or name)[:70]}")
            continue
        try:
            options = el.query_selector_all("option")
            for option in options:
                text = (option.inner_text() or "").strip()
                if not text:
                    continue
                if _agreement_option_matches(text, answer):
                    el.select_option(value=option.get_attribute("value") or text)
                    report.agreements_ticked[key] = f"{(label or name)[:40]} -> {text}"
                    break
        except Exception as exc:
            report.errors.append(f"{(label or name)[:40]}: {exc}")


_YES_WORDS = ("yes", "i agree", "agree", "acknowledge", "accept", "i consent", "opt in")
_NO_WORDS = ("no", "decline", "disagree", "opt out", "do not")


def _agreement_option_matches(option_text: str, answer: str) -> bool:
    text = option_text.strip().lower()
    want = answer.strip().lower()
    if text == want:
        return True
    if want in _YES_WORDS or want in ("true",):
        return any(text.startswith(w) for w in _YES_WORDS)
    if want in _NO_WORDS or want in ("false",):
        return any(text.startswith(w) for w in _NO_WORDS)
    return want in text


def _fill_selects(page, profile: Profile, report: FillReport) -> None:
    """Handle the few dropdowns that have an unambiguous answer.

    Country and work authorization only. Anything else — including every
    protected question, which is where dropdowns mostly live on these forms —
    is left for you.
    """
    choices = [
        (r"\bcountry\b", ["United States", "United States of America", "USA", "US"]),
        (r"authorized to work|work authorization|legally authorized",
         ["Yes"] if profile.work_authorized else ["No"]),
        (r"require sponsorship|need sponsorship|visa sponsorship|"
         r"sponsor (?:you|me)\b|sponsor.{0,25}work permit",
         ["No"] if not profile.requires_sponsorship else ["Yes"]),
    ]

    for el in page.query_selector_all("select"):
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        if is_protected(label, name, el_id, placeholder):
            report.skipped_protected.append((label or name or el_id)[:70])
            continue

        try:
            if (el.input_value() or "").strip():
                continue
        except Exception:
            pass

        blob = f"{label} {name} {el_id} {placeholder}"
        for pattern, options in choices:
            if not re.search(pattern, blob, re.IGNORECASE):
                continue
            for option in options:
                try:
                    el.select_option(label=option)
                    report.filled[(label or name or "select")[:20]] = option
                    break
                except Exception:
                    continue
            break


def _unambiguous_choices(profile: Profile) -> list[tuple[str, str]]:
    """Dropdown questions with exactly one correct answer from the profile."""
    return [
        (r"\bcountry\b", "United States"),
        (r"authorized to work|work authorization|legally authorized",
         "Yes" if profile.work_authorized else "No"),
        (r"require sponsorship|need sponsorship|visa sponsorship|"
         r"sponsor (?:you|me)\b|sponsor.{0,25}work permit",
         "Yes" if profile.requires_sponsorship else "No"),
    ]


def _fill_comboboxes(page, profile: Profile, report: FillReport) -> None:
    """Fill react-select style comboboxes.

    Greenhouse renders these for country, work authorization, AND every
    demographic question — 14 of them on a single form. The protected check
    runs first for that reason: getting this wrong means auto-answering an
    EEO question.
    """
    targets = _unambiguous_choices(profile)

    for el in page.query_selector_all("input[role=combobox]"):
        try:
            if not el.is_visible() or not el.is_editable():
                continue
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        blob = f"{label} {name} {el_id} {placeholder}"

        if is_protected(label, name, el_id, placeholder):
            report.skipped_protected.append((label or name or el_id or "dropdown")[:70])
            continue

        try:
            if (el.input_value() or "").strip():
                continue
        except Exception:
            pass

        value = next((v for p, v in targets if re.search(p, blob, re.IGNORECASE)), None)
        if not value:
            continue

        try:
            _open_dropdown(page, el)
            el.fill(value)
            page.wait_for_timeout(500)

            options = _options_for(page, el)

            def option_text(opt) -> str:
                # The country picker doubles as a phone dial-code selector, so
                # its options read "United States +1". Strip the code before
                # comparing or nothing ever matches exactly.
                raw = (opt.inner_text() or "").strip()
                return re.sub(r"\s*\+\d[\d\s-]*$", "", raw).strip()

            chosen = next(
                (o for o in options if option_text(o).lower() == value.lower()), None
            )
            if chosen is None:
                # Unique prefix match: "United States" must not silently pick
                # "United States Minor Outlying Islands".
                prefixed = [
                    o for o in options
                    if option_text(o).lower().startswith(value.lower())
                ]
                if len(prefixed) == 1:
                    chosen = prefixed[0]

            if chosen is not None:
                chosen.click(timeout=2000)
                report.filled[(label or el_id or "dropdown")[:20]] = value
            else:
                report.unmatched.append(f"{(label or el_id)[:40]} (no option matched)")
        except Exception as exc:
            report.errors.append(f"{(label or el_id)[:30]} dropdown: {exc}")


# Forms phrase these answers at length ("I am not a protected veteran",
# "I don't wish to answer"). Map a short profile answer onto the wording used.
EEO_ANSWER_ALIASES: dict[str, list[str]] = {
    # Forms spell "No" as a whole sentence more often than as the word:
    # "I have not previously been employed at Affirm".
    "no": [r"^no$", r"\bi am not\b", r"\bnot a protected veteran\b",
           r"\bno,? i (?:am|do) not\b", r"^not hispanic",
           r"^no,? i do not have", r"^i have not\b", r"^i do not\b",
           r"^i haven'?t\b", r"^i don'?t\b",
           # Veteran lists phrase the negative several more ways, and a miss
           # here leaves a required EEO dropdown blank.
           r"^i am not a (?:protected )?veteran", r"not a veteran",
           r"^no,? i am not a", r"identify as not"],
    "yes": [r"^yes$", r"\bi am one or more\b", r"\bi identify as\b",
            r"^yes,? i"],
    "male": [r"^male$", r"^man$"],
    "female": [r"^female$", r"^woman$"],
    # "I prefer to self-describe" is deliberately NOT here. It is not a
    # decline: picking it reveals a free-text "Please specify" box, so it
    # turns one answered question into one answered question plus a new
    # required blank — which is how a disability field came back as
    # "I prefer to self-describe" with an empty specify box beneath it.
    "decline": [r"decline", r"prefer not", r"don'?t wish", r"do not wish",
                r"do not want to answer", r"don'?t want to answer",
                r"choose not to (?:answer|disclose)", r"not (?:to )?disclose"],
    "black or african american": [r"black", r"african american"],
    "two or more races": [r"two or more", r"multiracial", r"multi[- ]racial"],
    # Two shapes for one answer: an orientation list (pick "Bisexual") and a
    # binary "member of the LGBT2QIA+ community?" (pick "Yes"). Listing both
    # lets the same profile value answer either without a second setting.
    "bisexual": [r"^bisexual", r"bisexual", r"^yes$", r"\bi identify as\b"],
    "cisgender": [r"^cisgender", r"cisgender", r"^no$", r"not transgender"],
    # Pronoun lists are written "He/him/his", not "He/Him".
    "he/him": [r"^he\s*/\s*him"],
    "she/her": [r"^she\s*/\s*her"],
    "they/them": [r"^they\s*/\s*them"],
}


def _open_dropdown(page, el) -> None:
    """Scroll a dropdown into view and open it.

    These pages animate on load, and Playwright refuses to click an element it
    considers unstable or outside the viewport — which is how the country
    field, sitting below the fold, failed with a click timeout every run.
    """
    try:
        el.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    page.wait_for_timeout(150)
    el.click(timeout=8000)


def _options_for(page, el):
    """Options belonging to THIS combobox, never another one on the page.

    `page.query_selector_all("[role=option]")` returns every option rendered
    anywhere, so with two dropdowns open it can hand back the country list
    while you are filling an ethnicity field. react-select links the input to
    its listbox through aria-controls / aria-owns; scope to that, and fall back
    to the nearest wrapper rather than to the whole document.
    """
    for attr in ("aria-controls", "aria-owns"):
        try:
            target_id = el.get_attribute(attr)
        except Exception:
            target_id = None
        if target_id:
            try:
                container = page.query_selector(f'#{target_id}')
                if container:
                    return container.query_selector_all("[role=option]")
            except Exception:
                pass

    # Fall back to the enclosing select wrapper, not the document.
    try:
        handle = el.evaluate_handle(
            """e => e.closest('[class*="select"]')
                   || e.closest('[data-testid]')
                   || e.parentElement"""
        )
        container = handle.as_element()
        if container:
            found = container.query_selector_all("[role=option]")
            if found:
                return found
    except Exception:
        pass
    return []


def _eeo_option_matches(option_text: str, answer: str) -> bool:
    """Whether a dropdown option represents the configured answer."""
    text = (option_text or "").strip().lower()
    want = answer.strip().lower()
    if not text:
        return False
    if text == want:
        return True
    for patterns in (EEO_ANSWER_ALIASES.get(want) or []):
        if re.search(patterns, text, re.IGNORECASE):
            return True
    return False


LOCATION_PATTERN = r"location\s*\(city\)|^location$|^city$|city.{0,12}location"


def _fill_location(page, profile: Profile, report: FillReport) -> None:
    """Fill a geo typeahead like Greenhouse's "Location (City)".

    These resolve free text against a place database and only accept a value
    picked from their own suggestions, so typing "Los Angeles, CA" and moving
    on leaves the field empty and the form invalid. Type the city, then choose
    the suggestion that also names the state.
    """
    if not profile.city:
        return

    state_terms = [t for t in (profile.state, _STATE_NAMES.get(profile.state.upper())) if t]

    for el in page.query_selector_all("input[role=combobox], input[type=text]"):
        try:
            if not el.is_visible() or not el.is_editable():
                continue
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        if never_fill(label, name, el_id, placeholder):
            continue
        if not re.search(LOCATION_PATTERN, _normalize(label, name, el_id), re.IGNORECASE):
            continue

        try:
            if (el.input_value() or "").strip():
                continue
        except Exception:
            pass

        try:
            _open_dropdown(page, el)
            el.fill(profile.city)
            page.wait_for_timeout(900)  # geo lookups are a round trip

            options = _options_for(page, el)
            if not options:
                report.unmatched.append(f"{label or 'Location'} (no suggestions)")
                continue

            def text_of(o) -> str:
                return (o.inner_text() or "").strip()

            city_lc = profile.city.lower()
            preferred = [
                o for o in options
                if city_lc in text_of(o).lower()
                and any(s.lower() in text_of(o).lower() for s in state_terms)
            ]
            chosen = preferred[0] if preferred else next(
                (o for o in options if city_lc in text_of(o).lower()), None
            )
            if chosen is None:
                report.unmatched.append(f"{label or 'Location'} (no match for {profile.city})")
                continue

            picked = text_of(chosen)
            chosen.click(timeout=6000)
            report.filled[(label or "Location")[:20]] = picked[:45]
        except Exception as exc:
            report.errors.append(f"{(label or 'Location')[:30]}: {exc}")


_STATE_NAMES = {
    "CA": "California", "NY": "New York", "TX": "Texas", "WA": "Washington",
    "MA": "Massachusetts", "IL": "Illinois", "CO": "Colorado", "GA": "Georgia",
    "FL": "Florida", "OR": "Oregon", "PA": "Pennsylvania", "NC": "North Carolina",
    "VA": "Virginia", "NJ": "New Jersey", "AZ": "Arizona", "UT": "Utah",
    "MN": "Minnesota", "OH": "Ohio", "MI": "Michigan", "TN": "Tennessee",
    "DC": "District of Columbia",
}


def _fill_eeo(page, profile: Profile, report: FillReport, passes: int = 3) -> None:
    """Answer the EEO questions the profile configures, repeatedly.

    These forms reveal fields progressively — Greenhouse renders the race
    question only once the Hispanic/Latino one is answered — so a single pass
    fills ethnicity and then reports race as absent. Repeat until a pass adds
    nothing.
    """
    for _ in range(passes):
        before = len(report.eeo_answered)
        _fill_eeo_once(page, profile, report)
        if len(report.eeo_answered) == before:
            return
        page.wait_for_timeout(500)


def _fill_eeo_once(page, profile: Profile, report: FillReport) -> None:
    """One sweep over the EEO controls currently on the page.

    A blank or absent profile entry means the field is skipped and reported,
    every time. That is the mechanism that keeps "I'll decide when I submit"
    working rather than quietly defaulting to something.
    """
    controls = page.query_selector_all("input[role=combobox], select")

    for el in controls:
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        if never_fill(label, name, el_id, placeholder):
            continue

        key, answer = eeo_answer(profile, label, name, el_id, placeholder)
        if not key or key in report.eeo_answered:
            continue
        if not answer:
            report.eeo_left_blank.append(key)
            continue

        try:
            if (el.input_value() or "").strip():
                continue
        except Exception:
            pass

        try:
            tag = el.evaluate("e => e.tagName").lower()
            if tag == "select":
                options = el.query_selector_all("option")
                target = next(
                    (o for o in options
                     if _eeo_option_matches(o.inner_text(), answer)), None
                )
                if target is None:
                    report.unmatched.append(f"{key} (no option matched {answer!r})")
                    continue
                el.select_option(value=target.get_attribute("value"))
            else:
                _open_dropdown(page, el)
                page.wait_for_timeout(500)
                options = _options_for(page, el)
                if not options:
                    report.unmatched.append(f"{key} (no options found)")
                    el.press("Escape")
                    continue
                target = next(
                    (o for o in options
                     if _eeo_option_matches(o.inner_text(), answer)), None
                )
                if target is None:
                    report.unmatched.append(f"{key} (no option matched {answer!r})")
                    el.press("Escape")
                    continue
                target.click(timeout=6000)

            report.eeo_answered[key] = answer
        except Exception as exc:
            report.errors.append(f"{key}: {exc}")


def _fill_custom(page, profile: Profile, report: FillReport) -> None:
    """Answer the recurring custom questions from the profile's map.

    These are the ones every form asks in its own words — preferred name,
    pronouns, sponsorship, state of residence, how you heard about them.
    Nothing in NEVER_FILL is reachable from here, and EEO questions are handled
    by their own pass so a custom pattern can't quietly answer one.
    """
    controls = page.query_selector_all(
        "input[type=text], input[type=url], input:not([type]), textarea, "
        "input[role=combobox], select"
    )

    for el in controls:
        try:
            if not el.is_visible() or not el.is_editable():
                continue
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        if never_fill(label, name, el_id, placeholder):
            continue
        if eeo_key(label, name, el_id, placeholder):
            continue  # the EEO pass owns these

        answer = custom_answer(profile, label, name, el_id, placeholder)
        if not answer:
            continue

        try:
            if (el.input_value() or "").strip():
                continue
        except Exception:
            pass

        descriptor = (label or name or el_id or "field").strip()[:40]
        try:
            tag = el.evaluate("e => e.tagName").lower()
            role = (el.get_attribute("role") or "").lower()

            if tag == "select":
                target = next(
                    (o for o in el.query_selector_all("option")
                     if _eeo_option_matches(o.inner_text(), answer)), None
                )
                if target is None:
                    report.unmatched.append(f"{descriptor} (no option for {answer!r})")
                    continue
                el.select_option(value=target.get_attribute("value"))
            elif role == "combobox":
                _open_dropdown(page, el)
                page.wait_for_timeout(450)
                options = _options_for(page, el)
                target = next(
                    (o for o in options
                     if _eeo_option_matches(o.inner_text(), answer)), None
                )
                if target is None:
                    report.unmatched.append(f"{descriptor} (no option for {answer!r})")
                    el.press("Escape")
                    continue
                target.click(timeout=6000)
            else:
                el.fill(answer)

            report.filled[descriptor] = answer
        except Exception as exc:
            report.errors.append(f"{descriptor}: {exc}")


def _report_blank_eeo(page, profile: Profile, report: FillReport) -> None:
    """Final sweep for EEO questions still unanswered.

    Runs last because these forms reveal fields progressively — Greenhouse only
    renders the race question after the Hispanic/Latino one is answered, so a
    single pass reports it as absent and you never learn it is waiting.
    """
    for el in page.query_selector_all("input[role=combobox], select"):
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        key = eeo_key(label, name, el_id, placeholder)
        if not key or key in report.eeo_answered:
            continue

        try:
            value = (el.input_value() or "").strip()
        except Exception:
            value = ""
        if not value:
            report.eeo_left_blank.append(key)


def _attach_resume(page, profile: Profile, report: FillReport) -> None:
    resume = Path(profile.resume_path)
    if not resume.exists():
        report.errors.append(f"resume not found at {resume}")
        return

    for el in page.query_selector_all("input[type=file]"):
        label, name, el_id, placeholder = _describe(el)
        blob = f"{label} {name} {el_id}".lower()
        if "cover" in blob and "resume" not in blob:
            continue  # that one wants a cover letter file, not the resume
        try:
            el.set_input_files(str(resume))
            report.resume_attached = True
            return
        except Exception as exc:
            report.errors.append(f"resume upload: {exc}")


def _cover_letter_reveal_button(page):
    """The "Enter manually" button belonging to the Cover Letter section.

    Greenhouse renders an identical Attach / Dropbox / Google Drive / Enter
    manually group for both Resume and Cover Letter. Clicking the first match
    on the page opens the resume editor instead, which is why this walks up
    from each candidate button to find the block that mentions a cover letter.
    """
    candidates = page.query_selector_all("button, [role=button]")
    for el in candidates:
        try:
            text = (el.inner_text() or "").strip().lower()
        except Exception:
            continue
        if not re.search(r"enter manually|type|paste|write", text):
            continue
        # Collect every ancestor's text rather than the first non-trivial one.
        # The section heading sits about five levels up; the nearer parents
        # just repeat the button's own label, so stopping early always picked
        # the resume uploader.
        try:
            ancestors = el.evaluate(
                """e => {
                    let n = e; const out = [];
                    for (let i = 0; i < 7 && n; i++) {
                        n = n.parentElement;
                        if (n) out.push(n.innerText || '');
                    }
                    return out;
                }"""
            )
        except Exception:
            ancestors = []

        for text in ancestors:
            is_cover = re.search(r"cover ?letter", text, re.IGNORECASE)
            is_resume = re.search(r"resume\s*/?\s*cv|\bresume\b", text, re.IGNORECASE)
            if is_cover and not is_resume:
                return el
            if is_resume and not is_cover:
                break  # this button belongs to the resume uploader
    return None


# Open-ended prompts that the cover letter answers. Railway asks "why", Vooma
# asks for context, Agave asks what interests you — all of them are the letter,
# and all of them were being left blank because they are pre-existing textareas
# with no "cover letter" in the label.
_OPEN_PROMPT = re.compile(
    r"why (?:do you|are you|this|us\b|join)|what interests you|"
    r"tell us (?:about|why)|anything else|additional (?:info|information|"
    r"context)|what draws you|motivat|your context|why railway|why are you "
    r"excited",
    re.IGNORECASE,
)

# Prompts that ask for a specific short length. A 280-word letter dropped into
# "in two sentences" reads worse than a blank box, so these are reported for a
# human rather than answered.
_BREVITY = re.compile(
    r"in (?:one|two|three|1|2|3) sentences?|\bbriefly\b|a few words|"
    r"(?:max(?:imum)?|no more than|under)\s*\d+\s*(?:words|characters)|"
    r"\d+\s*words? or (?:less|fewer)",
    re.IGNORECASE,
)


def _fill_open_prompts(page, letter: str, report: FillReport) -> None:
    """Answer open-ended essay boxes with the cover letter.

    Separate from `_paste_cover_letter`, which only ever fills a textarea that
    appeared after the reveal click or one that says "cover letter" outright.
    A form that asks "Why do you want to work at Railway?" in a box that was
    always on the page got nothing.
    """
    if not letter:
        return
    for el in page.query_selector_all("textarea"):
        try:
            if not el.is_visible() or not el.is_editable():
                continue
            if (el.input_value() or "").strip():
                continue
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        blob = f"{label} {name} {el_id} {placeholder}"
        if is_protected(label, name, el_id, placeholder):
            continue
        if not _OPEN_PROMPT.search(blob):
            continue
        if _BREVITY.search(blob):
            report.skipped.append(
                f"{label or name or el_id}: asks for a short answer — the "
                "letter would be too long, write this one yourself"
            )
            continue
        try:
            el.fill(letter)
            report.filled[label or name or el_id] = "cover letter"
        except Exception as exc:
            report.errors.append(f"open prompt {label or name}: {exc}")


def _paste_cover_letter(page, letter: str, report: FillReport) -> None:
    """Reveal the cover letter editor and paste into it.

    The revealed textarea often carries no useful name or label, so rather
    than matching on attributes this records which textareas existed before
    the click and fills whichever one is new.
    """
    before = set()
    for el in page.query_selector_all("textarea"):
        try:
            before.add(el.evaluate("e => e.id || e.name || e.outerHTML.slice(0, 80)"))
        except Exception:
            pass

    button = _cover_letter_reveal_button(page)
    if button:
        try:
            button.click(timeout=3000)
            page.wait_for_timeout(600)
        except Exception as exc:
            report.errors.append(f"cover letter reveal: {exc}")

    for el in page.query_selector_all("textarea"):
        try:
            if not el.is_visible() or not el.is_editable():
                continue
            if (el.input_value() or "").strip():
                continue
            ident = el.evaluate("e => e.id || e.name || e.outerHTML.slice(0, 80)")
        except Exception:
            continue

        label, name, el_id, placeholder = _describe(el)
        if is_protected(label, name, el_id, placeholder):
            continue

        named = re.search(
            r"cover ?letter", f"{label} {name} {el_id} {placeholder}", re.IGNORECASE
        )
        if not named and ident in before:
            continue  # pre-existing textarea for some other question

        try:
            el.fill(letter)
            report.cover_letter_pasted = True
            return
        except Exception as exc:
            report.errors.append(f"cover letter: {exc}")

"""Eligibility gate detection.

This is the module that matters most for a candidate without a completed
degree. Most new-grad and internship postings carry an automated knockout
question — "are you currently enrolled?", "will you graduate by June 2027?" —
and an application that fails it is rejected before a human reads it.

Rather than guess, we classify each posting into an explicit verdict and keep
the evidence string that produced it, so the decision is auditable in the
review queue instead of being a black-box score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Gate(str, Enum):
    """How reachable a posting is for someone without a finished degree."""

    OPEN = "open"                    # no degree language at all
    EQUIVALENT_OK = "equivalent_ok"  # degree "or equivalent experience" — best targets
    SOFT_DEGREE = "soft_degree"      # degree "preferred" / "nice to have"
    HARD_DEGREE = "hard_degree"      # degree "required"
    ENROLLMENT = "enrollment"        # must be currently enrolled / graduating by date
    ADVANCED = "advanced"            # MS/PhD required

    @property
    def rank(self) -> int:
        """Lower is better. Used for sorting the queue."""
        return _GATE_RANK[self]

    @property
    def label(self) -> str:
        return _GATE_LABEL[self]

    @property
    def worth_applying(self) -> bool:
        """Whether this posting belongs in the review queue by default."""
        return self in (Gate.OPEN, Gate.EQUIVALENT_OK, Gate.SOFT_DEGREE)


_GATE_RANK = {
    Gate.EQUIVALENT_OK: 0,
    Gate.OPEN: 1,
    Gate.SOFT_DEGREE: 2,
    Gate.HARD_DEGREE: 3,
    Gate.ENROLLMENT: 4,
    Gate.ADVANCED: 5,
}

_GATE_LABEL = {
    Gate.EQUIVALENT_OK: "Equivalent experience accepted",
    Gate.OPEN: "No degree language",
    Gate.SOFT_DEGREE: "Degree preferred, not required",
    Gate.HARD_DEGREE: "Degree required",
    Gate.ENROLLMENT: "Must be currently enrolled",
    Gate.ADVANCED: "Advanced degree required",
}


# ── Patterns ───────────────────────────────────────────────────────────────────
# Ordered most-specific first; the first family to match wins.

# "or equivalent practical experience" — the phrase that opens a door.
EQUIVALENT_PATTERNS = [
    r"or equivalent (?:practical |work |industry |relevant )?experience",
    r"or equivalent (?:combination of )?(?:education|training)",
    r"equivalent (?:practical |relevant )?experience (?:in lieu|instead) of",
    r"in lieu of a? ?degree",
    r"degree or (?:comparable|equivalent|relevant) experience",
    # Reversed phrasing: "either equivalent practical experience or a
    # Bachelor's degree". Common at fintechs and just as much an opening.
    r"either equivalent (?:practical |work |relevant )?experience or",
    r"equivalent (?:practical |work |relevant )?experience or an? (?:bachelor|degree|b\.?s\.?)",
    # "...pursuing or having completed their Bachelor's degree or possessing
    # comparable data science qualifications". Found on the Massachusetts Life
    # Sciences Center program, which was classified soft_degree — the opening
    # is worded around "qualifications" rather than "experience", and a verb
    # sits between "or" and the adjective.
    r"or (?:possessing |holding |having )?(?:comparable|equivalent|similar)\b[\w ]{0,40}\bqualifications",
    r"we (?:do not|don'?t) require a (?:college )?degree",
    r"no degree (?:is )?(?:required|necessary)",
    r"(?:formal )?education (?:is not|isn'?t) a requirement",
    r"self-?taught (?:candidates|applicants|engineers) (?:are )?welcome",
    r"bootcamp (?:graduates|grads) (?:are )?(?:welcome|encouraged)",
]

# Currently-enrolled / graduation-window gates. These are the hard stops.
ENROLLMENT_PATTERNS = [
    r"currently enrolled",
    r"must be enrolled",
    r"actively enrolled",
    r"enrolled in an accredited",
    r"currently pursuing (?:a|an|your)",
    # "Pursuing a PhD in computer science" — Databricks' GenAI Research
    # Scientist Intern said exactly this and was classified OPEN, because the
    # alternation below listed every degree except the one that rules him out
    # hardest.
    r"pursuing (?:a|an|your) (?:bachelor|master|ph\.?\s?d|b\.?s\.?|m\.?s\.?|degree)",
    r"rising (?:sophomore|junior|senior)",
    r"graduat(?:e|ing) (?:in|by|between) \w+ ?\d{4}",
    # Samsara: "expected graduation no earlier than Summer 2028". The old
    # pattern allowed one word between the phrase and the year, so any
    # qualifier at all — "no earlier than", "on or after" — slipped past it.
    r"expected graduation.{0,40}\d{4}",
    r"class of \d{4}",
    r"return(?:ing)? to (?:school|university|campus)",
    r"must be a (?:current )?(?:student|undergraduate)",
    r"(?:degree|program) conferred (?:by|between)",
    r"available for a \d+[- ]week (?:summer )?internship",
]

# Advanced degree gates.
ADVANCED_PATTERNS = [
    r"ph\.?d\.?(?: degree)? (?:is )?(?:required|preferred|candidates)",
    r"(?:must have|requires?) a ph\.?d",
    r"master'?s degree (?:is )?required",
    r"m\.?s\.? (?:degree )?required",
    r"advanced degree required",
    r"doctoral degree",
]

# Bachelor's-required gates.
HARD_DEGREE_PATTERNS = [
    r"bachelor'?s? degree (?:is )?required",
    r"requires? a bachelor'?s",
    r"must (?:have|possess) a bachelor'?s",
    r"b\.?s\.?/?b\.?a\.? (?:degree )?required",
    r"degree in .{0,40} is required",
    r"required:? .{0,30}bachelor'?s degree",
    r"minimum (?:of )?a bachelor'?s",
    r"4[- ]year degree required",
    # "Bachelor's required" / "BS required" — the word "degree" is often dropped.
    r"bachelor'?s?\s+(?:is\s+)?required",
    r"\b(?:bs|ba)\b[^.\n]{0,25}\brequired\b",
]

# Softer phrasing — degree wanted but not gated.
SOFT_DEGREE_PATTERNS = [
    r"bachelor'?s degree (?:is )?preferred",
    r"degree (?:is )?(?:preferred|a plus|nice to have|desirable)",
    r"preferred:? .{0,30}degree",
    r"(?:bs|ba|b\.s\.|b\.a\.)\b.{0,60}(?:preferred|a plus)",
    # "A relevant advanced degree (Masters or PhD) in ML" stated as a wish
    # rather than a requirement. Worded loosely enough that the ADVANCED
    # patterns above (which need "required") correctly skip it.
    # Note this must not catch "a high degree of autonomy" — hence the
    # explicit qualifiers rather than a bare \bdegree\b.
    r"\b(?:advanced|graduate|master'?s|doctoral) degree\b",
]

# Generic degree mention with no strength marker — treated as soft.
DEGREE_MENTION_PATTERNS = [
    r"bachelor'?s",
    r"\bb\.?s\.?\b",
    r"\bb\.?a\.?\b",
    r"undergraduate degree",
    r"college degree",
    # "Degree in Data Science, Computer Science, or a related field" — no
    # strength marker and no "bachelor", but still degree language.
    r"\bdegree in\b",
    r"\bdegree\b.{0,30}\b(?:computer science|engineering|mathematics|statistics)\b",
]


# ── "We could not read this posting" ───────────────────────────────────────────
# Some boards only list a link, and some of those links render the posting in
# JavaScript, so no description text can be recovered (see enrich.py). Such a
# posting classifies as OPEN, because there is no degree language in text that
# does not exist — which reads identically to a posting we checked and cleared.
#
# It is not the same thing, and the difference matters more here than anywhere
# else: OPEN carries a scoring bonus, so an unreadable posting would outrank a
# verified one purely for being unknown. Enrichment stamps this marker into the
# description when it gives up, and the marker travels with the stored text so
# `rescore` reaches the same conclusion later.
UNVERIFIED_MARKER = "[posting text unavailable]"

UNVERIFIED_NOTE = (
    "Posting text could not be read, so no eligibility gate has been checked. "
    "This is 'not looked at', not 'no gate found' — open the link before "
    "spending an application on it."
)

# Stamped when the posting's own page answers 404/410 — the listing is gone.
# Curated boards go stale between refreshes: of the Braven board's 21 Delta
# postings, the three checked by hand were all closed. A dead link has no
# business in a queue whose entire purpose is to be worked through in order.
GONE_MARKER = "[posting no longer available]"


def gate_unverified(description: str) -> bool:
    return UNVERIFIED_MARKER in (description or "")


def posting_gone(description: str) -> bool:
    return GONE_MARKER in (description or "")


# The Braven board writes its own "Application deadline: YYYY-MM-DD" line into
# a posting's description (see braven.board_summary) so gating can read it back
# in the same pass that reads degree language. Nothing did, which is how the
# MLSC Data Science Internship stayed in the queue — score 45, letter written —
# a full day after its own listed deadline had passed. A posting you cannot
# submit to is exactly the same problem as one whose page 404s; it belongs in
# the same knockout, not a separate one nobody checks.
_DEADLINE_LINE = re.compile(r"application deadline:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def deadline_passed(description: str, today: date | None = None) -> bool:
    match = _DEADLINE_LINE.search(description or "")
    if not match:
        return False
    try:
        deadline = datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return False
    return deadline < (today or datetime.now().date())


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_EQUIVALENT = _compile(EQUIVALENT_PATTERNS)
_ENROLLMENT = _compile(ENROLLMENT_PATTERNS)
_ADVANCED = _compile(ADVANCED_PATTERNS)
_HARD = _compile(HARD_DEGREE_PATTERNS)
_SOFT = _compile(SOFT_DEGREE_PATTERNS)
_MENTION = _compile(DEGREE_MENTION_PATTERNS)


@dataclass
class GateVerdict:
    """Classification plus the text that justified it."""

    gate: Gate
    evidence: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.gate.label

    @property
    def worth_applying(self) -> bool:
        return self.gate.worth_applying

    def as_dict(self) -> dict:
        return {"gate": self.gate.value, "evidence": self.evidence}


def _excerpt(text: str, match: re.Match, width: int = 90) -> str:
    """Pull a readable snippet around a match for the review UI."""
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    snippet = text[start:end].replace("\n", " ")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


# Job descriptions are pasted out of word processors, so "Bachelor's" arrives
# with a curly apostrophe far more often than a straight one. Every pattern
# here spells it "'", so without normalizing, `bachelor'?s` silently fails to
# match `Bachelor’s` and the posting is classified as having no degree language
# at all. Same story for the various unicode dashes in "3–5 years".
_NORMALIZE = str.maketrans({
    "‘": "'", "’": "'", "ʼ": "'", "´": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ",
})


def normalize(text: str) -> str:
    """Fold smart punctuation and collapse whitespace before matching."""
    if not text:
        return ""
    return re.sub(r"[ \t]+", " ", text.translate(_NORMALIZE))


def _scan(text: str, patterns: list[re.Pattern], limit: int = 3) -> list[str]:
    found: list[str] = []
    for pat in patterns:
        for m in pat.finditer(text):
            found.append(_excerpt(text, m))
            if len(found) >= limit:
                return found
    return found


def classify(description: str, title: str = "") -> GateVerdict:
    """Classify a posting's eligibility gate.

    Precedence is deliberate: an explicit "or equivalent experience" clause
    outranks a nearby "bachelor's degree required", because that pairing is
    the single most common way a posting stays open to non-graduates. An
    enrollment requirement outranks everything else because it is the one
    gate that cannot be argued around.
    """
    text = normalize(f"{title}\n{description or ''}")
    if not text.strip():
        return GateVerdict(Gate.OPEN)

    # Enrollment gates are absolute — check first.
    if evidence := _scan(text, _ENROLLMENT):
        return GateVerdict(Gate.ENROLLMENT, evidence)

    # "or equivalent experience" beats a degree requirement in the same posting.
    if evidence := _scan(text, _EQUIVALENT):
        return GateVerdict(Gate.EQUIVALENT_OK, evidence)

    if evidence := _scan(text, _ADVANCED):
        return GateVerdict(Gate.ADVANCED, evidence)

    if evidence := _scan(text, _HARD):
        return GateVerdict(Gate.HARD_DEGREE, evidence)

    if evidence := _scan(text, _SOFT):
        return GateVerdict(Gate.SOFT_DEGREE, evidence)

    # A bare mention of a degree with no strength marker: treat as soft rather
    # than hard. Being over-cautious here hides reachable jobs.
    if evidence := _scan(text, _MENTION, limit=2):
        return GateVerdict(Gate.SOFT_DEGREE, evidence)

    # Nothing matched. Say why: an empty evidence list on a posting we never
    # managed to read is the most misleading output this module can produce.
    if gate_unverified(description):
        return GateVerdict(Gate.OPEN, [UNVERIFIED_NOTE])

    return GateVerdict(Gate.OPEN)


# ── Years of experience ────────────────────────────────────────────────────────

_YEARS_PATTERNS = [
    # "2+ years", "5 + years" — the plus sign alone marks it as a floor,
    # whatever noun follows ("2+ years building services").
    re.compile(r"(\d{1,2})\s*\+\s*years?", re.IGNORECASE),
    # "3-5 years", "3 to 5 years" — take the low end.
    re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*\d{1,2}\s*years?", re.IGNORECASE),
    # "3 years of professional experience", "3 years experience"
    re.compile(
        r"(\d{1,2})\s*years?(?:\s+of)?"
        r"(?:\s+(?:relevant|professional|industry|hands-on|practical|related))?"
        r"\s+experience",
        re.IGNORECASE,
    ),
    # "minimum of 4 years", "at least 4 years"
    re.compile(
        r"(?:minimum(?:\s+of)?|at least)\s+(\d{1,2})\s*years?", re.IGNORECASE
    ),
]


def min_years_required(description: str) -> int | None:
    """Smallest years-of-experience figure the posting asks for.

    Postings often list several ("2+ years Python, 5+ years distributed
    systems"); the minimum is the better signal of the floor to clear.
    """
    if not description:
        return None
    values: list[int] = []
    for pattern in _YEARS_PATTERNS:
        values.extend(int(m.group(1)) for m in pattern.finditer(description))
    values = [v for v in values if 0 < v <= 20]
    return min(values) if values else None

"""Work arrangement detection.

A posting whose location field reads "Seattle, WA, US; San Francisco, CA, US"
can still require you to live within commuting distance of one of those offices
and pay your own way there. The location string alone does not say so — the
requirement is buried in the body, in phrases like "commutable distance",
"[1x per week]", and "not eligible for relocation assistance".

For someone in Los Angeles applying to companies headquartered elsewhere, that
distinction decides whether an application is worth sending at all, so it gets
parsed explicitly rather than inferred from the location field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Worksite(str, Enum):
    REMOTE = "remote"      # genuinely location-independent
    HYBRID = "hybrid"      # some days in an office, must live near it
    ONSITE = "onsite"      # full-time in an office
    UNCLEAR = "unclear"    # nothing stated either way

    @property
    def label(self) -> str:
        return {
            Worksite.REMOTE: "Remote",
            Worksite.HYBRID: "Hybrid — must live near an office",
            Worksite.ONSITE: "Onsite",
            Worksite.UNCLEAR: "Not stated",
        }[self]


# ── Patterns ───────────────────────────────────────────────────────────────────

REMOTE_PATTERNS = [
    r"fully[- ]remote",
    r"100% remote",
    r"remote[- ]first",
    r"work from anywhere",
    r"\bremote\s*\((?:us|usa|united states)\)",
    r"remote\s*[-–]\s*(?:us|usa|united states)",
    r"this role is (?:fully )?remote",
    r"(?:completely|entirely) remote",
    r"remote friendly within the united states",
    r"we are a (?:fully )?(?:remote|distributed) (?:company|team)",
]

# Anything here means you must live within range of a specific office.
HYBRID_PATTERNS = [
    r"commutable distance",
    r"commuting distance",
    r"\bhybrid\b",
    r"\[?\d\s*x?\s*(?:days?|times?)?\s*per week\]?[^.]{0,40}(?:office|in[- ]person)",
    r"(?:office|in[- ]person)[^.]{0,40}\[?\d\s*x?\s*(?:days?|times?)?\s*per week",
    r"\d\s*days?\s*(?:a|per)\s*week\s*in\s*(?:the\s*)?office",
    r"in[- ]office requirement",
    r"expected to be in the office",
]

ONSITE_PATTERNS = [
    r"\bon[- ]?site\b",
    r"in[- ]person(?: role| position)",
    r"based (?:out of|in) our [a-z ]{3,25} office",
    r"5 days a week in (?:the )?office",
    r"this is not a remote (?:role|position)",
    r"no remote work",
]

# Checked BEFORE the offered list, so a posting that says both ("we offer
# relocation... relocation will not be provided for this role") reads as a
# refusal. That ordering is what makes it safe to widen the offered list.
NO_RELOCATION = [
    r"not eligible for relocation",
    # "will not be provided" and "cannot be provided" were both missed by the
    # old "(?:is )?not" form — 58 postings in the corpus phrase the refusal
    # that way, and every one of them would have been read as an offer once
    # the offered patterns below were widened.
    r"relocation (?:assistance|support|package|benefits?|expenses?)?\s*"
    r"(?:is|are|will|would|can)?\s*(?:not be|not|n't be)\s*"
    r"(?:provided|offered|available|covered|supported)",
    r"no relocation (?:assistance|package|support|benefits?|expenses?)",
    r"we (?:do not|don't|cannot|can't) (?:offer|provide|cover) relocation",
    r"unable to (?:offer|provide|cover) relocation",
    r"relocation (?:is |will )?not (?:be )?(?:eligible|possible)",
]

# Widened against the actual corpus: of 1,296 postings mentioning relocation,
# 870 were missed. The dominant miss was "…and offer relocation assistance to
# new employees", which the old `we (?:offer|provide) relocation` could not
# match because "we" is not adjacent to "offer".
RELOCATION_OFFERED = [
    r"relocation (?:assistance|package|support|benefits?|expenses?|stipend|bonus)",
    r"(?:offer|provide|cover|fund)(?:s|ing)? relocation",
    r"relocation (?:expense )?(?:coverage|reimbursement)",
    r"support(?:s|ing)? .{0,30}relocation",
    r"we (?:offer|provide) relocation",
]

_DAYS_RE = re.compile(
    r"\[?(\d)\s*x?\s*(?:days?|times?)?\s*(?:per|a)\s*week\]?", re.IGNORECASE
)


def _compile(patterns):
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_REMOTE = _compile(REMOTE_PATTERNS)
_HYBRID = _compile(HYBRID_PATTERNS)
_ONSITE = _compile(ONSITE_PATTERNS)
_NO_RELO = _compile(NO_RELOCATION)
_RELO_OK = _compile(RELOCATION_OFFERED)


@dataclass
class WorksiteVerdict:
    worksite: Worksite = Worksite.UNCLEAR
    days_in_office: int | None = None
    relocation_offered: bool | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        base = self.worksite.label
        if self.days_in_office:
            base += f" ({self.days_in_office}x/week)"
        if self.relocation_offered is False:
            base += ", no relocation help"
        elif self.relocation_offered is True:
            base += ", relocation offered"
        return base

    def as_dict(self) -> dict:
        return {
            "worksite": self.worksite.value,
            "days_in_office": self.days_in_office,
            "relocation_offered": self.relocation_offered,
            "evidence": self.evidence,
        }


def _first_match(text: str, patterns, width: int = 80) -> str | None:
    for pat in patterns:
        m = pat.search(text)
        if m:
            start = max(0, m.start() - width // 2)
            end = min(len(text), m.end() + width // 2)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            return snippet
    return None


def classify(description: str, location: str = "") -> WorksiteVerdict:
    """Determine the work arrangement a posting actually requires.

    Hybrid is checked before remote on purpose: postings routinely say
    "Remote - US" in the location field and then require one day a week in a
    named office in the body. The stricter requirement is the real one.
    """
    text = f"{location}\n{description or ''}"
    if not text.strip():
        return WorksiteVerdict()

    verdict = WorksiteVerdict()

    # Relocation is independent of arrangement; capture it either way.
    if _first_match(text, _NO_RELO):
        verdict.relocation_offered = False
    elif _first_match(text, _RELO_OK):
        verdict.relocation_offered = True

    if hybrid := _first_match(text, _HYBRID):
        verdict.worksite = Worksite.HYBRID
        verdict.evidence.append(hybrid)
        # Search the whole posting, not just the matched snippet: whichever
        # hybrid phrase matched first is often not the one carrying the count
        # ("commutable distance" here, "[1x per week]" two clauses earlier).
        for m in _DAYS_RE.finditer(text):
            days = int(m.group(1))
            if 1 <= days <= 7:
                verdict.days_in_office = days
                break
        return verdict

    if remote := _first_match(text, _REMOTE):
        verdict.worksite = Worksite.REMOTE
        verdict.evidence.append(remote)
        return verdict

    if onsite := _first_match(text, _ONSITE):
        verdict.worksite = Worksite.ONSITE
        verdict.evidence.append(onsite)
        return verdict

    # A bare "Remote" in the location field with nothing contradicting it.
    if re.search(r"\bremote\b", location or "", re.IGNORECASE):
        verdict.worksite = Worksite.REMOTE
        verdict.evidence.append(f"location: {location}")

    return verdict

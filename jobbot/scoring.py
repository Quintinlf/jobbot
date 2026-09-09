"""Fit scoring.

Replaces the flat keyword-counting in auto_apply_bot/app/jobs/score_jobs.py,
which gave "Head of Investor Relations" the same treatment as "ML Engineer"
because neither contained the word "python".

Scoring is deliberately transparent: every posting carries the list of reasons
that produced its number, so a bad ranking can be diagnosed rather than
guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobbot import config, skills as skills_mod, worksite as worksite_mod
from jobbot.gating import (
    Gate, GateVerdict, classify, deadline_passed, gate_unverified,
    min_years_required, posting_gone,
)
from jobbot.worksite import Worksite, WorksiteVerdict

# "Engineer I", "Engineer 1" — entry level, but easy to miss with substrings.
_LEVEL_ONE_RE = re.compile(r"\bengineer\s+(?:i|1)\b", re.IGNORECASE)

# A numbered level of II or higher. TITLE_KNOCKOUTS already catches the words —
# senior, staff, principal — but a numeral says the same thing about seniority
# and was sailing straight through: "Software Engineer II", "Delivery Engineer
# III" and "Data Scientist II" were all being offered as entry-reachable.
# I and 1 are deliberately absent: that is the entry rung, and it earns a bonus.
_MID_LEVEL_TOKENS = frozenset(
    {"ii", "iii", "iv", "v", "2", "3", "4", "5", "l2", "l3", "l4", "l5"}
)


@dataclass
class Score:
    total: int
    reasons: list[str] = field(default_factory=list)
    knockout: str | None = None

    @property
    def rejected(self) -> bool:
        return self.knockout is not None


def _level_knockout(title_lc: str) -> str | None:
    """A numbered level above entry, read off the end of the role name.

    Only the last word of the role proper is considered — everything before the
    first comma or bracket. That keeps "3D Reconstruction Engineer" and "Web3
    Infrastructure Engineer" out of it, where the numeral belongs to the
    subject rather than to a rung on a ladder.
    """
    head = title_lc.split(",")[0].split("(")[0].strip()
    words = head.split()
    if not words:
        return None
    last = words[-1].strip(".")
    if last in _MID_LEVEL_TOKENS and len(words) > 1:
        return f"numbered level above entry: {words[-1]!r}"

    # Some companies put the rung in brackets instead — Twilio posts "Software
    # Engineer (L2)". Matching the whole bracketed token against the same set
    # keeps "(Web3)" and "(AI)" out of it.
    for chunk in re.findall(r"\(([^)]*)\)", title_lc):
        if chunk.strip() in _MID_LEVEL_TOKENS:
            return f"numbered level above entry: {chunk.strip()!r}"
    return None


def _title_knockout(title_lc: str) -> str | None:
    if reason := _level_knockout(title_lc):
        return reason
    for term in config.TITLE_KNOCKOUTS:
        if term in title_lc:
            # "lead" appears inside "leadership"; the config uses "lead " with a
            # trailing space, but guard the sr./sr cases too.
            return f"seniority term in title: {term.strip()!r}"
    for term in config.DEPARTMENT_KNOCKOUTS:
        if term in title_lc:
            return f"non-technical role: {term!r}"
    return None


def score_location(location: str) -> tuple[int, str | None]:
    """Points for location desirability. Never a knockout on its own."""
    loc = (location or "").lower()
    if not loc:
        return 0, None

    # Do this before awarding anything: "Remote - Spain" matches "remote" and
    # would otherwise outrank an onsite Los Angeles role.
    if config.looks_non_us(location):
        return -20, f"non-US location: {location} (-20)"

    best_pts, best_term = 0, None
    for term, pts in config.LOCATION_PRIORITY.items():
        if term in loc and pts > best_pts:
            best_pts, best_term = pts, term
    if best_term:
        return best_pts, f"location match: {best_term} (+{best_pts})"
    return 0, None


def score_salary(salary_min: int | None) -> tuple[int, str | None]:
    """Points for the posted pay band, read as seniority rather than money.

    California requires the range for the level a requisition is written for,
    so the floor of the band says which rung it is — more reliably than the
    title does. Blaxel posts a Site Reliability Engineer at $175K-$250K and a
    Software Engineer at $140K-$190K; to a scorer reading only words those are
    the same seniority, and they are not.

    A band with no floor published says nothing and costs nothing.
    """
    if not salary_min:
        return 0, None
    if salary_min >= config.SALARY_SENIOR_FLOOR:
        return (
            -config.SALARY_SENIOR_PENALTY,
            f"band starts at ${salary_min:,} — written for a senior "
            f"(-{config.SALARY_SENIOR_PENALTY})",
        )
    if salary_min <= config.SALARY_ENTRY_CEILING:
        return 4, f"band starts at ${salary_min:,} — an entry band (+4)"
    return 0, None


def score_posting(
    title: str,
    description: str,
    location: str = "",
    verdict: GateVerdict | None = None,
    profile_skills: set[str] | None = None,
    salary_min: int | None = None,
) -> tuple[Score, GateVerdict, WorksiteVerdict]:
    """Score one posting, classify its eligibility gate and work arrangement.

    Returns all three so callers persist a single consistent judgement rather
    than re-deriving any of it later from different text.
    """
    title = title or ""
    title_lc = title.lower()
    verdict = verdict or classify(description, title)
    site = worksite_mod.classify(description, location)

    # A listing whose page reports it is gone cannot be applied to, whatever it
    # scores. Curated boards lag the underlying postings by days.
    if posting_gone(description):
        return Score(total=0, knockout="posting no longer accepting applications"), verdict, site

    if deadline_passed(description):
        return Score(total=0, knockout="application deadline has passed"), verdict, site

    if reason := _title_knockout(title_lc):
        return Score(total=0, knockout=reason), verdict, site

    total = 0
    reasons: list[str] = []

    # ── Role fit ───────────────────────────────────────────────────────────────
    matched_role = False
    strong_title_role = False
    for term, pts in config.ROLE_TERMS.items():
        if term in title_lc:
            total += pts
            reasons.append(f"role: {term!r} (+{pts})")
            matched_role = True
            if pts >= 8:
                strong_title_role = True

    # A title carrying nothing but the bare "engineer"/"developer" says almost
    # nothing, and everything after this point is blind to what the job is. If
    # the description then reads like a trade, it is a trade: UCLA's "Service
    # Engineer" is a commercial refrigeration technician and was reaching
    # fourth place in a batch on +4 for the word and +52 for being in LA.
    if not strong_title_role and description:
        desc_head = description.lower()[:3000]
        for term in config.TRADES_TERMS:
            if term in desc_head:
                return (
                    Score(total=0, knockout=f"skilled trade, not software: {term!r}"),
                    verdict,
                    site,
                )

    if not matched_role:
        # Fall back to the description so oddly-titled roles still surface,
        # but at a discount — the title is the stronger signal.
        desc_lc = (description or "").lower()[:2000]
        for term, pts in config.ROLE_TERMS.items():
            if pts >= 9 and term in desc_lc:
                bonus = pts // 3
                total += bonus
                reasons.append(f"role in description: {term!r} (+{bonus})")
                matched_role = True
                break

        # A description-only match means the title names no target role. The
        # small bonus above is not enough on its own: everything that follows
        # (location, gate, stack) is blind to what the job actually is, and
        # together those are worth far more than the role terms ever were. See
        # config.OFF_TITLE_PENALTY for the two postings that made this obvious.
        if matched_role:
            total -= config.OFF_TITLE_PENALTY
            reasons.append(
                f"title names no target role (-{config.OFF_TITLE_PENALTY})"
            )

    if not matched_role:
        return Score(total=0, knockout="no target role match"), verdict, site

    # ── Level fit ──────────────────────────────────────────────────────────────
    for term, pts in config.LEVEL_TERMS.items():
        if pts and term in title_lc:
            total += pts
            reasons.append(f"level: {term!r} (+{pts})")

    if _LEVEL_ONE_RE.search(title):
        total += 6
        reasons.append("level: 'Engineer I' (+6)")

    # ── Experience ceiling ─────────────────────────────────────────────────────
    years = min_years_required(description)
    if years is not None:
        if years > config.MAX_YEARS_EXPERIENCE:
            penalty = min(25, (years - config.MAX_YEARS_EXPERIENCE) * 6)
            total -= penalty
            reasons.append(f"asks {years}+ yrs experience (-{penalty})")
        else:
            total += 5
            reasons.append(f"only {years} yrs experience wanted (+5)")

    # ── Location ───────────────────────────────────────────────────────────────
    # A country named in the TITLE overrides a vague location field. Measured
    # 2026-09-07: "Backend Software Engineer - India" and "Analytics
    # Engineering Advocate - Europe" both carried location "Remote", collected
    # the full remote bonus, and came out at 99 and 79 — the first and sixth
    # highest-scoring postings in a batch of 25 to apply to. `score_location`
    # only ever saw the word "Remote", which is true and useless.
    # Only when the location field names nowhere concrete in the US. A role in
    # Los Angeles on a team called "India Team" is a Los Angeles role, and an
    # earlier version of this check moved it from 89 points to 31.
    effective_location = location
    if config.looks_non_us(title) and not config.names_us_location(location):
        effective_location = title
        reasons.append(f"title names a non-US location: {title!r}")
    elif not config.names_us_location(location):
        # Neither the location field nor the title says where this is, so ask
        # the description. Bree's "Machine Learning Engineer, Underwriting"
        # listed "Remote", scored 96 — the highest in the queue — and is a
        # Canadian consumer-finance company: three mentions of Canadians, none
        # of the US. `foreign_country_in` needs both halves before it answers,
        # because the cost of being wrong here is hiding a reachable job.
        if country := config.foreign_country_in(description):
            effective_location = country
            reasons.append(f"description places this in {country}, not the US")
    loc_pts, loc_reason = score_location(effective_location)
    total += loc_pts
    if loc_reason:
        reasons.append(loc_reason)

    # ── Posted pay band ────────────────────────────────────────────────────────
    pay_pts, pay_reason = score_salary(salary_min)
    total += pay_pts
    if pay_reason:
        reasons.append(pay_reason)

    # ── Gate adjustment ────────────────────────────────────────────────────────
    # This is the dropout-aware part: postings that explicitly accept
    # equivalent experience get pushed to the top, enrollment-gated ones sink.
    gate_points = {
        Gate.EQUIVALENT_OK: 25,
        Gate.OPEN: 10,
        Gate.SOFT_DEGREE: 0,
        Gate.HARD_DEGREE: -20,
        Gate.ADVANCED: -35,
        Gate.ENROLLMENT: -40,
    }
    pts = gate_points.get(verdict.gate, 0)

    # OPEN pays +10 for carrying no degree language. A posting whose text we
    # never recovered carries no degree language either, so without this it
    # collects the bonus for being unreadable — and outranks postings that were
    # actually checked. Withhold the bonus rather than the posting: it stays in
    # the queue, labelled, and can be opened by hand.
    if verdict.gate is Gate.OPEN and gate_unverified(description):
        reasons.append("gate: not checked, posting text unavailable (+0)")
    else:
        total += pts
        sign = "+" if pts >= 0 else ""
        reasons.append(f"gate: {verdict.label} ({sign}{pts})")

    # ── Skill overlap ──────────────────────────────────────────────────────────
    # Without this the ranking is blind to whether the posting's stack is
    # anything you've worked in.
    if profile_skills:
        match = skills_mod.compare(title, description, profile_skills)
        total += match.points
        reasons.extend(match.reasons)

    # ── Work arrangement ───────────────────────────────────────────────────────
    pref = config.RELOCATION

    if config.is_home_metro(location):
        # Already where the job is. Arrangement is irrelevant and no relocation
        # question arises, so this gets the same credit as a remote role.
        total += config.NO_MOVE_BONUS
        reasons.append(f"in your metro, no move needed (+{config.NO_MOVE_BONUS})")
    else:
        site_points = config.WORKSITE_POINTS.get(pref, config.WORKSITE_POINTS["open"])
        pts = site_points.get(site.worksite.value, 0)

        if pts <= -999:
            return (
                Score(total=0, knockout=f"work arrangement: {site.worksite.label}"),
                verdict,
                site,
            )

        total += pts
        if pts:
            sign = "+" if pts > 0 else ""
            reasons.append(f"worksite: {site.label} ({sign}{pts})")

        if site.relocation_offered is False and site.worksite is not Worksite.REMOTE:
            relo = config.NO_RELOCATION_PENALTY.get(pref, 0)
            total += relo
            if relo:
                reasons.append(f"no relocation assistance ({relo})")
        elif site.relocation_offered is True and site.worksite is not Worksite.REMOTE:
            # The mirror of the penalty above, and it was missing. An
            # out-of-metro role that explicitly funds the move scored
            # identically to one that never mentions relocation, so the
            # postings that are actually reachable for someone who would move
            # but cannot self-fund it were indistinguishable from the ones
            # that are not.
            relo = config.RELOCATION_OFFERED_BONUS.get(pref, 0)
            total += relo
            if relo:
                reasons.append(f"relocation assistance offered (+{relo})")

    return Score(total=max(0, total), reasons=reasons), verdict, site

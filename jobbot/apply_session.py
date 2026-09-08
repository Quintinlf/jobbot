"""Shared apply-queue helpers.

`apply` (one tab, sequential) and `prepare-applications` (many tabs, then
confirm) both pick letter-ready jobs the same way and both record Applied
only through `store.set_status(..., APPLIED)`.
"""

from __future__ import annotations

import re

from jobbot import config, store
from jobbot.gating import min_years_required
from jobbot.replies import normalize_company


def _role_key(row) -> tuple:
    """What makes two postings the same job to a human applying to them."""
    title = re.sub(r"[^a-z0-9]+", " ", (row["title"] or "").lower()).strip()
    company = re.sub(r"[^a-z0-9]+", "", (row["company"] or "").lower())
    location = re.sub(r"[^a-z0-9]+", " ", (row["location"] or "").lower()).strip()
    return company, title, location


def already_opened(conn) -> set[str]:
    """Postings jobbot has already filled a tab for in some earlier sitting.

    `prepare-applications` logs a `prepared` event per tab. A posting that was
    opened, submitted on the company's own site, and then never confirmed back
    in the terminal keeps its `new` status forever — so it returns to the top
    of the queue every single run. Measured 2026-09-05: 19 of the 22 postings
    in the ready queue had already been opened, which is the whole of the
    "it keeps giving me the same jobs" complaint.

    Being opened is not proof of being applied to, so these are hidden rather
    than marked applied. `include_opened=True` brings them back.
    """
    return {
        r["external_id"]
        for r in conn.execute(
            "SELECT DISTINCT external_id FROM events WHERE kind = 'prepared'"
        ).fetchall()
    }


def applied_companies(conn) -> set[str]:
    """Companies an application has already gone to.

    Distinct from `already_opened`, which is about one posting. The complaint
    this answers is about the company: measured 2026-09-07, 14 of the 25
    postings a batch offered were at companies with an application already
    out — Chime, GitLab, Hightouch, Twilio, OpenAI, Affirm and eight more. The
    per-batch `one_per_company` rule cannot see them, because it only dedupes
    within the batch it is building.

    Read from both `applications` and the jobs table. `applications` is the
    richer record but is only written by the paths that log one; a row moved
    to APPLIED by hand is in `jobs` alone, and either is proof enough.
    """
    seen = set()
    for source in (
        "SELECT DISTINCT company FROM applications",
        "SELECT DISTINCT company FROM jobs WHERE status = 'applied'",
    ):
        for row in conn.execute(source).fetchall():
            key = normalize_company(row["company"] or "")
            if key:
                seen.add(key)
    return seen


def ready_rows(
    conn, count: int = 5, min_score: int = 1, job_id: str | None = None,
    one_per_company: bool = True, include_opened: bool = False,
    max_years: int | None = config.MAX_YEARS_EXPERIENCE,
    require_letter: bool = True, include_applied: bool = False,
):
    """Jobs that have a cover letter and are still in the apply queue.

    One posting per role, and by default one posting per company. Companies
    routinely open several requisitions with an identical title and location —
    Twilio had two "Software Engineer (L2)", Remote - US, under different
    Greenhouse ids — and `store` is right not to merge those (either could be
    the live one), but handing you two of them in one sitting spends two of
    your ten slots on what reads as one application. The company rule goes
    further on purpose: four Affirm roles in one batch is four emails from the
    same recruiter within an hour of each other, which reads as spam pressure
    rather than four honest applications, whatever the postings actually are.

    `one_per_company=False` exists for `apply --job <id>`, which asks for one
    specific job by id and has no batch to dedupe.
    """
    if job_id:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE external_id = ?", (job_id,)
        ).fetchall()
        return list(rows)

    # Ask for lettered postings directly. Fetching the top 200 by score and
    # filtering afterwards meant a letter could drop out of the apply queue
    # simply because a scrape added 200 better-scoring jobs above it.
    candidates = store.queue(
        conn, limit=max(count * 8, 120), min_score=min_score,
        require_letter=require_letter,
    )
    opened = set() if include_opened else already_opened(conn)
    # Seed the per-batch company rule with the companies already applied
    # to, so one rule covers both "not twice in this batch" and "not
    # again at a company that already has my application".
    spent = set() if include_applied else applied_companies(conn)
    picked: list = []
    seen_roles: set[tuple] = set()
    seen_companies: set[str] = set(spent)
    for row in candidates:
        if row["external_id"] in opened:
            continue
        # `queue` filters years for the browsing commands but not here, so the
        # apply path could hand you a posting the queue listing had already
        # ruled out. Same ceiling, applied in the same place, for both.
        if max_years is not None:
            asked = min_years_required(row["description"] or "")
            if asked is not None and asked > max_years:
                continue
        role_key = _role_key(row)
        if role_key in seen_roles:
            continue
        company_key = normalize_company(row["company"] or "")
        if one_per_company and company_key and company_key in seen_companies:
            continue
        seen_roles.add(role_key)
        if company_key:
            seen_companies.add(company_key)
        picked.append(row)
        if len(picked) >= count:
            break
    return picked


def record_decision(conn, external_id: str, choice: str) -> str:
    """Persist a human decision. Returns the recorded action name.

    `y` is the only path that sets Applied / applied_at.
    `f` (failed fill) logs an event and does not mark applied.
    """
    if choice == "closed":
        store.mark_gone(conn, external_id, "page reports it is closed")
        return "closed"
    if choice == "y":
        store.set_status(conn, external_id, store.Status.APPLIED)
        return "applied"
    if choice == "s":
        store.set_status(conn, external_id, store.Status.SKIPPED)
        return "skipped"
    if choice == "f":
        store.log_event(conn, external_id, "prepare_failed", "fill failed; not applied")
        return "failed"
    if choice == "l":
        return "left"
    return choice

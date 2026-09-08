"""The refresh pipeline: fetch → score → classify → store.

Also imports the target_boards_ranked_*.csv exports that connection.ipynb has
been producing, so past scrapes land in the same database as live fetches
instead of sitting in loose CSVs at the repo root.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from jobbot import boards, boards_local, braven, config, enrich, profile, skills, store
from jobbot.boards import Posting
from jobbot.scoring import score_posting

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    fetched: int = 0
    new: int = 0
    updated: int = 0
    knocked_out: int = 0
    gated: int = 0
    reachable: int = 0
    retired: int = 0

    def summary(self) -> str:
        return (
            f"fetched {self.fetched} · new {self.new} · updated {self.updated} · "
            f"retired {self.retired} · knocked out {self.knocked_out} · "
            f"gate-blocked {self.gated} · reachable {self.reachable}"
        )


def retire_delisted(conn, postings: list[Posting]) -> int:
    """Retire stored jobs that have vanished from the board they came from.

    An ATS board endpoint returns every open posting, so a job we have on file
    that is absent from a fresh fetch of its own board has been taken down.
    Nothing used to act on that: the row kept its old score forever, which is
    how an expired CircleCI posting stayed at the top of the queue at 97 points
    until it was opened by hand and found to say "Posting expired".

    Only boards that returned at least one posting are considered. A board
    whose fetch failed comes back empty, and treating that as "every job here
    is gone" would wipe the queue on a network blip.
    """
    seen: dict[tuple[str, str], set[str]] = {}
    for posting in postings:
        seen.setdefault((posting.ats, posting.board_slug), set()).add(posting.external_id)

    retired = 0
    for (ats, slug), external_ids in seen.items():
        rows = conn.execute(
            """
            SELECT external_id FROM jobs
            WHERE ats = ? AND board_slug = ? AND applied_at IS NULL
            """,
            (ats, slug),
        ).fetchall()
        for row in rows:
            if row["external_id"] not in external_ids:
                store.mark_gone(conn, row["external_id"], f"delisted from {ats}:{slug}")
                retired += 1
    return retired


def _profile_skills() -> set[str]:
    """Canonical tech from the profile, loaded once per run."""
    try:
        return skills.canonicalize(profile.load().skills)
    except Exception:  # a missing or malformed profile must not stop a refresh
        logger.warning("could not load profile skills; skipping overlap scoring")
        return set()


def ingest(postings: list[Posting], conn) -> RefreshResult:
    """Score, classify, and persist a batch of postings."""
    result = RefreshResult(fetched=len(postings))
    profile_skills = _profile_skills()

    for posting in postings:
        score, verdict, site = score_posting(
            title=posting.title,
            description=posting.description,
            location=posting.location,
            profile_skills=profile_skills,
        )

        row = posting.as_row()
        if posting.raw.get("source_message_id"):
            row["source_message_id"] = posting.raw["source_message_id"]
        is_new = store.upsert_job(conn, row, score, verdict, site)
        if is_new:
            result.new += 1
        else:
            result.updated += 1

        if score.rejected:
            result.knocked_out += 1
        elif not verdict.worth_applying:
            result.gated += 1
        else:
            result.reachable += 1

    return result


def refresh(
    slugs: list[str] | None = None,
    on_board=None,
    conn=None,
    include_local: bool = True,
) -> RefreshResult:
    """Fetch every verified board and ingest the results."""
    verified = boards.load_verified()

    if slugs:
        verified = {s: verified[s] for s in slugs if s in verified}

    if not verified:
        raise RuntimeError(
            "No verified boards. Run `python -m jobbot verify` first — it probes "
            "each slug in data/companies.txt and records which ATS it uses."
        )

    postings = boards.fetch_all(verified, on_board=on_board)

    # Universities, hospitals and public agencies (Workday + NeoGov). The ATS
    # boards above are venture-backed tech almost exclusively, where 2% of
    # postings are sixth-house; these sources run 15-40%. Failures here are
    # logged and skipped rather than raised: a scraped public board going down
    # must not take the whole refresh with it.
    if include_local:
        # Only the fetch is guarded. An earlier version wrapped the callback
        # and the append in the same try, and `on_board` was being called with
        # two arguments where every caller's callback takes three. The
        # TypeError was caught, logged as "local boards skipped", and threw
        # away everything that had just been fetched — 674 postings on
        # 2026-09-07 (USC 382, LA County 116, UCLA 95, LA City 26, Long Beach
        # 25, Pasadena 26, Culver City 4), every run, silently. Bookkeeping
        # must not be able to discard data.
        local: list[Posting] = []
        try:
            local = boards_local.fetch_all_local()
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("local boards skipped: %s", exc)
        if local:
            postings = postings + local
            if on_board:
                on_board("local (workday/neogov/radancy)", "local", len(local))

    def run(c):
        result = ingest(postings, c)
        result.retired = retire_delisted(c, postings)
        return result

    if conn is not None:
        return run(conn)
    with store.session() as c:
        return run(c)


# ── Braven board ───────────────────────────────────────────────────────────────

def refresh_braven(
    enrich_descriptions: bool = True,
    on_fetch=None,
    on_enrich=None,
    conn=None,
) -> tuple[RefreshResult, enrich.EnrichResult]:
    """Fetch the Braven Opportunity Board, recover descriptions, ingest.

    Enrichment is on by default and is not an optimization: without it every
    posting reaches gating.py with nothing but its board metadata, and comes
    back OPEN because there is no text to find a gate in. That is a false
    negative dressed as a clean result.
    """
    postings = braven.fetch_board()
    if on_fetch:
        on_fetch(len(postings))

    enriched = enrich.EnrichResult()
    if enrich_descriptions and postings:
        enriched = enrich.attach_descriptions(postings, on_result=on_enrich)

    def run(c):
        result = ingest(postings, c)
        # The Braven fetch returns the whole board too, so a row Braven has
        # taken down should retire the same way an ATS delisting does.
        result.retired = retire_delisted(c, postings)
        return result

    if conn is not None:
        return run(conn), enriched
    with store.session() as c:
        return run(c), enriched


def rescore(conn) -> RefreshResult:
    """Re-score and re-classify every stored posting, without refetching.

    Tuning ROLE_TERMS or a gate pattern otherwise means a full network refresh
    of every board to see the effect. This re-runs the judgement over the
    descriptions already on disk, which takes seconds instead of minutes.
    """
    rows = conn.execute(
        "SELECT external_id, title, description, location FROM jobs"
    ).fetchall()
    result = RefreshResult(fetched=len(rows))
    profile_skills = _profile_skills()

    for row in rows:
        score, verdict, site = score_posting(
            title=row["title"],
            description=row["description"] or "",
            location=row["location"] or "",
            profile_skills=profile_skills,
        )
        conn.execute(
            """
            UPDATE jobs SET score=?, score_reasons=?, gate=?, gate_evidence=?,
                            knockout=?, worksite=?, worksite_detail=?
            WHERE external_id=?
            """,
            (
                score.total,
                json.dumps(score.reasons),
                verdict.gate.value,
                json.dumps(verdict.evidence),
                score.knockout,
                site.worksite.value,
                json.dumps(site.as_dict()),
                row["external_id"],
            ),
        )
        result.updated += 1
        if score.rejected:
            result.knocked_out += 1
        elif not verdict.worth_applying:
            result.gated += 1
        else:
            result.reachable += 1

    return result


# ── Legacy CSV import ──────────────────────────────────────────────────────────

def _legacy_rows(path: Path) -> list[Posting]:
    """Convert a target_boards_ranked_*.csv row into a Posting.

    Those exports carry no description column, so gate classification cannot
    run on them — they land as OPEN with no evidence. Live fetches are
    strictly better; this exists so historical scrapes are not lost.
    """
    postings: list[Posting] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            link = (row.get("link") or "").strip()
            if not link:
                continue
            job_id = (row.get("job_id") or link).strip()
            postings.append(
                Posting(
                    external_id=f"legacy:{job_id}",
                    title=(row.get("title") or "").strip(),
                    company=(row.get("company") or row.get("entity") or "").strip(),
                    location=(row.get("location") or "").strip(),
                    url=link,
                    description="",
                    ats=(row.get("source") or "legacy").strip(),
                    board_slug=(row.get("source") or "").strip().lower(),
                    posted_at=(row.get("first_published") or "").strip(),
                )
            )
    return postings


def import_legacy(base_dir: Path | None = None, conn=None) -> RefreshResult:
    """Pull every target_boards_ranked_*.csv at the repo root into the DB."""
    base = base_dir or config.BASE_DIR
    files = sorted(base.glob(config.LEGACY_CSV_GLOB))
    if not files:
        return RefreshResult()

    postings: list[Posting] = []
    for path in files:
        try:
            postings.extend(_legacy_rows(path))
        except Exception as exc:
            logger.warning("could not read %s: %s", path.name, exc)

    # Later exports supersede earlier ones for the same job.
    deduped = {p.external_id: p for p in postings}

    if conn is not None:
        return ingest(list(deduped.values()), conn)
    with store.session() as c:
        return ingest(list(deduped.values()), c)

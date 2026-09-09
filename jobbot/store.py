"""SQLite persistence and application lifecycle.

The old jobs.db only recorded "seen this link before". That answers whether to
skip a job but not the question that actually matters after a few weeks: which
applications went out, when, and what came back. Response rate is impossible
to reason about without that, so status is tracked as a first-class column.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from jobbot import config
from jobbot.gating import GONE_MARKER
from jobbot.identity import ats_key, canonical_url, fingerprint, prefer_url


class Status(str, Enum):
    NEW = "new"              # scraped, scored, awaiting review
    QUEUED = "queued"        # you marked it worth applying to
    APPLIED = "applied"      # submitted
    SKIPPED = "skipped"      # reviewed and passed on
    REJECTED = "rejected"    # heard back, no
    INTERVIEW = "interview"  # heard back, yes
    GHOSTED = "ghosted"      # no reply after the follow-up window


# Note on column comments: keep "--" comments out of the CREATE TABLE body.
# SQLite reconstructs the DDL when running ALTER TABLE ... DROP COLUMN, and an
# inline comment makes that fail with "incomplete input".
#
# letter_variants holds JSON {framing: letter}; cover_letter holds whichever
# variant is currently selected, so code that just wants "the letter" works.
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    external_id   TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT,
    url           TEXT,
    description   TEXT,
    ats           TEXT,
    board_slug    TEXT,
    department    TEXT,
    posted_at     TEXT,

    score         INTEGER DEFAULT 0,
    score_reasons TEXT,
    gate          TEXT,
    gate_evidence TEXT,
    knockout      TEXT,
    worksite      TEXT,
    worksite_detail TEXT,
    skill_match   TEXT,

    status        TEXT NOT NULL DEFAULT 'new',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    applied_at    TEXT,
    responded_at  TEXT,
    notes         TEXT,

    cover_letter  TEXT,
    resume_notes  TEXT,
    letter_variants TEXT,

    canonical_url TEXT,
    identity_key  TEXT,
    sources       TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_gate   ON jobs(gate);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_job ON events(external_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    config.ensure_dirs()
    path = db_path or config.DB_PATH
    # timeout + WAL so the review app stays readable while a refresh is
    # writing. Without this, `jobbot stats` or an open queue fails outright
    # with "database is locked" for the whole duration of a fetch.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    from jobbot import outcomes  # local import: outcomes reads jobs rows

    outcomes.ensure_schema(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    CREATE TABLE IF NOT EXISTS silently does nothing on an existing table, so
    new columns have to be added explicitly or an upgrade breaks a database
    that already holds your application history.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, ddl in (
        ("letter_variants", "TEXT"),
        ("worksite", "TEXT"),
        ("worksite_detail", "TEXT"),
        ("skill_match", "TEXT"),
        ("canonical_url", "TEXT"),
        ("identity_key", "TEXT"),
        ("sources", "TEXT"),
        # Posted pay band. Ashby returns it structured and jobbot was
        # already asking for it, then throwing it away — so the clearest
        # seniority signal a posting carries never reached the score.
        ("salary_min", "INTEGER"),
        ("salary_max", "INTEGER"),
        ("salary_summary", "TEXT"),
    ):
        if column not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_canonical ON jobs(canonical_url)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_identity ON jobs(identity_key)"
    )


@contextmanager
def session(db_path: Path | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_event(conn: sqlite3.Connection, external_id: str, kind: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO events (external_id, at, kind, detail) VALUES (?,?,?,?)",
        (external_id, now(), kind, detail),
    )


def _parse_sources(raw) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _identity_key(row: dict) -> str:
    return ats_key(row.get("url") or "") or fingerprint(
        row.get("company") or "",
        row.get("title") or "",
        row.get("location") or "",
    )


def _source_entry(row: dict, ts: str) -> dict:
    entry = {
        "source": row.get("ats") or "",
        "board_slug": row.get("board_slug") or "",
        "url": row.get("url") or "",
        "at": ts,
    }
    if row.get("source_message_id"):
        entry["message_id"] = row["source_message_id"]
    return entry


def _merge_sources(existing_raw, incoming: dict, ts: str) -> str:
    merged = _parse_sources(existing_raw)
    entry = _source_entry(incoming, ts)
    signature = (entry.get("source"), entry.get("url"), entry.get("message_id", ""))
    have = {
        (e.get("source"), e.get("url"), e.get("message_id", ""))
        for e in merged
        if isinstance(e, dict)
    }
    if signature not in have:
        merged.append(entry)
    return json.dumps(merged)


def find_duplicate(conn: sqlite3.Connection, row: dict):
    """Existing job that is the same posting from another source, or None."""
    ext = row.get("external_id") or ""
    if ext:
        hit = conn.execute(
            "SELECT * FROM jobs WHERE external_id = ?", (ext,)
        ).fetchone()
        if hit:
            return hit

    canon = row.get("canonical_url") or canonical_url(row.get("url") or "")
    if canon:
        hit = conn.execute(
            "SELECT * FROM jobs WHERE canonical_url = ? AND canonical_url != ''",
            (canon,),
        ).fetchone()
        if hit:
            return hit

    key = ats_key(row.get("url") or "")
    if key:
        hit = conn.execute(
            "SELECT * FROM jobs WHERE identity_key = ?", (key,)
        ).fetchone()
        if hit:
            return hit

    # Company+title+location is only for email/aggregator copies of a role.
    # Two Greenhouse postings both titled "Data Engineer" at the same company
    # are different jobs.
    incoming_email = (row.get("ats") or "") == "email"
    loc = row.get("location") or ""
    fp = fingerprint(row.get("company") or "", row.get("title") or "", loc)
    if fp and incoming_email:
        hit = conn.execute(
            "SELECT * FROM jobs WHERE identity_key = ?", (fp,)
        ).fetchone()
        if hit:
            return hit
        core = fingerprint(row.get("company") or "", row.get("title") or "", "")
        if core and not loc.strip():
            like = core + "%"
            matches = conn.execute(
                "SELECT * FROM jobs WHERE identity_key LIKE ?", (like,)
            ).fetchall()
            if len(matches) == 1:
                return matches[0]
    elif fp and not incoming_email:
        hit = conn.execute(
            "SELECT * FROM jobs WHERE identity_key = ? AND ats = 'email'",
            (fp,),
        ).fetchone()
        if hit:
            return hit
    return None


def upsert_job(conn: sqlite3.Connection, row: dict, score, verdict, site=None) -> bool:
    """Insert a posting or refresh an existing one.

    Returns True if this was a new posting. Re-scraping an existing job
    updates its score and last_seen but never clobbers status, applied_at, or
    anything you typed — those are yours.

    A second discovery of the same role (Jobot email vs Greenhouse board)
    merges onto the existing row instead of creating another candidate.
    """
    ts = now()
    incoming = dict(row)
    incoming["canonical_url"] = canonical_url(incoming.get("url") or "")
    incoming["identity_key"] = _identity_key(incoming)

    match = find_duplicate(conn, incoming)
    same_id = bool(
        match and match["external_id"] == incoming.get("external_id")
    )
    duplicate = bool(match) and not same_id

    payload = {
        # Defaults first, so a caller that builds a row by hand — registering
        # an application sent outside the tool, for instance — does not have to
        # know every column the schema has grown since.
        "salary_min": None,
        "salary_max": None,
        "salary_summary": "",
        **incoming,
        "score": score.total,
        "score_reasons": json.dumps(score.reasons),
        "gate": verdict.gate.value,
        "gate_evidence": json.dumps(verdict.evidence),
        "knockout": score.knockout,
        "worksite": site.worksite.value if site else None,
        "worksite_detail": json.dumps(site.as_dict()) if site else None,
        "last_seen": ts,
    }

    if match:
        payload["external_id"] = match["external_id"]
        payload["url"] = prefer_url(match["url"] or "", incoming.get("url") or "")
        payload["canonical_url"] = canonical_url(payload["url"])
        payload["identity_key"] = (
            ats_key(payload["url"]) or incoming["identity_key"] or match["identity_key"]
        )
        if incoming.get("ats") in {"greenhouse", "lever", "ashby"}:
            payload["ats"] = incoming["ats"]
            payload["board_slug"] = incoming.get("board_slug") or match["board_slug"]
        elif (match["ats"] or "") in {"greenhouse", "lever", "ashby"} and (
            incoming.get("ats") == "email"
        ):
            payload["ats"] = match["ats"]
            payload["board_slug"] = match["board_slug"]
        if duplicate and (match["description"] or "") and len(
            match["description"] or ""
        ) > len(incoming.get("description") or ""):
            payload["description"] = match["description"]
        payload["sources"] = _merge_sources(match["sources"], incoming, ts)
        conn.execute(
            """
            UPDATE jobs SET
                title=:title, company=:company, location=:location, url=:url,
                description=:description, ats=:ats, board_slug=:board_slug,
                department=:department, posted_at=:posted_at,
                score=:score, score_reasons=:score_reasons,
                gate=:gate, gate_evidence=:gate_evidence, knockout=:knockout,
                worksite=:worksite, worksite_detail=:worksite_detail,
                salary_min=:salary_min, salary_max=:salary_max,
                salary_summary=:salary_summary,
                last_seen=:last_seen,
                canonical_url=:canonical_url, identity_key=:identity_key,
                sources=:sources
            WHERE external_id=:external_id
            """,
            payload,
        )
        if duplicate:
            log_event(
                conn,
                match["external_id"],
                "discovered_from",
                f"{incoming.get('ats') or incoming.get('board_slug')}: "
                f"{incoming.get('url') or incoming.get('external_id')}",
            )
        return False

    payload["first_seen"] = ts
    payload["status"] = Status.NEW.value
    payload["sources"] = json.dumps([_source_entry(incoming, ts)])
    conn.execute(
        """
        INSERT INTO jobs (
            external_id, title, company, location, url, description, ats,
            board_slug, department, posted_at, score, score_reasons, gate,
            gate_evidence, knockout, worksite, worksite_detail,
            salary_min, salary_max, salary_summary,
            status, first_seen, last_seen,
            canonical_url, identity_key, sources
        ) VALUES (
            :external_id, :title, :company, :location, :url, :description, :ats,
            :board_slug, :department, :posted_at, :score, :score_reasons, :gate,
            :gate_evidence, :knockout, :worksite, :worksite_detail,
            :salary_min, :salary_max, :salary_summary,
            :status, :first_seen, :last_seen,
            :canonical_url, :identity_key, :sources
        )
        """,
        payload,
    )
    log_event(conn, incoming["external_id"], "discovered", f"{incoming['company']} — {incoming['title']}")
    return True


def mark_gone(conn: sqlite3.Connection, external_id: str, reason: str) -> None:
    """Retire a posting that no longer exists.

    The marker goes into the description rather than only into `knockout`,
    because `rescore` recomputes knockout from the description and would
    otherwise resurrect the job the next time scoring is tuned. Scoring reads
    the marker and knocks the posting out, so the two agree offline.

    Applications already sent are left alone — a posting closing after you
    applied is normal, and rewriting that record would lose the history.
    """
    row = conn.execute(
        "SELECT description, applied_at FROM jobs WHERE external_id=?", (external_id,)
    ).fetchone()
    if row is None or row["applied_at"]:
        return

    description = row["description"] or ""
    if GONE_MARKER not in description:
        description = f"{description}\n\n{GONE_MARKER}".strip()

    conn.execute(
        "UPDATE jobs SET description=?, knockout=?, score=0 WHERE external_id=?",
        (description, f"posting no longer available: {reason}"[:200], external_id),
    )
    log_event(conn, external_id, "gone", reason[:200])


def set_status(
    conn: sqlite3.Connection, external_id: str, status: Status, note: str = ""
) -> None:
    ts = now()
    fields = {"status": status.value, "external_id": external_id}
    sql = "UPDATE jobs SET status=:status"

    if status is Status.APPLIED:
        sql += ", applied_at=:ts"
        fields["ts"] = ts
    elif status in (Status.REJECTED, Status.INTERVIEW):
        sql += ", responded_at=:ts"
        fields["ts"] = ts

    if note:
        sql += ", notes=:note"
        fields["note"] = note

    sql += " WHERE external_id=:external_id"
    conn.execute(sql, fields)
    log_event(conn, external_id, f"status:{status.value}", note)

    if status is Status.APPLIED:
        # Freeze what this application looked like on the way out. The row is
        # about to keep changing — rescore rewrites the score, a refresh
        # rewrites the description — and a model trained on the current row
        # would be learning from a posting that is no longer the one you
        # applied to. Imported here to keep the module import graph acyclic.
        from jobbot import outcomes

        outcomes.snapshot_application(conn, external_id, applied_at=ts)


def save_materials(
    conn: sqlite3.Connection,
    external_id: str,
    cover_letter: str = "",
    resume_notes: str = "",
    variants: dict[str, str] | None = None,
    overwrite: bool = False,
) -> None:
    """Store letters for a job.

    `variants` holds every education framing generated so far; `cover_letter`
    is the one currently selected.

    New variants are MERGED into what is already stored, and an existing
    non-empty letter is never replaced unless overwrite=True. Writing a
    generated draft over a letter you already had is silent data loss — it
    happened once, to a real letter, via the review app's brief button.
    """
    if variants:
        row = conn.execute(
            "SELECT letter_variants FROM jobs WHERE external_id=?", (external_id,)
        ).fetchone()
        existing = {}
        if row and row["letter_variants"]:
            try:
                existing = json.loads(row["letter_variants"])
            except json.JSONDecodeError:
                existing = {}

        merged = dict(existing)
        for key, letter in variants.items():
            if not letter:
                continue
            if overwrite or not merged.get(key):
                merged[key] = letter

        payload = json.dumps(merged)
        if not cover_letter:
            cover_letter = next(iter(merged.values()), "")
    else:
        payload = None

    conn.execute(
        """
        UPDATE jobs SET cover_letter=?, resume_notes=?,
                        letter_variants=COALESCE(?, letter_variants)
        WHERE external_id=?
        """,
        (cover_letter, resume_notes, payload, external_id),
    )
    log_event(
        conn,
        external_id,
        "materials_generated",
        ", ".join(variants) if variants else "",
    )


def select_letter(conn: sqlite3.Connection, external_id: str, framing: str) -> str:
    """Promote one stored variant to be the active cover letter."""
    row = conn.execute(
        "SELECT letter_variants FROM jobs WHERE external_id=?", (external_id,)
    ).fetchone()
    if not row or not row["letter_variants"]:
        return ""

    variants = json.loads(row["letter_variants"])
    letter = variants.get(framing, "")
    if letter:
        conn.execute(
            "UPDATE jobs SET cover_letter=? WHERE external_id=?",
            (letter, external_id),
        )
        log_event(conn, external_id, "framing_selected", framing)
    return letter


def excluded_companies() -> list[str]:
    """Company slugs the user has ruled out, lowercased. Never raises.

    Read lazily rather than passed in, so every caller of `queue` honours the
    exclusion without each one having to remember to thread a profile through.
    A missing or malformed profile means "exclude nothing", not a crash — this
    is a filter, and failing closed would silently empty the queue.
    """
    try:
        from jobbot import profile as profile_mod

        return [
            str(c).strip().lower()
            for c in (profile_mod.load().excluded_companies or [])
            if str(c).strip()
        ]
    except Exception:
        return []


def queue(
    conn: sqlite3.Connection,
    limit: int = 50,
    min_score: int = 1,
    include_gated: bool = False,
    statuses: tuple[str, ...] = (Status.NEW.value, Status.QUEUED.value),
    require_letter: bool = False,
) -> list[sqlite3.Row]:
    """Postings awaiting review, best first.

    Gated postings are hidden by default — that is the whole point — but
    include_gated surfaces them so the filter can be sanity-checked rather
    than trusted blindly.

    `require_letter` narrows to postings that already have a cover letter, and
    exists because the apply queue used to fetch the top N by score and filter
    for letters afterwards. With fourteen thousand jobs and seventeen letters,
    a fresh scrape of higher-scoring postings pushed jobs you had already
    written letters for out of the window, and they vanished from the apply
    queue without anything being logged. Filtering in SQL keeps the letters in
    view no matter how much the queue above them churns.
    """
    gates = ("equivalent_ok", "open", "soft_degree")
    sql = f"""
        SELECT * FROM jobs
        WHERE status IN ({','.join('?' * len(statuses))})
          AND knockout IS NULL
          AND score >= ?
    """
    params: list = [*statuses, min_score]

    if require_letter:
        sql += " AND cover_letter IS NOT NULL AND TRIM(cover_letter) != ''"

    if not include_gated:
        sql += f" AND gate IN ({','.join('?' * len(gates))})"
        params.extend(gates)

    # Companies ruled out once, rather than re-skipped by hand every refresh.
    # Setting their rows to `skipped` in the database does not hold: the next
    # refresh re-upserts the same postings as NEW and they climb straight back
    # into the queue. The decision has to live in the profile and be applied
    # on read.
    excluded = excluded_companies()
    if excluded:
        sql += f" AND LOWER(company) NOT IN ({','.join('?' * len(excluded))})"
        params.extend(excluded)

    sql += " ORDER BY score DESC, first_seen DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def stats(conn: sqlite3.Connection) -> dict:
    """Funnel counts plus the response rate the whole exercise is about."""
    by_status = {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")
    }
    by_gate = {
        r["gate"]: r["n"]
        for r in conn.execute(
            "SELECT gate, COUNT(*) n FROM jobs WHERE knockout IS NULL GROUP BY gate"
        )
    }
    total = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    knocked = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE knockout IS NOT NULL"
    ).fetchone()["n"]

    # Count applications off applied_at, not status. Status is a single column,
    # so a job that moves applied -> interview would otherwise stop counting as
    # applied and silently inflate the response rate.
    applied = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()["n"]
    responded = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE responded_at IS NOT NULL"
    ).fetchone()["n"]

    interviews = by_status.get(Status.INTERVIEW.value, 0)
    responses = responded

    letter_ready = conn.execute(
        """
        SELECT COUNT(*) n FROM jobs
        WHERE knockout IS NULL
          AND cover_letter IS NOT NULL AND TRIM(cover_letter) != ''
          AND status IN ('new', 'queued')
        """
    ).fetchone()["n"]
    letter_pending = conn.execute(
        """
        SELECT COUNT(*) n FROM jobs
        WHERE knockout IS NULL
          AND (cover_letter IS NULL OR TRIM(cover_letter) = '')
          AND status IN ('new', 'queued')
          AND gate IN ('equivalent_ok', 'open', 'soft_degree')
        """
    ).fetchone()["n"]

    return {
        "total": total,
        "knocked_out": knocked,
        "by_status": by_status,
        "by_gate": by_gate,
        "letter_ready": letter_ready,
        "letter_pending": letter_pending,
        "applied": applied,
        "responses": responses,
        "interviews": interviews,
        "response_rate": (responses / applied) if applied else None,
        "interview_rate": (interviews / applied) if applied else None,
    }

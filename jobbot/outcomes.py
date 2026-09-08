"""What came back after you applied, and the frozen inputs that produced it.

`store.stats` can already tell you 7 applications went out and 0 came back.
That is a number, not a lesson. To learn anything from a rejection you need
two things this module adds:

1. The features of the application AS IT WAS SENT. The jobs row keeps
   mutating — `rescore` rewrites `score`, a refresh rewrites `description`,
   `mark_gone` rewrites both. Reading features off the live row weeks later
   trains a model on a posting that no longer resembles what you applied to.
   So a snapshot is frozen at the moment status becomes APPLIED.

2. The outcome as an event with provenance, not a status column. Status is one
   value; the history is "auto-acknowledged day 0, rejected day 14, stage
   screen, learned from an email whose text said the word degree". That
   sequence is the training signal. Overwriting a column throws it away.

Nothing here decides anything. It records, and `learn` reads.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from jobbot import config
from jobbot.identity import canonical_url

# Outcome kinds. Kept separate from store.Status: status is where the job sits
# now, an outcome is a thing that happened on a date.
REJECTED = "rejected"
INTERVIEW = "interview"
OFFER = "offer"
ACK = "ack"          # automated "we received your application"
GHOSTED = "ghosted"  # derived from silence, never from a message

OUTCOMES = (REJECTED, INTERVIEW, OFFER, ACK, GHOSTED)

# Outcomes that mean a human read it and wanted to continue. This is the label
# the model predicts — not "not rejected", because silence is not encouragement.
ADVANCED = (INTERVIEW, OFFER)

# Days of silence after which an application is treated as a negative example.
# Shorter than it feels: most ATS rejections land inside three weeks, and
# holding out for a reply that never comes starves the model of negatives.
GHOST_AFTER_DAYS = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    external_id TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    company     TEXT,
    title       TEXT,
    features    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL,
    at          TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    stage       TEXT,
    source      TEXT NOT NULL,
    confidence  REAL,
    detail      TEXT,
    message_id  TEXT NOT NULL DEFAULT '',
    signals     TEXT
);

CREATE INDEX IF NOT EXISTS idx_outcomes_job ON outcomes(external_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_kind ON outcomes(outcome);
"""

# Partial index so re-reading the same rejection email is a no-op, while manual
# entries (which carry no message id) stay insertable more than once.
DEDUPE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_outcomes_msg
ON outcomes(external_id, message_id) WHERE message_id != ''
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(DEDUPE_INDEX)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str | None):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days_between(later: str | None, earlier: str | None):
    a, b = _parse(later), _parse(earlier)
    if a is None or b is None:
        return None
    return round((a - b).total_seconds() / 86400, 1)


# ── Feature extraction ─────────────────────────────────────────────────────────
# Deliberately small and mostly categorical. A dozen honest features over a few
# dozen applications is a model; sixty features over the same data is noise
# wearing a lab coat.

_LEVEL_WORDS = (
    ("intern", ("intern", "internship", "co-op", "coop")),
    ("apprentice", ("apprentice", "residency", "trainee")),
    ("entry", ("entry level", "entry-level", "new grad", "university grad", "graduate")),
    ("junior", ("junior", "jr.", "jr ")),
    ("associate", ("associate",)),
)

_LEVEL_ONE_RE = re.compile(r"\bengineer\s+(?:i|1)\b", re.IGNORECASE)


def title_level(title: str) -> str:
    lowered = (title or "").lower()
    for label, terms in _LEVEL_WORDS:
        if any(t in lowered for t in terms):
            return label
    if _LEVEL_ONE_RE.search(title or ""):
        return "level_one"
    return "unmarked"


def location_bucket(location: str, worksite: str | None) -> str:
    loc = (location or "").strip()
    if worksite == "remote":
        return "remote"
    if not loc:
        return "unknown"
    if config.looks_non_us(loc):
        return "non_us"
    if config.is_home_metro(loc):
        return "home_metro"
    return "us_other"


def _column(row, name):
    """Read a column that may not exist on an older row object."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _letter_framing(row) -> str:
    """Which education framing actually went out with this application."""
    letter = (_column(row, "cover_letter") or "").strip()
    if not letter:
        return "none"
    raw = _column(row, "letter_variants")
    try:
        variants = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        variants = {}
    if isinstance(variants, dict):
        for framing, text in variants.items():
            if (text or "").strip() == letter:
                return framing
    return "custom"


def _skill_counts(row) -> tuple[int, int]:
    raw = _column(row, "skill_match")
    try:
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        return 0, 0
    return len(data.get("overlap") or []), len(data.get("foreign_core") or [])


def features(row, applied_at: str | None = None) -> dict:
    """The inputs to one application, as a flat dict of model features.

    Everything here has to be knowable BEFORE the outcome, or the model learns
    to predict the past. No outcome-derived fields, no response counts.
    """
    from jobbot.gating import min_years_required

    applied_at = applied_at or _column(row, "applied_at") or now()
    description = _column(row, "description") or ""
    letter = (_column(row, "cover_letter") or "").strip()
    overlap, foreign = _skill_counts(row)

    return {
        "score": _column(row, "score") or 0,
        "gate": _column(row, "gate") or "unknown",
        "worksite": _column(row, "worksite") or "unclear",
        "ats": _column(row, "ats") or "unknown",
        "source_board": _column(row, "board_slug") or "",
        "title_level": title_level(_column(row, "title") or ""),
        "location_bucket": location_bucket(
            _column(row, "location") or "", _column(row, "worksite")
        ),
        "years_required": min_years_required(description),
        "skill_overlap": overlap,
        "skill_foreign": foreign,
        "has_letter": bool(letter),
        "letter_framing": _letter_framing(row),
        "letter_words": len(letter.split()) if letter else 0,
        "description_words": len(description.split()),
        "days_known_before_applying": _days_between(applied_at, _column(row, "first_seen")),
        "posting_age_days": _days_between(applied_at, _column(row, "posted_at")),
    }


def snapshot_application(
    conn: sqlite3.Connection, external_id: str, applied_at: str | None = None
) -> bool:
    """Freeze the features of an application at send time.

    Called from `store.set_status` when a job becomes APPLIED. Idempotent, and
    never overwrites an existing snapshot: re-marking a job applied months
    later must not rewrite what was true the first time.
    """
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM jobs WHERE external_id=?", (external_id,)).fetchone()
    if row is None:
        return False
    if conn.execute(
        "SELECT 1 FROM applications WHERE external_id=?", (external_id,)
    ).fetchone():
        return False

    ts = applied_at or _column(row, "applied_at") or now()
    conn.execute(
        "INSERT INTO applications (external_id, applied_at, company, title, features)"
        " VALUES (?,?,?,?,?)",
        (external_id, ts, row["company"], row["title"], json.dumps(features(row, ts))),
    )
    return True


def log_external_application(
    conn: sqlite3.Connection,
    company: str,
    title: str,
    url: str = "",
    location: str = "",
    description: str = "",
    applied_at: str | None = None,
) -> str:
    """Register an application you made outside jobbot.

    Without this, the training set is only the slice of your job search that
    happened to go through this tool. That is not a random slice — the jobs you
    applied to directly are the ones you cared enough about to chase — so
    dropping them would bias every rate the model learns.

    The row is scored through the same path as a scraped posting so its
    features are comparable, but it is marked ats='manual': a posting whose
    description you never captured genuinely knows less about itself, and
    `learn` sees that as missing data rather than as a zero.
    """
    from jobbot import store
    from jobbot.scoring import score_posting

    company = (company or "").strip()
    title = (title or "").strip()
    if not company or not title:
        raise ValueError("company and title are both required")

    key = canonical_url(url) or f"{company.lower()}|{title.lower()}"
    external_id = f"manual:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"

    existing = conn.execute(
        "SELECT * FROM jobs WHERE external_id=?", (external_id,)
    ).fetchone()
    if existing is None:
        score, verdict, site = score_posting(title, description, location)
        store.upsert_job(
            conn,
            {
                "external_id": external_id,
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "description": description,
                "ats": "manual",
                "board_slug": "",
                "department": "",
                "posted_at": "",
            },
            score, verdict, site,
        )

    store.set_status(conn, external_id, store.Status.APPLIED)
    if applied_at:
        conn.execute(
            "UPDATE jobs SET applied_at=? WHERE external_id=?", (applied_at, external_id)
        )
        conn.execute(
            "UPDATE applications SET applied_at=? WHERE external_id=?",
            (applied_at, external_id),
        )
    return external_id


def backfill_snapshots(conn: sqlite3.Connection) -> int:
    """Snapshot applications that were sent before snapshots existed.

    Their features come off the current row, which is imperfect — the score may
    have been retuned since. They are flagged `reconstructed` so `learn` can
    report how much of the training set is rebuilt rather than frozen.
    """
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT j.* FROM jobs j
        LEFT JOIN applications a ON a.external_id = j.external_id
        WHERE j.applied_at IS NOT NULL AND a.external_id IS NULL
        """
    ).fetchall()
    for row in rows:
        payload = features(row, row["applied_at"])
        payload["reconstructed"] = True
        conn.execute(
            "INSERT INTO applications (external_id, applied_at, company, title, features)"
            " VALUES (?,?,?,?,?)",
            (row["external_id"], row["applied_at"], row["company"], row["title"],
             json.dumps(payload)),
        )
    return len(rows)


# ── Stated reasons ─────────────────────────────────────────────────────────────
# Most rejection letters say nothing. The ones that do say something say it in a
# handful of shapes, and at small sample sizes a single explicit "requires a
# bachelor's degree" is worth more than any coefficient a model could fit.

REASON_PATTERNS: tuple[tuple[str, str], ...] = (
    ("degree",
     r"(?:bachelor|master's degree|degree (?:in|require|program)|diploma|"
     r"academic (?:requirement|credential))"),
    ("experience",
     r"(?:years of (?:relevant )?experience|more experience|"
     r"experience (?:level|require)|level of (?:seniority|experience))"),
    ("skills_gap",
     r"(?:specific (?:skills|technical)|technical (?:skills|requirement)|"
     r"proficien\w+ in|skill ?set)"),
    ("work_authorization",
     r"(?:work authorization|sponsor\w*|visa|right to work|citizenship)"),
    ("location",
     r"(?:relocat\w+|on-?site requirement|must (?:be|reside|live) (?:in|near)|time ?zone)"),
    # "the position you applied for has been filled" puts four words between
    # the noun and the verb, so these cannot be adjacent-word patterns. Bounded
    # to a single sentence so it does not reach across into unrelated text.
    ("role_closed",
     r"(?:(?:position|role|req(?:uisition)?|opening)[^.!?]{0,60}\b"
     r"(?:filled|closed|cancell?ed|eliminated|on hold)\b|"
     r"no longer (?:open|available|accepting)|hiring freeze)"),
    ("stronger_candidates",
     r"(?:more closely align|other candidates|stronger (?:candidates|applicants)|"
     r"better (?:match|fit) for)"),
)
_REASONS = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in REASON_PATTERNS]


def reason_signals(text: str) -> list[str]:
    """Named reasons a rejection actually stated. Empty is the common case."""
    blob = text or ""
    return [name for name, pattern in _REASONS if pattern.search(blob)]


# ── Recording ──────────────────────────────────────────────────────────────────

def message_already_used(conn: sqlite3.Connection, message_id: str) -> bool:
    """Whether this email has already produced an outcome for ANY job.

    The dedupe index on (external_id, message_id) only stops the SAME job from
    being written twice by the same message — it does nothing to stop the
    message being matched to a SECOND, DIFFERENT job on a later run. That is
    exactly what happened to a real Reddit confirmation: it correctly resolved
    to one "Software Engineer, Ads" posting the first time it was read, and
    then — after a duplicate-titled req with the identical name appeared on a
    later scrape — resolved to that OTHER posting too, because nothing
    remembered the message had already been spent. One email is evidence about
    one application; it must never be allowed to settle two.
    """
    if not message_id:
        return False
    ensure_schema(conn)
    return conn.execute(
        "SELECT 1 FROM outcomes WHERE message_id=? LIMIT 1", (message_id,)
    ).fetchone() is not None


def record(
    conn: sqlite3.Connection,
    external_id: str,
    outcome: str,
    *,
    source: str = "manual",
    stage: str = "",
    detail: str = "",
    message_id: str = "",
    confidence: float | None = None,
    at: str | None = None,
) -> bool:
    """Log one thing that came back. Returns False if already recorded.

    Status is updated to match, but the outcome row is the record of truth — a
    later interview invite does not erase the earlier rejection event, which is
    exactly what a status column would do.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; expected one of {OUTCOMES}")
    ensure_schema(conn)

    signals = json.dumps(reason_signals(detail)) if detail else "[]"
    try:
        conn.execute(
            "INSERT INTO outcomes (external_id, at, outcome, stage, source,"
            " confidence, detail, message_id, signals) VALUES (?,?,?,?,?,?,?,?,?)",
            (external_id, at or now(), outcome, stage, source, confidence,
             (detail or "")[:8000], message_id or "", signals),
        )
    except sqlite3.IntegrityError:
        return False

    # Snapshot late rather than never: an outcome can arrive for an application
    # that predates snapshotting, and a labelled row with no features is a
    # wasted lesson.
    snapshot_application(conn, external_id)

    from jobbot import store

    if outcome == REJECTED:
        store.set_status(conn, external_id, store.Status.REJECTED, detail[:200])
    elif outcome in ADVANCED:
        store.set_status(conn, external_id, store.Status.INTERVIEW, detail[:200])
    elif outcome == GHOSTED:
        store.set_status(conn, external_id, store.Status.GHOSTED, detail[:200])
    else:
        store.log_event(conn, external_id, f"outcome:{outcome}", detail[:200])
    return True


def sweep_ghosted(conn: sqlite3.Connection, days: int = GHOST_AFTER_DAYS) -> list[str]:
    """Label long silences as negative examples.

    Without this the training set is only the applications that bothered to
    reply, which is a biased slice — a company that replies at all is already
    different from one that does not.

    Reversible by design: the row is written with source='derived', so a reply
    arriving in month three still records on top of it and wins the label.
    """
    ensure_schema(conn)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT a.external_id FROM applications a
        WHERE a.applied_at < ?
          AND NOT EXISTS (
              SELECT 1 FROM outcomes o
              WHERE o.external_id = a.external_id AND o.outcome != 'ack'
          )
        """,
        (cutoff,),
    ).fetchall()
    marked = []
    for row in rows:
        if record(conn, row["external_id"], GHOSTED, source="derived",
                  detail=f"no reply after {days} days"):
            marked.append(row["external_id"])
    return marked


# ── Reading back ───────────────────────────────────────────────────────────────

_RANK = {GHOSTED: 0, REJECTED: 1, INTERVIEW: 2, OFFER: 3}


def dataset(conn: sqlite3.Connection, include_ghosted: bool = True) -> list[dict]:
    """Labelled training rows: frozen features plus what came back.

    One row per application, carrying its best outcome. `ack` is not terminal —
    an acknowledgement is the ATS confirming receipt, not a company forming an
    opinion — so it is kept as a feature instead of a label.
    """
    ensure_schema(conn)
    apps = conn.execute("SELECT * FROM applications ORDER BY applied_at").fetchall()
    rows: list[dict] = []
    for app in apps:
        events = conn.execute(
            "SELECT * FROM outcomes WHERE external_id=? ORDER BY at",
            (app["external_id"],),
        ).fetchall()
        terminal = [e for e in events if e["outcome"] != ACK]
        if not terminal:
            continue

        # Best outcome wins the label: an interview followed by a later
        # rejection is still evidence the application itself worked.
        best = max(terminal, key=lambda e: _RANK.get(e["outcome"], 0))
        if best["outcome"] == GHOSTED and not include_ghosted:
            continue

        try:
            feats = dict(json.loads(app["features"]))
        except (json.JSONDecodeError, TypeError):
            continue
        feats["acknowledged"] = any(e["outcome"] == ACK for e in events)

        signals: list[str] = []
        for event in terminal:
            try:
                signals.extend(json.loads(event["signals"] or "[]"))
            except json.JSONDecodeError:
                pass

        rows.append({
            "external_id": app["external_id"],
            "company": app["company"],
            "title": app["title"],
            "applied_at": app["applied_at"],
            "outcome": best["outcome"],
            "advanced": best["outcome"] in ADVANCED,
            "stage": best["stage"] or "",
            "source": best["source"],
            "days_to_outcome": _days_between(best["at"], app["applied_at"]),
            "reason_signals": sorted(set(signals)),
            "reconstructed": bool(feats.pop("reconstructed", False)),
            "features": feats,
        })
    return rows


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Applications still waiting — sent, no terminal outcome yet."""
    ensure_schema(conn)
    return conn.execute(
        """
        SELECT a.* FROM applications a
        WHERE NOT EXISTS (
            SELECT 1 FROM outcomes o
            WHERE o.external_id = a.external_id AND o.outcome != 'ack'
        )
        ORDER BY a.applied_at
        """
    ).fetchall()


def find_applied(conn: sqlite3.Connection, needle: str):
    """Resolve a job by external id, url, or an unambiguous company/title text.

    Returns None when the text matches more than one application. Guessing
    which of two Anthropic applications a rejection belongs to would corrupt
    the training set silently, which is worse than asking.
    """
    ident = (needle or "").strip()
    if not ident:
        return None
    hit = conn.execute("SELECT * FROM jobs WHERE external_id=?", (ident,)).fetchone()
    if hit:
        return hit
    canon = canonical_url(ident)
    if canon:
        hit = conn.execute(
            "SELECT * FROM jobs WHERE canonical_url=? AND canonical_url != ''", (canon,)
        ).fetchone()
        if hit:
            return hit
    like = f"%{ident.lower()}%"
    matches = conn.execute(
        """
        SELECT * FROM jobs
        WHERE applied_at IS NOT NULL
          AND (LOWER(company) LIKE ? OR LOWER(title) LIKE ?
               OR LOWER(company || ' ' || title) LIKE ?)
        """,
        (like, like, like),
    ).fetchall()
    return matches[0] if len(matches) == 1 else None

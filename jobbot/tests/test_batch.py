"""Tests for the export → draft → import round trip."""

from __future__ import annotations

import json

import pytest

from jobbot import batch, store
from jobbot.boards import Posting
from jobbot.profile import Profile
from jobbot.scoring import score_posting
from jobbot.tailor import FRAMINGS


@pytest.fixture
def conn(tmp_path):
    with store.session(tmp_path / "test.db") as c:
        yield c


@pytest.fixture
def profile():
    return Profile(
        first_name="Quintin",
        last_name="Fletcher",
        email="q@example.com",
        city="Los Angeles",
        state="CA",
        background="Builds ML systems.",
        narrative="Started college young and learned by building.",
        skills=["Python", "PostgreSQL"],
        projects=[
            {
                "name": "Sports Analytics",
                "stack": ["Python", "FastAPI"],
                "description": "Multi-sport prediction platform.",
                "impact": "44x pipeline speedup.",
            }
        ],
    )


def seed(conn, ext_id: str, title="Data Engineer"):
    posting = Posting(
        external_id=ext_id,
        title=title,
        company="acme",
        location="Los Angeles, CA",
        url=f"https://example.com/{ext_id}",
        description="Python and SQL. No degree required.",
        ats="greenhouse",
        board_slug="acme",
    )
    score, verdict, site = score_posting(posting.title, posting.description, posting.location)
    store.upsert_job(conn, posting.as_row(), score, verdict, site)
    return conn.execute(
        "SELECT * FROM jobs WHERE external_id = ?", (ext_id,)
    ).fetchone()


def block(framing: str, body: str = "") -> str:
    return f"{batch.LETTER_OPEN}{framing} -->\n{body}\n{batch.LETTER_CLOSE}"


def fill(text: str, framing: str, body: str) -> str:
    """Replace one empty framing block with a drafted letter."""
    return text.replace(block(framing), block(framing, body), 1)


# ── Export ─────────────────────────────────────────────────────────────────────

def test_export_emits_one_letter_per_job_by_default(profile, conn, tmp_path):
    rows = [seed(conn, "a:1"), seed(conn, "a:2", title="ML Engineer")]
    path = batch.export(profile, rows, out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")

    assert "a:1" in text and "a:2" in text
    assert text.count(f"{batch.LETTER_OPEN}projects_only -->") == 2
    assert f"{batch.LETTER_OPEN}neutral -->" not in text
    assert "250-400" in text or "250–400" in text
    assert "ChatGPT Web" in text
    assert "Never invent experience" in text


def test_export_can_still_emit_all_framings(profile, conn, tmp_path):
    rows = [seed(conn, "a:1"), seed(conn, "a:2", title="ML Engineer")]
    path = batch.export(profile, rows, out_dir=tmp_path, framings=list(FRAMINGS))
    text = path.read_text(encoding="utf-8")
    for framing in FRAMINGS:
        assert text.count(f"{batch.LETTER_OPEN}{framing} -->") == 2


def test_export_includes_full_posting(profile, conn, tmp_path):
    long_desc = "Python and SQL. " + ("Requirements follow. " * 400)
    posting_row = seed(conn, "a:1")
    conn.execute(
        "UPDATE jobs SET description=? WHERE external_id='a:1'", (long_desc,)
    )
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    path = batch.export(profile, [row], out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert long_desc.strip() in text
    assert "[…truncated]" not in text
    assert "Sports Analytics" in text


def test_export_includes_profile_and_posting(profile, conn, tmp_path):
    path = batch.export(profile, [seed(conn, "a:1")], out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "Sports Analytics" in text
    assert "Python and SQL" in text
    assert "Started college young" in text  # narrative reaches the drafter


def test_export_forbids_disability_disclosure(profile, conn, tmp_path):
    """The batch must carry the no-disclosure rule to whoever drafts it."""
    path = batch.export(profile, [seed(conn, "a:1")], out_dir=tmp_path)
    text = path.read_text(encoding="utf-8").lower()
    assert "disability" in text and "candidate's to make" in text


def test_export_does_not_overwrite_an_existing_batch(profile, conn, tmp_path):
    rows = [seed(conn, "a:1")]
    first = batch.export(profile, rows, out_dir=tmp_path)
    second = batch.export(profile, rows, out_dir=tmp_path)
    assert first != second
    assert first.exists() and second.exists()


def test_round_trip_default_one_letter(profile, conn, tmp_path):
    path = batch.export(profile, [seed(conn, "a:1")], out_dir=tmp_path)
    text = fill(path.read_text(encoding="utf-8"), "projects_only", "A tailored letter.")
    path.write_text(text, encoding="utf-8")
    imported, empty = batch.import_letters(path, conn)
    assert imported == 1
    assert empty == 0
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["cover_letter"] == "A tailored letter."
    assert json.loads(row["letter_variants"]) == {"projects_only": "A tailored letter."}


def test_export_includes_optional_education_context(profile, conn, tmp_path):
    profile.education = "Some college, computer science"
    profile.major = "Computer Science"
    profile.coursework = ["Data structures"]
    path = batch.export(profile, [seed(conn, "a:1")], out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "Some college, computer science" in text
    assert "Computer Science" in text
    assert "Data structures" in text

def test_round_trip_all_framings(profile, conn, tmp_path):
    rows = [seed(conn, "a:1"), seed(conn, "a:2", title="ML Engineer")]
    path = batch.export(profile, rows, out_dir=tmp_path, framings=list(FRAMINGS))

    text = path.read_text(encoding="utf-8")
    for framing in FRAMINGS:
        while block(framing) in text:
            text = fill(text, framing, f"Letter using {framing}.")
    path.write_text(text, encoding="utf-8")

    imported, empty = batch.import_letters(path, conn)
    assert imported == 2
    assert empty == 0

    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    variants = json.loads(row["letter_variants"])
    assert set(variants) == set(FRAMINGS)
    assert variants["narrative"] == "Letter using narrative."
    # The first framing becomes the active letter until you pick another.
    assert row["cover_letter"] == variants[next(iter(FRAMINGS))]


def test_partial_batch_imports_what_is_finished(profile, conn, tmp_path):
    """A half-finished batch must import cleanly rather than saving blanks."""
    rows = [seed(conn, "a:1"), seed(conn, "a:2", title="ML Engineer")]
    path = batch.export(profile, rows, out_dir=tmp_path)

    text = fill(path.read_text(encoding="utf-8"), "projects_only", "Only the first.")
    path.write_text(text, encoding="utf-8")

    imported, empty = batch.import_letters(path, conn)
    assert imported == 1
    assert empty == 1

    second = conn.execute("SELECT * FROM jobs WHERE external_id='a:2'").fetchone()
    assert not second["cover_letter"]


def test_one_framing_drafted_is_enough(profile, conn, tmp_path):
    path = batch.export(profile, [seed(conn, "a:1")], out_dir=tmp_path, framings=list(FRAMINGS))
    text = fill(path.read_text(encoding="utf-8"), "neutral", "Neutral version.")
    path.write_text(text, encoding="utf-8")

    batch.import_letters(path, conn)
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert json.loads(row["letter_variants"]) == {"neutral": "Neutral version."}


def test_unknown_job_ids_are_ignored(profile, conn, tmp_path):
    path = batch.export(profile, [seed(conn, "a:1")], out_dir=tmp_path)
    text = path.read_text(encoding="utf-8").replace("a:1", "ghost:999")
    text = fill(text, "projects_only", "Letter for a job not in the DB.")
    path.write_text(text, encoding="utf-8")

    imported, _ = batch.import_letters(path, conn)
    assert imported == 0


def test_parse_ignores_empty_placeholders():
    text = (
        f"{batch.JOB_MARKER}x:1 -->\n{block('neutral')}\n"
        f"{batch.JOB_MARKER}x:2 -->\n{block('neutral', 'Real letter.')}\n"
    )
    assert batch.parse(text) == {"x:2": {"neutral": "Real letter."}}


# ── Selection ──────────────────────────────────────────────────────────────────

def test_select_letter_promotes_a_variant(conn):
    seed(conn, "a:1")
    store.save_materials(
        conn,
        "a:1",
        variants={"projects_only": "No education.", "narrative": "The arc."},
    )

    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["cover_letter"] == "No education."

    assert store.select_letter(conn, "a:1", "narrative") == "The arc."
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["cover_letter"] == "The arc."


def test_select_unknown_framing_is_a_noop(conn):
    seed(conn, "a:1")
    store.save_materials(conn, "a:1", variants={"neutral": "Only one."})
    assert store.select_letter(conn, "a:1", "narrative") == ""
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["cover_letter"] == "Only one."


# ── Data loss regressions ──────────────────────────────────────────────────────

def test_saving_never_overwrites_an_existing_letter(conn):
    """The review app's brief button destroyed a real drafted letter."""
    seed(conn, "a:1")
    store.save_materials(conn, "a:1", variants={"projects_only": "The real letter."})

    # A later generation attempt for the same framing must not win.
    store.save_materials(conn, "a:1", variants={"projects_only": "# Brief — checklist"})

    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert json.loads(row["letter_variants"])["projects_only"] == "The real letter."


def test_saving_merges_rather_than_replaces(conn):
    seed(conn, "a:1")
    store.save_materials(conn, "a:1", variants={"projects_only": "First."})
    store.save_materials(conn, "a:1", variants={"narrative": "Second."})

    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    variants = json.loads(row["letter_variants"])
    assert variants == {"projects_only": "First.", "narrative": "Second."}


def test_overwrite_is_possible_when_asked(conn):
    seed(conn, "a:1")
    store.save_materials(conn, "a:1", variants={"neutral": "Old."})
    store.save_materials(conn, "a:1", variants={"neutral": "New."}, overwrite=True)

    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert json.loads(row["letter_variants"])["neutral"] == "New."


def test_empty_letters_never_replace_real_ones(conn):
    seed(conn, "a:1")
    store.save_materials(conn, "a:1", variants={"neutral": "Real."})
    store.save_materials(conn, "a:1", variants={"neutral": ""}, overwrite=True)

    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert json.loads(row["letter_variants"])["neutral"] == "Real."


def test_offline_brief_scans_the_whole_posting():
    """Truncating to 1500 chars reported "no direct overlap" for a posting
    whose requirements section listed Python, LightGBM, XGBoost and MLflow."""
    from jobbot import tailor

    profile = Profile(skills=["Python", "LightGBM", "XGBoost", "MLflow"])
    padding = "About the company. " * 120  # pushes requirements past 1500 chars
    description = padding + "You have strong Python skills and experience with "
    description += "gradient-boosted trees like LightGBM/XGBoost. We use MLflow."

    assert len(padding) > 1500
    brief = tailor.offline_brief(profile, "ML Engineer", "acme", description, "open")
    assert "No direct overlap" not in brief
    assert "lightgbm" in brief.lower()


def test_offline_brief_is_not_indented():
    """textwrap.dedent is a no-op once an interpolated value adds its own
    newlines, which left the whole brief indented and rendering as a code
    block."""
    from jobbot import tailor

    profile = Profile(
        skills=["Python"],
        projects=[{"name": "Sports Analytics"}, {"name": "Forecasting Engine"}],
    )
    brief = tailor.offline_brief(profile, "ML Engineer", "acme", "Python.", "open")

    for line in brief.splitlines():
        if line.startswith("  - "):
            continue  # project bullets are intentionally indented
        assert line == line.lstrip(), f"unexpected indent: {line!r}"
    assert brief.startswith("# Brief")


def test_migration_adds_column_to_an_existing_database(tmp_path):
    """An upgrade must not break a database that already holds history."""
    import sqlite3

    db = tmp_path / "old.db"
    with store.session(db) as conn:
        seed(conn, "a:1")
        store.set_status(conn, "a:1", store.Status.APPLIED)

    # Simulate a pre-upgrade database by dropping the newer column.
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE jobs DROP COLUMN letter_variants")
    raw.commit()
    raw.close()

    with store.session(db) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert "letter_variants" in cols
        row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
        assert row["status"] == store.Status.APPLIED.value  # history survived

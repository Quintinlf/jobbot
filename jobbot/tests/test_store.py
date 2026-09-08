"""Tests for persistence and the application funnel."""

from __future__ import annotations

import json

import pytest

from jobbot import store
from jobbot.boards import Posting
from jobbot.scoring import score_posting


@pytest.fixture
def conn(tmp_path):
    with store.session(tmp_path / "test.db") as c:
        yield c


def add(conn, ext_id: str, title="Data Engineer", desc="Python. No degree required."):
    posting = Posting(
        external_id=ext_id,
        title=title,
        company="acme",
        location="Los Angeles, CA",
        url=f"https://example.com/{ext_id}",
        description=desc,
        ats="greenhouse",
        board_slug="acme",
    )
    score, verdict, site = score_posting(posting.title, posting.description, posting.location)
    return store.upsert_job(conn, posting.as_row(), score, verdict, site)


def test_upsert_is_idempotent(conn):
    assert add(conn, "a:1") is True
    assert add(conn, "a:1") is False
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1


def test_rescrape_preserves_your_decisions(conn):
    """A refresh must never undo a status you set or a note you wrote."""
    add(conn, "a:1")
    store.set_status(conn, "a:1", store.Status.APPLIED, note="referred by Dana")

    add(conn, "a:1")  # simulate the next refresh seeing it again

    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["status"] == store.Status.APPLIED.value
    assert row["notes"] == "referred by Dana"
    assert row["applied_at"] is not None


def test_applied_count_survives_status_progression(conn):
    """applied -> interview must not stop counting as an application.

    Status is a single column, so counting applications by status would drop
    this job and inflate the response rate to a meaningless number.
    """
    for i in range(4):
        add(conn, f"a:{i}")
        store.set_status(conn, f"a:{i}", store.Status.APPLIED)

    store.set_status(conn, "a:0", store.Status.INTERVIEW)
    store.set_status(conn, "a:1", store.Status.REJECTED)

    stats = store.stats(conn)
    assert stats["applied"] == 4
    assert stats["responses"] == 2
    assert stats["interviews"] == 1
    assert stats["response_rate"] == 0.5
    assert stats["interview_rate"] == 0.25


def test_response_rate_is_none_before_any_application(conn):
    add(conn, "a:1")
    assert store.stats(conn)["response_rate"] is None


def test_queue_hides_gated_postings_by_default(conn):
    add(conn, "open:1", desc="Python. No degree required.")
    add(conn, "gated:1", desc="Must be currently enrolled in a degree program.")

    ids = {r["external_id"] for r in store.queue(conn)}
    assert ids == {"open:1"}

    ids_all = {r["external_id"] for r in store.queue(conn, include_gated=True)}
    assert ids_all == {"open:1", "gated:1"}


def test_queue_excludes_knocked_out_titles(conn):
    add(conn, "ko:1", title="Senior Software Engineer")
    add(conn, "ok:1", title="Data Engineer")

    ids = {r["external_id"] for r in store.queue(conn, include_gated=True)}
    assert ids == {"ok:1"}


def test_queue_orders_by_score(conn):
    add(conn, "low:1", title="Analytics", desc="Bachelor's degree preferred.")
    add(conn, "high:1", title="Machine Learning Engineer",
        desc="No degree required. Python.")

    rows = store.queue(conn)
    assert rows[0]["external_id"] == "high:1"


def test_skipped_jobs_leave_the_queue(conn):
    add(conn, "a:1")
    store.set_status(conn, "a:1", store.Status.SKIPPED)
    assert store.queue(conn) == []


def test_events_are_logged(conn):
    add(conn, "a:1")
    store.set_status(conn, "a:1", store.Status.APPLIED)

    kinds = [
        r["kind"]
        for r in conn.execute("SELECT kind FROM events WHERE external_id='a:1'")
    ]
    assert "discovered" in kinds
    assert "status:applied" in kinds


def test_materials_round_trip(conn):
    add(conn, "a:1")
    store.save_materials(conn, "a:1", "Dear team…", "- lead with the ETL project")

    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["cover_letter"] == "Dear team…"
    assert "ETL" in row["resume_notes"]


def test_cross_source_dedup_by_ats_url(conn):
    """A Jobot email for the same Greenhouse job must not create a second row."""
    posting = Posting(
        external_id="greenhouse:acme:4242",
        title="Data Engineer",
        company="Acme",
        location="Los Angeles, CA",
        url="https://job-boards.greenhouse.io/acme/jobs/4242",
        description="Python. No degree required.",
        ats="greenhouse",
        board_slug="acme",
    )
    s, v, site = score_posting(posting.title, posting.description, posting.location)
    assert store.upsert_job(conn, posting.as_row(), s, v, site) is True

    email_row = Posting(
        external_id="email:jobot:zzzz",
        title="Data Engineer",
        company="Acme",
        location="Los Angeles, CA",
        url="https://job-boards.greenhouse.io/acme/jobs/4242?utm_source=jobot",
        description="Python and SQL. No degree required.",
        ats="email",
        board_slug="jobot",
    ).as_row()
    email_row["source_message_id"] = "mid-jobot"
    assert store.upsert_job(conn, email_row, s, v, site) is False

    n = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    assert n == 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["external_id"] == "greenhouse:acme:4242"
    assert row["ats"] == "greenhouse"
    sources = json.loads(row["sources"])
    assert any(e.get("source") == "email" for e in sources)
    kinds = [
        r["kind"]
        for r in conn.execute("SELECT kind FROM events WHERE external_id=?",
                              ("greenhouse:acme:4242",))
    ]
    assert "discovered_from" in kinds


def test_cross_source_dedup_by_company_title_location(conn):
    a = Posting(
        external_id="board:1",
        title="Analytics Engineer",
        company="Southwind",
        location="Santa Monica, CA",
        url="https://example.com/board/1",
        description="SQL. No degree required.",
        ats="greenhouse",
        board_slug="southwind",
    )
    s, v, site = score_posting(a.title, a.description, a.location)
    store.upsert_job(conn, a.as_row(), s, v, site)

    b = Posting(
        external_id="email:indeed:1",
        title="Analytics Engineer",
        company="Southwind",
        location="Santa Monica, CA",
        url="https://www.indeed.com/viewjob?jk=abc",
        description="SQL and dbt.",
        ats="email",
        board_slug="indeed",
    )
    assert store.upsert_job(conn, b.as_row(), s, v, site) is False
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1


def test_chatgpt_paste_saves_into_letter_variants(conn):
    add(conn, "a:1")
    store.save_materials(conn, "a:1", variants={"chatgpt": "Pasted letter from ChatGPT."})
    store.select_letter(conn, "a:1", "chatgpt")
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["cover_letter"] == "Pasted letter from ChatGPT."
    store.save_materials(
        conn, "a:1", variants={"chatgpt": "A replacement that must not win."}
    )
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert json.loads(row["letter_variants"])["chatgpt"] == "Pasted letter from ChatGPT."

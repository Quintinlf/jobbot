"""Tests for the apply queue's selection and bookkeeping.

The browser half is exercised against live forms; what is worth pinning down
here is which jobs get offered, in what order, and what each answer records —
because a mistake there means either applying twice or losing track of an
application you actually sent.
"""

from __future__ import annotations

import pytest

from jobbot import apply_session, store
from jobbot.boards import Posting
from jobbot.scoring import score_posting


@pytest.fixture
def conn(tmp_path):
    with store.session(tmp_path / "test.db") as c:
        yield c


def add(conn, ext_id, title="Data Engineer", letter="A letter.", score_desc=None):
    posting = Posting(
        external_id=ext_id,
        title=title,
        company="acme",
        location="Remote - US",
        url=f"https://example.com/{ext_id}",
        description=score_desc or "Python and SQL. No degree required. Fully remote.",
        ats="greenhouse",
        board_slug="acme",
    )
    s, v, site = score_posting(posting.title, posting.description, posting.location)
    store.upsert_job(conn, posting.as_row(), s, v, site)
    if letter:
        store.save_materials(conn, ext_id, variants={"projects_only": letter})
    return ext_id


def ready(conn, min_score=1, count=10):
    """Mirror of the selection cmd_apply performs."""
    rows = store.queue(conn, limit=200, min_score=min_score)
    return [r for r in rows if r["cover_letter"]][:count]


# ── Selection ──────────────────────────────────────────────────────────────────

def test_only_jobs_with_letters_are_offered(conn):
    add(conn, "with:1")
    add(conn, "without:1", letter="")
    ids = [r["external_id"] for r in ready(conn)]
    assert ids == ["with:1"]


def test_applied_jobs_do_not_come_back(conn):
    add(conn, "a:1")
    add(conn, "a:2", title="ML Engineer")
    store.set_status(conn, "a:1", store.Status.APPLIED)

    ids = [r["external_id"] for r in ready(conn)]
    assert "a:1" not in ids
    assert "a:2" in ids


def test_skipped_jobs_do_not_come_back(conn):
    add(conn, "a:1")
    store.set_status(conn, "a:1", store.Status.SKIPPED)
    assert ready(conn) == []


def test_left_for_later_stays_in_the_queue(conn):
    """"Leave it, decide later" must not change status."""
    add(conn, "a:1")
    before = conn.execute(
        "SELECT status FROM jobs WHERE external_id='a:1'"
    ).fetchone()["status"]
    assert before == store.Status.NEW.value
    assert [r["external_id"] for r in ready(conn)] == ["a:1"]


def test_count_limits_the_sitting(conn):
    for i in range(8):
        add(conn, f"a:{i}")
    assert len(ready(conn, count=3)) == 3


def test_best_first(conn):
    add(conn, "low:1", title="Analytics", score_desc="Bachelor's degree preferred.")
    add(conn, "high:1", title="Machine Learning Engineer",
        score_desc="No degree required. Python. Fully remote.")
    assert ready(conn)[0]["external_id"] == "high:1"


def test_min_score_filters(conn):
    add(conn, "a:1")
    high = ready(conn, min_score=1)[0]["score"]
    assert ready(conn, min_score=high + 50) == []


# ── Bookkeeping ────────────────────────────────────────────────────────────────

def test_applying_records_a_timestamp(conn):
    add(conn, "a:1")
    store.set_status(conn, "a:1", store.Status.APPLIED)
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["applied_at"] is not None
    assert store.stats(conn)["applied"] == 1


def test_a_session_of_mixed_answers(conn):
    """Four jobs: applied, skipped, left, applied."""
    for i in range(4):
        add(conn, f"a:{i}")

    store.set_status(conn, "a:0", store.Status.APPLIED)
    store.set_status(conn, "a:1", store.Status.SKIPPED)
    # a:2 left alone
    store.set_status(conn, "a:3", store.Status.APPLIED)

    stats = store.stats(conn)
    assert stats["applied"] == 2
    assert [r["external_id"] for r in ready(conn)] == ["a:2"]


def test_events_record_each_decision(conn):
    add(conn, "a:1")
    store.set_status(conn, "a:1", store.Status.APPLIED)
    kinds = [r["kind"] for r in
             conn.execute("SELECT kind FROM events WHERE external_id='a:1'")]
    assert "status:applied" in kinds


def test_letters_survive_the_status_change(conn):
    """Marking applied must not disturb the letter that was sent."""
    add(conn, "a:1", letter="The letter I sent.")
    store.set_status(conn, "a:1", store.Status.APPLIED)
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["cover_letter"] == "The letter I sent."


def test_previously_opened_postings_are_hidden_by_default(conn):
    """The "it keeps giving me the same jobs" bug. A posting opened by
    prepare-applications, submitted on the company's own site, and never
    confirmed back in the terminal keeps its `new` status forever -- so it
    returned to the top of the queue on every run. Measured 2026-09-05: 19 of
    the 22 postings in the ready queue had already been opened."""
    old = add(conn, "gh:acme:1", title="Data Engineer")
    new = add(conn, "gh:beta:1", title="ML Engineer")
    # one_per_company would otherwise collapse these two into one pick.
    conn.execute("UPDATE jobs SET company='beta' WHERE external_id=?", (new,))

    both = {r["external_id"] for r in apply_session.ready_rows(conn, count=10)}
    assert both == {old, new}

    store.log_event(conn, old, "prepared", "tab filled and left open")

    fresh = {r["external_id"] for r in apply_session.ready_rows(conn, count=10)}
    assert fresh == {new}, "an already-opened posting must not come back by default"

    everything = {
        r["external_id"]
        for r in apply_session.ready_rows(conn, count=10, include_opened=True)
    }
    assert everything == {old, new}, "--include-opened must bring it back"


def test_already_opened_reports_what_was_prepared(conn):
    """Hidden, not forgotten: being opened is not proof of having applied, so
    the ids stay retrievable rather than the rows being marked applied."""
    ext = add(conn, "gh:acme:1")
    assert apply_session.already_opened(conn) == set()
    store.log_event(conn, ext, "prepared", "tab filled")
    assert apply_session.already_opened(conn) == {ext}
    row = conn.execute(
        "SELECT status, applied_at FROM jobs WHERE external_id=?", (ext,)
    ).fetchone()
    assert row["status"] == "new" and row["applied_at"] is None

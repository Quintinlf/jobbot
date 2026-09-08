"""Outcome logging: frozen features, event history, derived ghosting."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from jobbot import outcomes, store
from jobbot.boards import Posting
from jobbot.scoring import score_posting


@pytest.fixture
def conn(tmp_path):
    with store.session(tmp_path / "test.db") as c:
        yield c


def add(conn, ext_id="a:1", title="Data Engineer", company="acme",
        desc="Python and SQL. No degree required.", location="Los Angeles, CA"):
    posting = Posting(
        external_id=ext_id,
        title=title,
        company=company,
        location=location,
        url=f"https://boards.greenhouse.io/{company}/jobs/{ext_id.split(':')[-1]}",
        description=desc,
        ats="greenhouse",
        board_slug=company,
    )
    score, verdict, site = score_posting(posting.title, posting.description, posting.location)
    store.upsert_job(conn, posting.as_row(), score, verdict, site)
    return ext_id


def test_applying_freezes_the_features(conn):
    add(conn)
    store.set_status(conn, "a:1", store.Status.APPLIED)

    row = conn.execute("SELECT * FROM applications WHERE external_id='a:1'").fetchone()
    assert row is not None
    feats = json.loads(row["features"])
    assert feats["gate"]
    assert feats["title_level"] == "unmarked"
    assert feats["location_bucket"] == "home_metro"


def test_snapshot_survives_the_job_row_changing(conn):
    """A rescore after applying must not rewrite what was true at send time."""
    add(conn)
    store.set_status(conn, "a:1", store.Status.APPLIED)
    before = json.loads(
        conn.execute("SELECT features FROM applications WHERE external_id='a:1'")
        .fetchone()["features"]
    )

    conn.execute("UPDATE jobs SET score=1, description='' WHERE external_id='a:1'")
    store.set_status(conn, "a:1", store.Status.APPLIED)  # marked applied again

    after = json.loads(
        conn.execute("SELECT features FROM applications WHERE external_id='a:1'")
        .fetchone()["features"]
    )
    assert after == before
    assert after["score"] != 1


def test_rejection_records_event_and_status(conn):
    add(conn)
    store.set_status(conn, "a:1", store.Status.APPLIED)
    assert outcomes.record(conn, "a:1", outcomes.REJECTED,
                           detail="Unfortunately the role requires a bachelor's degree.")

    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["status"] == store.Status.REJECTED.value
    assert row["responded_at"]

    event = conn.execute("SELECT * FROM outcomes WHERE external_id='a:1'").fetchone()
    assert event["outcome"] == outcomes.REJECTED
    assert "degree" in json.loads(event["signals"])


def test_same_email_recorded_once(conn):
    add(conn)
    store.set_status(conn, "a:1", store.Status.APPLIED)
    assert outcomes.record(conn, "a:1", outcomes.REJECTED, message_id="m-1")
    assert outcomes.record(conn, "a:1", outcomes.REJECTED, message_id="m-1") is False
    assert conn.execute("SELECT COUNT(*) n FROM outcomes").fetchone()["n"] == 1


def test_manual_entries_are_not_deduped_against_each_other(conn):
    """Two hand-logged events on one job are real history, not a double write."""
    add(conn)
    store.set_status(conn, "a:1", store.Status.APPLIED)
    assert outcomes.record(conn, "a:1", outcomes.ACK)
    assert outcomes.record(conn, "a:1", outcomes.REJECTED)
    assert conn.execute("SELECT COUNT(*) n FROM outcomes").fetchone()["n"] == 2


def test_interview_then_rejection_still_labels_as_advanced(conn):
    add(conn)
    store.set_status(conn, "a:1", store.Status.APPLIED)
    outcomes.record(conn, "a:1", outcomes.INTERVIEW, message_id="m-1")
    outcomes.record(conn, "a:1", outcomes.REJECTED, message_id="m-2")

    rows = outcomes.dataset(conn)
    assert len(rows) == 1
    assert rows[0]["advanced"] is True
    assert rows[0]["outcome"] == outcomes.INTERVIEW


def test_ack_alone_is_not_an_outcome(conn):
    """A receipt confirms delivery. It is not the company forming a view."""
    add(conn)
    store.set_status(conn, "a:1", store.Status.APPLIED)
    outcomes.record(conn, "a:1", outcomes.ACK, message_id="m-1")

    assert outcomes.dataset(conn) == []
    assert len(outcomes.pending(conn)) == 1


def test_ghost_sweep_only_touches_old_silent_applications(conn):
    add(conn, "a:1")
    add(conn, "a:2")
    store.set_status(conn, "a:1", store.Status.APPLIED)
    store.set_status(conn, "a:2", store.Status.APPLIED)

    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    conn.execute("UPDATE applications SET applied_at=? WHERE external_id='a:1'", (old,))

    ghosted = outcomes.sweep_ghosted(conn, days=45)
    assert ghosted == ["a:1"]

    rows = {r["external_id"]: r for r in outcomes.dataset(conn)}
    assert rows["a:1"]["outcome"] == outcomes.GHOSTED
    assert rows["a:1"]["advanced"] is False
    assert "a:2" not in rows


def test_a_late_reply_overrides_a_derived_ghost(conn):
    add(conn)
    store.set_status(conn, "a:1", store.Status.APPLIED)
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    conn.execute("UPDATE applications SET applied_at=? WHERE external_id='a:1'", (old,))
    outcomes.sweep_ghosted(conn, days=45)

    outcomes.record(conn, "a:1", outcomes.INTERVIEW, source="email", message_id="m-9")

    row = outcomes.dataset(conn)[0]
    assert row["outcome"] == outcomes.INTERVIEW
    assert row["advanced"] is True


def test_backfill_flags_reconstructed_rows(conn):
    """Applications sent before snapshots existed must not pose as frozen."""
    add(conn)
    conn.execute(
        "UPDATE jobs SET status='applied', applied_at='2026-01-05T00:00:00+00:00'"
        " WHERE external_id='a:1'"
    )
    assert outcomes.backfill_snapshots(conn) == 1
    assert outcomes.backfill_snapshots(conn) == 0

    outcomes.record(conn, "a:1", outcomes.REJECTED)
    assert outcomes.dataset(conn)[0]["reconstructed"] is True


def test_reason_signals_read_the_letter_not_the_politeness():
    text = ("Thank you for your interest. We have decided to move forward with "
            "other candidates whose experience more closely aligns with the role.")
    assert outcomes.reason_signals(text) == ["stronger_candidates"]

    text = "This position requires a bachelor's degree in computer science."
    assert "degree" in outcomes.reason_signals(text)


def test_find_applied_refuses_to_guess_between_two(conn):
    add(conn, "a:1", company="acme", title="Data Engineer")
    add(conn, "a:2", company="acme", title="Data Analyst")
    store.set_status(conn, "a:1", store.Status.APPLIED)
    store.set_status(conn, "a:2", store.Status.APPLIED)

    assert outcomes.find_applied(conn, "acme") is None
    assert outcomes.find_applied(conn, "Data Analyst")["external_id"] == "a:2"
    assert outcomes.find_applied(conn, "a:1")["external_id"] == "a:1"


def test_unknown_outcome_is_rejected(conn):
    add(conn)
    store.set_status(conn, "a:1", store.Status.APPLIED)
    with pytest.raises(ValueError):
        outcomes.record(conn, "a:1", "maybe")

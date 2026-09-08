"""Recording Applied from the page instead of from a keypress."""

from __future__ import annotations

import pytest

from jobbot import __main__ as cli
from jobbot import autofill, config, store
from jobbot.boards import Posting
from jobbot.scoring import score_posting


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cli.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with store.session() as conn:
        posting = Posting(
            external_id="gh:acme:1", title="Software Engineer", company="acme",
            location="Remote - US", url="https://example.invalid/1",
            description="Python. No degree required.",
            ats="greenhouse", board_slug="acme",
        )
        s, v, site = score_posting(posting.title, posting.description, posting.location)
        store.upsert_job(conn, posting.as_row(), s, v, site)
    return tmp_path


class _Page:
    def __init__(self, closed=False):
        self.closed = closed
        self.close_calls = 0

    def is_closed(self):
        return self.closed

    def close(self):
        self.close_calls += 1
        self.closed = True

    def wait_for_timeout(self, ms):
        raise RuntimeError("stop the loop after one pass")


def _row(conn):
    return conn.execute(
        "SELECT * FROM jobs WHERE external_id = 'gh:acme:1'"
    ).fetchone()


def _prepared(page):
    with store.session() as conn:
        return [{"page": page, "row": _row(conn)}]


def test_a_confirmed_page_records_applied(db, monkeypatch):
    monkeypatch.setattr(
        autofill, "submission_state",
        lambda page: (autofill.CONFIRMED, "page says: 'application received'"),
    )
    page = _Page()
    applied, unresolved = cli._watch_prepared(_prepared(page), poll_ms=1)

    assert applied == 1
    assert unresolved == []
    assert page.close_calls == 1
    with store.session() as conn:
        assert _row(conn)["status"] == store.Status.APPLIED.value


def test_the_evidence_is_kept(db, monkeypatch):
    """A row recorded by the detector has to say what the detector saw."""
    monkeypatch.setattr(
        autofill, "submission_state",
        lambda page: (autofill.CONFIRMED, "url says so: /confirmation"),
    )
    cli._watch_prepared(_prepared(_Page()), poll_ms=1)

    with store.session() as conn:
        events = {
            r["kind"]: r["detail"] for r in conn.execute(
                "SELECT kind, detail FROM events WHERE external_id = 'gh:acme:1'"
            )
        }
    assert "confirmation" in events["applied_confirmed"]


def test_an_open_form_records_nothing(db, monkeypatch):
    """FORM_OPEN means it is still sitting there unsubmitted."""
    monkeypatch.setattr(
        autofill, "submission_state",
        lambda page: (autofill.FORM_OPEN, "the form is still on screen"),
    )
    applied, unresolved = cli._watch_prepared(_prepared(_Page()), poll_ms=1)

    assert applied == 0
    assert [r["external_id"] for r in unresolved] == ["gh:acme:1"]
    with store.session() as conn:
        assert _row(conn)["status"] == store.Status.NEW.value


def test_unknown_records_nothing(db, monkeypatch):
    """The detector cannot read every ATS. Guessing here would either invent
    applications or throw real ones away."""
    monkeypatch.setattr(
        autofill, "submission_state",
        lambda page: (autofill.UNKNOWN, "cannot tell from this page"),
    )
    applied, unresolved = cli._watch_prepared(_prepared(_Page()), poll_ms=1)

    assert applied == 0
    assert len(unresolved) == 1


def test_a_rejected_form_is_reported_but_not_recorded(db, monkeypatch, capsys):
    monkeypatch.setattr(
        autofill, "submission_state",
        lambda page: (autofill.ERRORS, "Please enter a valid phone number"),
    )
    applied, unresolved = cli._watch_prepared(_prepared(_Page()), poll_ms=1)

    assert applied == 0
    assert "valid phone number" in capsys.readouterr().out
    with store.session() as conn:
        assert _row(conn)["status"] == store.Status.NEW.value


def test_a_tab_closed_by_hand_is_not_an_application(db, monkeypatch):
    """Closing a tab says nothing about whether it was sent."""
    monkeypatch.setattr(
        autofill, "submission_state",
        lambda page: (autofill.CONFIRMED, "should never be consulted"),
    )
    applied, unresolved = cli._watch_prepared(_prepared(_Page(closed=True)), poll_ms=1)

    assert applied == 0
    assert [r["external_id"] for r in unresolved] == ["gh:acme:1"]
    with store.session() as conn:
        assert _row(conn)["status"] == store.Status.NEW.value


def test_hunt_watches_by_default():
    import inspect
    src = inspect.getsource(cli.cmd_hunt)
    assert "watch=not args.confirm_by_key" in src

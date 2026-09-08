"""The new commands end to end, against a temporary database."""

from __future__ import annotations

import json

import pytest

from jobbot import __main__ as cli
from jobbot import config, outcomes, store
from jobbot.boards import Posting
from jobbot.scoring import score_posting


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESUME_DIR", tmp_path / "resumes")
    monkeypatch.setattr(config, "OUTBOX_DIR", tmp_path / "outbox")
    return path


def seed(ext_id="greenhouse:acme:1", title="Data Engineer", company="acme"):
    with store.session() as conn:
        posting = Posting(
            external_id=ext_id,
            title=title,
            company=company,
            location="Los Angeles, CA",
            url=f"https://boards.greenhouse.io/{company}/jobs/1",
            description="Python and SQL. No degree required.",
            ats="greenhouse",
            board_slug=company,
        )
        score, verdict, site = score_posting(
            posting.title, posting.description, posting.location
        )
        store.upsert_job(conn, posting.as_row(), score, verdict, site)
        store.set_status(conn, ext_id, store.Status.APPLIED)
    return ext_id


def test_outcome_command_logs_a_rejection(db, capsys):
    ext = seed()
    code = cli.main([
        "outcome", ext, "rejected",
        "--note", "Unfortunately this role requires a bachelor's degree.",
    ])
    out = capsys.readouterr().out

    assert code == 0
    assert "Logged rejected" in out
    assert "degree" in out

    with store.session() as conn:
        row = conn.execute("SELECT * FROM outcomes").fetchone()
        assert row["outcome"] == outcomes.REJECTED
        assert row["source"] == "manual"


def test_outcome_command_refuses_an_unresolvable_job(db, capsys):
    seed()
    code = cli.main(["outcome", "no-such-job", "rejected"])

    assert code == 1
    assert "No single application matches" in capsys.readouterr().out


def test_outcome_command_is_idempotent(db, capsys):
    """Re-running the same command is a repeat, not a second rejection."""
    ext = seed()
    cli.main(["outcome", ext, "rejected", "--note", "x"])
    capsys.readouterr()
    cli.main(["outcome", ext, "rejected", "--note", "x"])

    assert "Already logged" in capsys.readouterr().out
    with store.session() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM outcomes").fetchone()["n"] == 1


def test_a_later_stage_is_new_history(db, capsys):
    """Rejected after a screen is a different event from rejected on paper."""
    ext = seed()
    cli.main(["outcome", ext, "interview", "--stage", "screen"])
    cli.main(["outcome", ext, "rejected", "--stage", "final"])
    capsys.readouterr()

    with store.session() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM outcomes").fetchone()["n"] == 2
        row = outcomes.dataset(conn)[0]
        assert row["advanced"] is True


def test_applications_command_lists_what_is_waiting(db, capsys):
    seed()
    assert cli.main(["applications"]) == 0
    out = capsys.readouterr().out
    assert "waiting" in out
    assert "acme" in out


def test_learn_command_reports_the_floor_rather_than_a_model(db, capsys):
    ext = seed()
    cli.main(["outcome", ext, "rejected"])
    capsys.readouterr()

    assert cli.main(["learn"]) == 0
    out = capsys.readouterr().out
    assert "No model yet" in out
    assert "more outcomes" in out


def test_learn_json_is_machine_readable(db, capsys):
    ext = seed()
    cli.main(["outcome", ext, "rejected"])
    capsys.readouterr()

    cli.main(["learn", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcomes"] == 1
    assert payload["counts"]["rejected"] == 1


def test_learn_advise_says_why_it_cannot_yet(db, capsys):
    ext = seed()
    cli.main(["outcome", ext, "rejected"])
    capsys.readouterr()

    assert cli.main(["learn", "--advise", ext]) == 0
    assert "No per-job prediction yet" in capsys.readouterr().out

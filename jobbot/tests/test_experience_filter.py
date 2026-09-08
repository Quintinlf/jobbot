"""`queue` and `export` dropping postings that ask for more experience than
you have. Score alone was not enough: a 5-year ask only costs ~12 points, and
Affirm's Capital Orchestration role scored 93 anyway — high enough to be
offered, letter written, applied to, despite asking for 5 when the config
ceiling is 3.
"""

from __future__ import annotations

import pytest

from jobbot import __main__ as cli
from jobbot import config, store
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


def seed(ext_id, title, years_text, company="acme"):
    with store.session() as conn:
        posting = Posting(
            external_id=ext_id,
            title=title,
            company=company,
            location="Los Angeles, CA",
            url=f"https://boards.greenhouse.io/{company}/{ext_id}",
            description=f"Software engineer role. {years_text} No degree required.",
            ats="greenhouse",
            board_slug=company,
        )
        score, verdict, site = score_posting(
            posting.title, posting.description, posting.location
        )
        store.upsert_job(conn, posting.as_row(), score, verdict, site)
    return ext_id


def test_queue_drops_high_experience_postings_by_default(db, capsys):
    seed("gh:acme:1", "Software Engineer", "5+ years of experience required.")
    seed("gh:acme:2", "Data Engineer", "2 years of experience preferred.")

    cli.main(["queue", "--min-score", "1"])
    out = capsys.readouterr().out

    assert "Data Engineer" in out
    assert "Software Engineer" not in out


def test_include_high_experience_brings_it_back(db, capsys):
    seed("gh:acme:1", "Software Engineer", "5+ years of experience required.")

    cli.main(["queue", "--min-score", "1", "--include-high-experience"])
    out = capsys.readouterr().out
    assert "Software Engineer" in out


def test_max_years_is_adjustable(db, capsys):
    seed("gh:acme:1", "Software Engineer", "4 years of experience required.")

    cli.main(["queue", "--min-score", "1"])  # default ceiling is 3
    assert "Software Engineer" not in capsys.readouterr().out

    cli.main(["queue", "--min-score", "1", "--max-years", "4"])
    assert "Software Engineer" in capsys.readouterr().out


def test_a_posting_with_no_years_mentioned_is_kept(db, capsys):
    seed("gh:acme:1", "Software Engineer", "")
    cli.main(["queue", "--min-score", "1"])
    assert "Software Engineer" in capsys.readouterr().out


def test_export_skips_high_experience_and_says_how_many(db, capsys):
    seed("gh:acme:1", "Software Engineer", "5+ years of experience required.")
    seed("gh:acme:2", "Data Engineer", "2 years of experience preferred.")

    code = cli.main(["export", "--min-score", "1", "--limit", "10"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Skipped 1 posting" in out

"""Tests for retiring postings that are no longer open.

An expired CircleCI posting sat at the top of the queue at 97 points until it
was opened by hand. `apply` then navigated to it, found no form, and reported
"resume NOT attached / cover letter not pasted" — which reads as a filling bug
rather than what it was: the job is gone.

Three defences, tested here:
  * the page says so       — looks_closed / posting_closed
  * the board dropped it   — retire_delisted
  * neither is undone by   — rescore, via the marker in the description

Run:  python -m pytest jobbot/tests/ -q
"""

from __future__ import annotations

import pytest

from jobbot import enrich, pipeline, store
from jobbot.boards import Posting
from jobbot.gating import posting_gone
from jobbot.scoring import score_posting


# ── Reading "this is closed" off the page ──────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Posting expired\nCheck back soon for new roles.",
    "This job is no longer accepting applications.",
    "We are no longer accepting applications for this role.",
    "This position has been filled.",
    "This posting is no longer available.",
    "Job not found",
    "This opportunity is no longer open.",
])
def test_closed_pages_are_recognized(text):
    assert enrich.looks_closed(text)


@pytest.mark.parametrize("text", [
    "Apply now. We review applications on a rolling basis.",
    "Applications are now open for the 2026-27 Data Science Internship program.",
    "We are accepting applications until the position is filled.",
    "Submit your application and we will be in touch.",
    "",
])
def test_live_pages_are_not_mistaken_for_closed(text):
    """A false positive retires a real job, so this direction matters more."""
    assert enrich.looks_closed(text) is None


def test_closed_page_returns_the_sentence_as_evidence():
    evidence = enrich.looks_closed("Careers\nPosting expired\nCheck back soon.")
    assert "Posting expired" in evidence


def test_soft_404_is_treated_as_gone_not_as_a_description():
    """CircleCI serves an expired posting as HTTP 200 with a page of other roles.

    Without this the page body would be stored as the job description and
    classified as though it were the posting.
    """
    class Resp:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        text = "<html><body><h1>Posting expired</h1><p>Check back soon.</p></body></html>"

        def raise_for_status(self):
            pass

    session = type("S", (), {"get": lambda self, url, **kw: Resp()})()
    with pytest.raises(enrich.PostingGone):
        enrich._page_text(session, "https://example.com/job/1")


# ── The live-browser check ─────────────────────────────────────────────────────

class _FakePage:
    """Minimal stand-in for a Playwright page."""

    def __init__(self, body: str, frames=()):
        self._body = body
        self.frames = list(frames)

    def inner_text(self, selector):
        return self._body


def test_posting_closed_reads_the_rendered_page():
    from jobbot import autofill

    assert autofill.posting_closed(_FakePage("Posting expired"))
    assert autofill.posting_closed(_FakePage("Apply for this job")) is None


def test_posting_closed_checks_embedded_frames():
    """Branded careers pages put the ATS content in an iframe."""
    from jobbot import autofill

    page = _FakePage("CircleCI Careers", frames=[_FakePage("Posting expired")])
    assert autofill.posting_closed(page)


# ── Retiring delisted jobs ─────────────────────────────────────────────────────

def _posting(external_id: str, slug: str = "circleci") -> Posting:
    return Posting(
        external_id=external_id,
        title="Associate Analytics Engineer",
        company=slug,
        location="Remote, US",
        url=f"https://example.com/{external_id}",
        description="We are hiring an analytics engineer. " * 20,
        ats="greenhouse",
        board_slug=slug,
    )


@pytest.fixture
def conn(tmp_path):
    with store.session(tmp_path / "test.db") as c:
        yield c


def test_job_missing_from_its_board_is_retired(conn):
    first = [_posting("greenhouse:circleci:1"), _posting("greenhouse:circleci:2")]
    pipeline.ingest(first, conn)

    # Second fetch of the same board no longer lists job 2.
    retired = pipeline.retire_delisted(conn, [_posting("greenhouse:circleci:1")])

    assert retired == 1
    rows = {r["external_id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert rows["greenhouse:circleci:2"]["knockout"]
    assert posting_gone(rows["greenhouse:circleci:2"]["description"])
    assert rows["greenhouse:circleci:1"]["knockout"] is None


def test_retired_job_leaves_the_queue(conn):
    pipeline.ingest([_posting("greenhouse:circleci:1")], conn)
    assert len(store.queue(conn, min_score=0)) == 1

    pipeline.retire_delisted(conn, [_posting("greenhouse:circleci:9")])
    assert store.queue(conn, min_score=0) == []


def test_other_boards_are_untouched(conn):
    pipeline.ingest(
        [_posting("greenhouse:circleci:1"), _posting("greenhouse:reddit:1", slug="reddit")],
        conn,
    )
    # Only circleci was fetched this run.
    pipeline.retire_delisted(conn, [_posting("greenhouse:circleci:1")])

    reddit = conn.execute(
        "SELECT knockout FROM jobs WHERE external_id='greenhouse:reddit:1'"
    ).fetchone()
    assert reddit["knockout"] is None


def test_a_failed_board_fetch_does_not_wipe_the_queue(conn):
    """An empty fetch is a network failure, not proof every job closed."""
    pipeline.ingest([_posting("greenhouse:circleci:1")], conn)

    assert pipeline.retire_delisted(conn, []) == 0
    row = conn.execute("SELECT knockout FROM jobs").fetchone()
    assert row["knockout"] is None


def test_applications_already_sent_are_never_rewritten(conn):
    pipeline.ingest([_posting("greenhouse:circleci:1")], conn)
    store.set_status(conn, "greenhouse:circleci:1", store.Status.APPLIED)

    assert pipeline.retire_delisted(conn, [_posting("greenhouse:circleci:9")]) == 0
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert not posting_gone(row["description"])
    assert row["applied_at"]


def test_rescore_does_not_resurrect_a_retired_job(conn):
    """knockout is recomputed from the description, so the marker must live there."""
    pipeline.ingest([_posting("greenhouse:circleci:1")], conn)
    pipeline.retire_delisted(conn, [_posting("greenhouse:circleci:9")])

    pipeline.rescore(conn)

    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["knockout"] == "posting no longer accepting applications"
    assert store.queue(conn, min_score=0) == []


def test_marking_gone_twice_does_not_duplicate_the_marker(conn):
    pipeline.ingest([_posting("greenhouse:circleci:1")], conn)
    store.mark_gone(conn, "greenhouse:circleci:1", "first")
    store.mark_gone(conn, "greenhouse:circleci:1", "second")

    row = conn.execute("SELECT description FROM jobs").fetchone()
    from jobbot.gating import GONE_MARKER

    assert row["description"].count(GONE_MARKER) == 1


def test_score_is_zeroed_so_it_cannot_top_the_queue(conn):
    """The whole complaint was a 97-point expired posting ranked first."""
    pipeline.ingest([_posting("greenhouse:circleci:1")], conn)
    pipeline.retire_delisted(conn, [_posting("greenhouse:circleci:9")])

    assert conn.execute("SELECT score FROM jobs").fetchone()["score"] == 0

    row = conn.execute("SELECT * FROM jobs").fetchone()
    score, _, _ = score_posting(title=row["title"], description=row["description"])
    assert score.rejected

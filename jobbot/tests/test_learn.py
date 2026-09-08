"""The learning layer, including what it refuses to do at small n."""

from __future__ import annotations

import pytest

from jobbot import learn, outcomes, store
from jobbot.boards import Posting
from jobbot.scoring import score_posting


@pytest.fixture
def conn(tmp_path):
    with store.session(tmp_path / "test.db") as c:
        yield c


def apply_one(conn, i, *, gate_text="No degree required.", location="Los Angeles, CA",
              title="Data Engineer", letter="", framing="projects_only"):
    ext = f"gh:acme:{i}"
    posting = Posting(
        external_id=ext,
        title=title,
        company=f"company{i % 7}",
        location=location,
        url=f"https://boards.greenhouse.io/acme/jobs/{i}",
        description=f"Python and SQL. {gate_text}",
        ats="greenhouse",
        board_slug="acme",
    )
    score, verdict, site = score_posting(posting.title, posting.description, posting.location)
    store.upsert_job(conn, posting.as_row(), score, verdict, site)
    if letter:
        store.save_materials(conn, ext, cover_letter=letter, variants={framing: letter})
    store.set_status(conn, ext, store.Status.APPLIED)
    return ext


def test_wilson_widens_when_there_is_nothing_to_go_on():
    low, high = learn.wilson(0, 3)
    assert low == 0.0
    assert high > 0.4          # 0/3 is not evidence that it never works
    low, high = learn.wilson(0, 200)
    assert high < 0.05


def test_no_model_below_the_outcome_floor(conn):
    for i in range(5):
        ext = apply_one(conn, i)
        outcomes.record(conn, ext, outcomes.REJECTED)

    report = learn.analyze(conn)
    assert report.model is None
    assert "more outcomes" in report.blocked
    assert str(learn.MIN_OUTCOMES) in report.blocked


def test_no_model_without_positive_examples(conn):
    """All-rejection data can only teach the model to say no. That is not learning."""
    for i in range(learn.MIN_OUTCOMES + 3):
        ext = apply_one(conn, i)
        outcomes.record(conn, ext, outcomes.REJECTED)

    report = learn.analyze(conn)
    assert report.model is None
    assert "advanced" in report.blocked


def test_stated_reasons_are_reported_from_a_single_rejection(conn):
    ext = apply_one(conn, 1)
    outcomes.record(
        conn, ext, outcomes.REJECTED,
        detail="Unfortunately this position requires a bachelor's degree.",
    )
    report = learn.analyze(conn)

    assert report.reasons["degree"] == 1
    assert any("degree" in line for line in report.advice)


def test_boilerplate_rejections_produce_no_advice(conn):
    """"Other candidates more closely aligned" says nothing and must not pretend to."""
    for i in range(4):
        ext = apply_one(conn, i)
        outcomes.record(
            conn, ext, outcomes.REJECTED,
            detail="We are moving forward with other candidates whose experience "
                   "more closely aligns with the role.",
        )
    report = learn.analyze(conn)

    assert report.reasons.get("stronger_candidates") == 4
    assert report.advice == []


def test_ghosted_applications_count_as_negatives(conn):
    for i in range(3):
        ext = apply_one(conn, i)
        outcomes.record(conn, ext, outcomes.GHOSTED, source="derived")

    assert len(learn.analyze(conn).splits or []) == 0  # under the descriptive floor
    report = learn.analyze(conn, include_ghosted=False)
    assert report.n_outcomes == 0


def test_the_model_fits_once_the_data_earns_it(conn):
    """A planted signal has to come back out, or the pipeline is decorative.

    The signal is the cover-letter framing, deliberately something `scoring`
    cannot see. Planting it in the location instead would prove nothing: the
    location already moves `score`, so the model could recover the label
    without ever looking at the feature under test.
    """
    for i in range(40):
        narrative = i % 2 == 0
        ext = apply_one(
            conn, i,
            location="Los Angeles, CA",
            letter="word " * 300,
            framing="narrative" if narrative else "projects_only",
        )
        outcomes.record(
            conn, ext,
            outcomes.INTERVIEW if narrative else outcomes.REJECTED,
        )

    report = learn.analyze(conn)
    assert report.model is not None
    assert report.model["n"] == 40
    assert report.model["positives"] == 20
    assert report.model["cv_auc"] is not None and report.model["cv_auc"] > 0.9

    top = report.model["coefficients"][0]["name"]
    assert top.startswith("letter_framing=")


def test_advice_only_names_things_you_can_change(conn):
    for i in range(40):
        ext = apply_one(conn, i, location="Remote - US" if i % 2 == 0 else "Austin, TX")
        outcomes.record(
            conn, ext, outcomes.INTERVIEW if i % 2 == 0 else outcomes.REJECTED
        )
    report = learn.analyze(conn)

    for line in report.advice:
        if line.startswith("model: "):
            root = line.split("model: ")[1].split("=")[0].split(" ")[0].split("__")[0]
            assert root in learn.ACTIONABLE


def test_advise_refuses_before_there_is_a_model(conn):
    ext = apply_one(conn, 1)
    outcomes.record(conn, ext, outcomes.REJECTED)
    result = learn.advise(conn, ext)

    assert "blocked" in result
    assert "probability" not in result


def test_advise_scores_a_pending_job_once_a_model_exists(conn):
    for i in range(40):
        ext = apply_one(conn, i, location="Remote - US" if i % 2 == 0 else "Austin, TX")
        outcomes.record(
            conn, ext, outcomes.INTERVIEW if i % 2 == 0 else outcomes.REJECTED
        )

    pending = apply_one(conn, 999, location="Remote - US")
    conn.execute("UPDATE jobs SET status='new', applied_at=NULL WHERE external_id=?",
                 (pending,))
    conn.execute("DELETE FROM applications WHERE external_id=?", (pending,))

    result = learn.advise(conn, pending)
    assert 0.0 <= result["probability"] <= 1.0
    assert result["probability"] > result["base_rate"]


def test_report_serialises_without_the_estimator(conn):
    for i in range(40):
        ext = apply_one(conn, i, location="Remote - US" if i % 2 == 0 else "Austin, TX")
        outcomes.record(
            conn, ext, outcomes.INTERVIEW if i % 2 == 0 else outcomes.REJECTED
        )
    import json

    payload = json.loads(learn.to_json(learn.analyze(conn)))
    assert payload["model"]["n"] == 40
    assert "estimator" not in payload["model"]

"""Apply-queue selection and human-confirmation bookkeeping.

Browser multi-tab behaviour is not exercised here — Playwright against live
ATS pages is environment-specific. This pins the mapping: which jobs are
prepared, and that Applied is recorded only on an explicit `y`.
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


def add(conn, ext_id, title="Data Engineer", letter="A letter.", company="acme",
        location="Remote - US"):
    posting = Posting(
        external_id=ext_id,
        title=title,
        company=company,
        location=location,
        url=f"https://example.com/{ext_id}",
        description="Python and SQL. No degree required. Fully remote.",
        ats="greenhouse",
        board_slug=company,
    )
    s, v, site = score_posting(posting.title, posting.description, posting.location)
    store.upsert_job(conn, posting.as_row(), s, v, site)
    if letter:
        store.save_materials(conn, ext_id, variants={"projects_only": letter})
    return ext_id


def test_ready_rows_require_a_letter(conn):
    add(conn, "with:1")
    add(conn, "without:1", letter="")
    ids = [r["external_id"] for r in apply_session.ready_rows(conn, count=10)]
    assert ids == ["with:1"]


def test_prepare_batch_selects_several(conn):
    for i in range(6):
        # Distinct titles, with the number after a comma: a bare trailing
        # numeral now reads as a seniority rung and is knocked out.
        add(conn, f"a:{i}", title=f"Data Engineer, Team {i}", company=f"company{i}")
    rows = apply_session.ready_rows(conn, count=5)
    assert len(rows) == 5


def test_the_same_role_is_only_offered_once(conn):
    """Twilio listed two "Software Engineer (L2)", Remote - US, as separate reqs.

    Storage is right to keep them apart — either could be the live one — but
    preparing both spends two of ten tabs on one application. Checked with
    one_per_company off so this pins role_key deduping specifically, apart
    from the company rule below.

    The titles here drop the "(L2)" the real reqs carried, because an L2 is a
    mid-level rung and scoring knocks those out now. The duplicate-req problem
    this pins is about the title being identical, not about the level.
    """
    add(conn, "gh:twilio:1", title="Software Engineer", company="twilio")
    add(conn, "gh:twilio:2", title="Software Engineer", company="twilio")
    add(conn, "gh:twilio:3", title="Software Engineer, Platform", company="twilio")

    rows = apply_session.ready_rows(conn, count=10, one_per_company=False)
    titles = [r["title"] for r in rows]

    assert len(rows) == 2
    assert titles.count("Software Engineer") == 1
    assert "Software Engineer, Platform" in titles


def test_the_same_title_in_another_city_is_a_different_job(conn):
    """role_key still tells two postings apart by location, apart from company."""
    add(conn, "gh:acme:1", title="Data Engineer", company="acme")
    posting = Posting(
        external_id="gh:acme:2",
        title="Data Engineer",
        company="acme",
        location="Los Angeles, CA",
        url="https://example.com/gh:acme:2",
        description="Python and SQL. No degree required.",
        ats="greenhouse",
        board_slug="acme",
    )
    s_, v_, site_ = score_posting(posting.title, posting.description, posting.location)
    store.upsert_job(conn, posting.as_row(), s_, v_, site_)
    store.save_materials(conn, "gh:acme:2", variants={"projects_only": "A letter."})

    rows = apply_session.ready_rows(conn, count=10, one_per_company=False)
    assert len(rows) == 2


def test_one_job_per_company_by_default(conn):
    """Four Affirm roles in one sitting reads as spam pressure, not four honest tries."""
    add(conn, "gh:affirm:1", title="Software Engineer", company="affirm")
    add(conn, "gh:affirm:2", title="Machine Learning Engineer", company="affirm")
    add(conn, "gh:affirm:3", title="Backend Engineer", company="affirm")
    add(conn, "gh:pinterest:1", title="Data Scientist", company="pinterest")

    rows = apply_session.ready_rows(conn, count=10)
    companies = [r["company"] for r in rows]

    assert len(rows) == 2
    assert companies.count("affirm") == 1
    assert "pinterest" in companies


def test_company_names_are_normalised_for_the_one_per_batch_rule(conn):
    """"Acme" and "Acme, Inc." must count as the same company, not two."""
    add(conn, "gh:acme:1", title="Data Engineer", company="Acme")
    add(conn, "gh:acme2:1", title="Data Analyst", company="Acme, Inc.")

    rows = apply_session.ready_rows(conn, count=10)
    assert len(rows) == 1


def test_one_per_company_can_be_turned_off_for_a_single_job_lookup(conn):
    """job_id lookups (used by `apply --job`) bypass batch rules entirely."""
    add(conn, "gh:affirm:1", title="Software Engineer", company="affirm")
    rows = apply_session.ready_rows(conn, job_id="gh:affirm:1")
    assert [r["external_id"] for r in rows] == ["gh:affirm:1"]


def test_failed_fill_is_not_marked_applied(conn):
    add(conn, "a:1")
    action = apply_session.record_decision(conn, "a:1", "f")
    assert action == "failed"
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["applied_at"] is None
    assert row["status"] == store.Status.NEW.value
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events WHERE external_id='a:1'")]
    assert "prepare_failed" in kinds


def test_confirm_y_records_applied(conn):
    add(conn, "a:1")
    assert apply_session.record_decision(conn, "a:1", "y") == "applied"
    row = conn.execute("SELECT * FROM jobs WHERE external_id='a:1'").fetchone()
    assert row["status"] == store.Status.APPLIED.value
    assert row["applied_at"] is not None


def test_closing_one_does_not_apply_the_others(conn):
    # Different companies on purpose. Confirming one posting now hides the
    # rest of that company's postings — see
    # test_reachable_batch.test_a_company_already_applied_to_is_not_offered_again
    # — and this test is about status not cascading, which is a separate rule.
    add(conn, "a:1", company="acme")
    add(conn, "a:2", title="ML Engineer", company="beta")
    apply_session.record_decision(conn, "a:1", "y")

    still_new = conn.execute(
        "SELECT status FROM jobs WHERE external_id = 'a:2'"
    ).fetchone()
    assert still_new["status"] == store.Status.NEW.value

    remaining = apply_session.ready_rows(conn, count=10)
    assert [r["external_id"] for r in remaining] == ["a:2"]


def test_a_letter_is_not_pushed_out_by_higher_scoring_jobs(conn):
    """A prepared letter must not fall out of the queue because the board grew.

    ready_rows used to take the top N by score and keep the lettered ones. With
    thousands of postings and a handful of letters, a scrape that added better
    scoring jobs silently dropped work already done.
    """
    add(conn, "lettered:1", title="Data Analyst")

    for i in range(60):
        posting = Posting(
            external_id=f"loud:{i}",
            title="Machine Learning Engineer",
            company=f"company{i}",
            location="Remote - US",
            url=f"https://example.com/loud/{i}",
            description="Python. No degree required. Fully remote. Entry level.",
            ats="greenhouse",
            board_slug=f"company{i}",
        )
        s_, v_, site_ = score_posting(
            posting.title, posting.description, posting.location
        )
        store.upsert_job(conn, posting.as_row(), s_, v_, site_)

    top = store.queue(conn, limit=5, min_score=1)
    assert all(r["external_id"] != "lettered:1" for r in top), "fixture must outscore it"

    ids = [r["external_id"] for r in apply_session.ready_rows(conn, count=5)]
    assert ids == ["lettered:1"]

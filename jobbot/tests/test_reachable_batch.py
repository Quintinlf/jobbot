"""What separates a posting that could hire him from one that merely scores well.

Two failures found on 2026-09-06, both in the same place — the queue was ranked
almost entirely on signals that are blind to what the job is:

  * Stripe's "Investigator" (fraud/risk) scored 87 and sat at the top of the
    queue, above every Software Engineer posting, on the strength of one
    mention of data scientists in its description. USC's wet-lab "Research Lab
    Technician", Fivetran's "People Business Partner" and a "PhD GenAI Research
    Scientist Intern" came through the same door.
  * `queue` dropped postings asking for more years than the config ceiling, but
    `prepare-applications` did not, so the apply path could hand over a posting
    the browsing command had already ruled out.
"""

from __future__ import annotations

import pytest

from jobbot import apply_session, config, store
from jobbot.boards import Posting
from jobbot.scoring import score_posting


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return path


def seed(ext_id, title, description, company="acme", letter="Dear team,"):
    with store.session() as conn:
        posting = Posting(
            external_id=ext_id,
            title=title,
            company=company,
            location="Remote - United States",
            url=f"https://boards.greenhouse.io/{company}/{ext_id}",
            description=description,
            ats="greenhouse",
            board_slug=company,
        )
        score, verdict, site = score_posting(
            posting.title, posting.description, posting.location
        )
        store.upsert_job(conn, posting.as_row(), score, verdict, site)
        if letter:
            conn.execute(
                "UPDATE jobs SET cover_letter = ? WHERE external_id = ?",
                (letter, ext_id),
            )
    return ext_id


# ── The title has to name the job ──────────────────────────────────────────────

ENGINEERING_DESC = (
    "Build and ship services in Python. You will work with data scientists "
    "and engineers across the company. No degree required."
)


def test_off_title_match_is_penalised():
    """The Stripe Investigator case: role named only in the description."""
    on_title, _, _ = score_posting(
        "Software Engineer", ENGINEERING_DESC, "Remote - United States"
    )
    off_title, _, _ = score_posting(
        "Investigator", ENGINEERING_DESC, "Remote - United States"
    )
    assert off_title.total < on_title.total
    assert any("title names no target role" in r for r in off_title.reasons)


def test_off_title_posting_is_not_dropped():
    """An odd title is a discount, not a knockout. Genuinely oddly-named
    engineering roles exist, so these stay in the queue, ranked below the ones
    that say what they are."""
    score, _, _ = score_posting(
        "Investigator", ENGINEERING_DESC, "Remote - United States"
    )
    assert not score.rejected
    assert score.total > 0


def test_an_engineering_title_outranks_an_hr_one_on_the_same_text(db):
    """Fivetran's 'People Business Partner' outscored real engineering roles."""
    eng, _, _ = score_posting(
        "Backend Engineer", ENGINEERING_DESC, "Remote - United States"
    )
    hr, _, _ = score_posting(
        "People Business Partner", ENGINEERING_DESC, "Remote - United States"
    )
    assert eng.total > hr.total


# ── The apply path honours the same experience ceiling as the queue ────────────

def test_ready_rows_drops_postings_over_the_years_ceiling(db):
    seed("gh:acme:1", "Software Engineer",
         "Python work. 8+ years of experience required. No degree required.")
    seed("gh:acme:2", "Software Engineer",
         "Python work. 2 years of experience preferred. No degree required.",
         company="beta")

    with store.session() as conn:
        picked = apply_session.ready_rows(conn, count=10)

    assert [r["external_id"] for r in picked] == ["gh:acme:2"]


def test_the_ceiling_can_be_lifted(db):
    seed("gh:acme:1", "Software Engineer",
         "Python work. 8+ years of experience required. No degree required.")

    with store.session() as conn:
        picked = apply_session.ready_rows(conn, count=10, max_years=None)

    assert [r["external_id"] for r in picked] == ["gh:acme:1"]


# ── Volume is not capped by how many letters exist ─────────────────────────────

def test_ready_rows_can_include_postings_without_a_letter(db):
    """`hunt` tops a batch up with unlettered postings. Measured 2026-09-06:
    11 of 308 reachable unopened postings had a letter, so requiring one caps a
    sitting at 11 no matter how many jobs are open."""
    seed("gh:acme:1", "Software Engineer",
         "Python work. No degree required.", letter="Dear team,")
    seed("gh:beta:2", "Backend Engineer",
         "Python work. No degree required.", company="beta", letter="")

    with store.session() as conn:
        lettered = apply_session.ready_rows(conn, count=10, require_letter=True)
        both = apply_session.ready_rows(conn, count=10, require_letter=False)

    assert [r["external_id"] for r in lettered] == ["gh:acme:1"]
    assert {r["external_id"] for r in both} == {"gh:acme:1", "gh:beta:2"}


# -- Not the same company twice -------------------------------------------------

def test_a_company_already_applied_to_is_not_offered_again(db):
    """Measured 2026-09-07: 14 of the 25 postings a batch offered were at
    companies with an application already out — Chime, GitLab, Hightouch,
    Twilio, OpenAI, Affirm and eight more. `one_per_company` could not see
    them: it only dedupes within the batch it is building."""
    seed("gh:chime:1", "Software Engineer",
         "Python work. No degree required.", company="chime")
    seed("gh:chime:2", "Backend Engineer",
         "Python work. No degree required.", company="chime")
    seed("gh:other:3", "Software Engineer",
         "Python work. No degree required.", company="other")

    with store.session() as conn:
        store.set_status(conn, "gh:chime:1", store.Status.APPLIED)
        picked = apply_session.ready_rows(conn, count=10)

    assert [r["external_id"] for r in picked] == ["gh:other:3"]


def test_the_filter_can_be_lifted(db):
    seed("gh:chime:1", "Software Engineer",
         "Python work. No degree required.", company="chime")
    seed("gh:chime:2", "Backend Engineer",
         "Python work. No degree required.", company="chime")

    with store.session() as conn:
        store.set_status(conn, "gh:chime:1", store.Status.APPLIED)
        picked = apply_session.ready_rows(conn, count=10, include_applied=True)

    assert [r["external_id"] for r in picked] == ["gh:chime:2"]


def test_company_names_are_matched_loosely(db):
    """The applications table and the jobs table do not always spell a company
    the same way, so the comparison goes through normalize_company."""
    seed("gh:scaleai:1", "Software Engineer",
         "Python work. No degree required.", company="Scale AI")
    seed("gh:scaleai:2", "Backend Engineer",
         "Python work. No degree required.", company="scaleai")

    with store.session() as conn:
        store.set_status(conn, "gh:scaleai:1", store.Status.APPLIED)
        picked = apply_session.ready_rows(conn, count=10)

    assert picked == []


# -- A country in the title beats a vague location field ------------------------

def test_a_country_in_the_title_overrides_a_bare_remote():
    """Measured 2026-09-07: "Backend Software Engineer - India" and "Analytics
    Engineering Advocate - Europe" both carried location "Remote", collected
    the full remote bonus, and came out first and sixth in a batch of 25 to
    apply to. score_location only ever saw the word "Remote"."""
    here, _, _ = score_posting(
        "Backend Software Engineer", ENGINEERING_DESC, "Remote"
    )
    there, _, _ = score_posting(
        "Backend Software Engineer - India", ENGINEERING_DESC, "Remote"
    )
    assert there.total < here.total
    assert any("title names a non-US location" in r for r in there.reasons)


def test_a_region_counts_as_non_us():
    """Lightdash's posting named no country at all — just "Europe"."""
    scored, _, _ = score_posting(
        "Analytics Engineering Advocate - Europe", ENGINEERING_DESC, "Remote"
    )
    assert any("title names a non-US location" in r for r in scored.reasons)


def test_a_real_us_location_wins_over_the_title():
    """A Los Angeles role on a team called "India Team" is a Los Angeles role.
    An earlier version of this check moved it from 89 points to 31."""
    scored, _, _ = score_posting(
        "Software Engineer, India Team", ENGINEERING_DESC, "Los Angeles, CA"
    )
    assert not any("non-US" in r for r in scored.reasons)
    assert any("los angeles" in r.lower() for r in scored.reasons)


# -- Numbered levels read as seniority -----------------------------------------

def test_a_numbered_level_is_knocked_out():
    """TITLE_KNOCKOUTS caught senior/staff/principal but not the numerals that
    say the same thing. "Software Engineer II", "Delivery Engineer III" and
    "Data Scientist II" were all being offered as entry-reachable."""
    for title in ("Software Engineer II", "Delivery Engineer III",
                  "Data Scientist II", "Software Engineer (L2)"):
        scored, _, _ = score_posting(title, ENGINEERING_DESC, "Remote - US")
        assert scored.rejected, f"{title} should be knocked out"


def test_the_entry_rung_survives():
    for title in ("Software Engineer I", "Engineer 1", "Software Engineer"):
        scored, _, _ = score_posting(title, ENGINEERING_DESC, "Remote - US")
        assert not scored.rejected, f"{title} should be kept"


def test_a_numeral_in_the_subject_is_not_a_level():
    """"3D Reconstruction Engineer" and "Backend Engineer (Web3)" are not
    rungs on a ladder."""
    for title in ("3D Reconstruction Engineer", "Backend Engineer (Web3)",
                  "Software Engineer (AI)"):
        scored, _, _ = score_posting(title, ENGINEERING_DESC, "Remote - US")
        assert not scored.rejected, f"{title} should be kept"


# -- A trade is a trade even when the title says "Engineer" --------------------

REFRIGERATION_DESC = (
    "Under the direction of the Senior Superintendent, install, maintain and "
    "repair commercial and domestic refrigeration systems."
)


def test_a_trade_described_under_a_vague_engineer_title_is_dropped():
    """UCLA's "Service Engineer" is a commercial refrigeration technician. The
    title matched nothing but the bare "engineer" (+4), the word
    "refrigeration" never appears in it, and an LA location (+52) carried it to
    fourth place in a batch of jobs to apply to."""
    scored, _, _ = score_posting(
        "Service Engineer", REFRIGERATION_DESC, "Los Angeles, CA"
    )
    assert scored.rejected
    assert "skilled trade" in scored.knockout


def test_a_software_role_at_an_hvac_company_survives():
    """The guard only fires when the title said nothing specific. A building
    automation startup hiring a Software Engineer says HVAC too."""
    scored, _, _ = score_posting(
        "Software Engineer",
        "Build the control plane for HVAC systems in commercial buildings. "
        "Python and Go.",
        "San Francisco",
    )
    assert not scored.rejected


def test_sourcing_is_recruiting_under_another_name():
    scored, _, _ = score_posting(
        "Talent Sourcer, Engineering", ENGINEERING_DESC, "Remote - United States"
    )
    assert scored.rejected


# -- A letter buys a place in the batch, not the top of it ---------------------

def test_hunt_orders_the_batch_by_score(db, monkeypatch):
    """A USC "Project Assistant" scoring 16 led a sitting of 25 because it was
    the one job with a letter written, above a 96-point ML role. You work down
    the tabs in order, so the order is the recommendation."""
    import inspect

    from jobbot import __main__ as cli

    source = inspect.getsource(cli.cmd_hunt)
    assert 'lettered.sort(key=lambda r: -(r["score"] or 0))' in source


# -- The description can say which country this is -----------------------------

CANADIAN_DESC = (
    "Bree is a consumer finance platform building faster, simpler and more "
    "affordable financial services for Canadians who live paycheck to "
    "paycheck. 800,000+ Canadians have signed up for Bree. We are looking for "
    "a Machine Learning Engineer to build and scale ML systems in Canada."
)


def test_a_country_in_the_description_overrides_a_bare_remote():
    """Bree's "Machine Learning Engineer, Underwriting" listed its location as
    "Remote", scored 96 — the highest in the queue — and is Canadian. He is not
    entitled to work in Canada."""
    scored, _, _ = score_posting(
        "Machine Learning Engineer, Underwriting", CANADIAN_DESC, "Remote"
    )
    assert any("Canada" in r for r in scored.reasons)


def test_a_us_company_selling_into_canada_is_not_canadian():
    """Both halves are required: a country named repeatedly AND the US named
    nowhere. Otherwise every US fintech with Canadian customers disappears."""
    desc = (
        "We are a San Francisco company serving customers in Canada and the "
        "United States. Canadians make up a third of our users. Python, SQL."
    )
    scored, _, _ = score_posting("Software Engineer", desc, "Remote")
    assert not any("not the US" in r for r in scored.reasons)


def test_us_cities_count_as_the_us():
    """"San Francisco" and "New York" were reading as naming nowhere in the
    US, which would have flagged San Francisco roles as foreign."""
    for place in ("San Francisco", "New York", "Menlo Park", "Boston"):
        assert config.names_us_location(place), place
    assert not config.names_us_location("Toronto")
    assert not config.names_us_location("Remote")


# -- The posted band says which rung the requisition is for --------------------

def test_a_senior_band_is_penalised():
    """Blaxel posts a Site Reliability Engineer at $175K-$250K and a Software
    Engineer at $140K-$190K. To a scorer reading only words those are the same
    seniority, and they are not."""
    senior, _, _ = score_posting(
        "Site Reliability Engineer", ENGINEERING_DESC, "San Francisco",
        salary_min=175_000,
    )
    entry, _, _ = score_posting(
        "Site Reliability Engineer", ENGINEERING_DESC, "San Francisco",
        salary_min=140_000,
    )
    assert senior.total < entry.total
    assert any("written for a senior" in r for r in senior.reasons)


def test_no_published_band_costs_nothing():
    """Most boards publish nothing. Silence must not be read as a signal."""
    quiet, _, _ = score_posting(
        "Software Engineer", ENGINEERING_DESC, "San Francisco", salary_min=None
    )
    middling, _, _ = score_posting(
        "Software Engineer", ENGINEERING_DESC, "San Francisco", salary_min=160_000
    )
    assert quiet.total == middling.total
    assert not any("band" in r for r in quiet.reasons)


def test_ashby_salary_is_read_off_the_posting():
    """`includeCompensation=true` was already on the request URL and the answer
    was thrown away."""
    from jobbot.boards import _ashby_salary

    job = {
        "compensation": {
            "scrapeableCompensationSalarySummary": "$175K - $250K",
            "summaryComponents": [
                {"compensationType": "EquityPercentage", "interval": "NONE",
                 "minValue": 0.05, "maxValue": 0.5},
                {"compensationType": "Salary", "interval": "1 YEAR",
                 "currencyCode": "USD", "minValue": 175000, "maxValue": 250000},
            ],
        }
    }
    assert _ashby_salary(job) == (175000, 250000, "$175K - $250K")


def test_equity_alone_is_not_a_salary():
    """A percentage is not a number that can be compared against a floor."""
    from jobbot.boards import _ashby_salary

    job = {"compensation": {"summaryComponents": [
        {"compensationType": "EquityPercentage", "interval": "NONE",
         "minValue": 0.05, "maxValue": 0.5},
    ]}}
    assert _ashby_salary(job) == (None, None, "")

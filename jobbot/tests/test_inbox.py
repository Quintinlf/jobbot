"""Email classification and parsing. No live Gmail, no credentials."""

from __future__ import annotations

import pytest

from jobbot import inbox, store


@pytest.fixture
def conn(tmp_path):
    with store.session(tmp_path / "test.db") as c:
        yield c


def mail(**kwargs) -> inbox.MailItem:
    defaults = dict(
        message_id="mid-1",
        sender="alerts@example.com",
        subject="Hello",
        text="",
        html="",
        received="2026-08-18T15:24:00+00:00",
        urls=[],
    )
    defaults.update(kwargs)
    if not defaults["urls"]:
        defaults["urls"] = inbox.extract_urls(defaults["html"], defaults["text"])
    return inbox.MailItem(**defaults)


WESTBURY = """\
Hello Quintin,

I'm part of the senior HR management team at Westbury & Co.
At Westbury & Co., our role goes beyond traditional career consulting.

Here's how we support your job search:
* Taking care of your job applications to suitable companies, so you don't have to.
* And yes, we guarantee a suitable career opportunity through our program.

Would you be interested in learning more?
Simply reply to this email with "Learn more," and I'll share further details
and help schedule a Google Meet consultation with one of our managers.
"""


JOBOT = """\
Role: Data Engineer
Company: Acme
Location: Los Angeles, CA

Python and SQL. No degree required. Fully remote.

Apply: https://job-boards.greenhouse.io/acme/jobs/4242
"""


LINKEDIN = """\
New job: Machine Learning Engineer
Company: Northwind
Location: Remote - US

Python. No degree required.
https://www.linkedin.com/comm/jobs/view/9988776655/?tracking=1
"""


INDEED = """\
Job title: Analytics Engineer
Company: Southwind
Location: Santa Monica, CA

SQL and dbt. No degree required.
https://www.indeed.com/viewjob?jk=abc123def&utm_source=alert
"""


def test_westbury_is_agency_pitch_not_a_job():
    item = mail(
        sender="Alex Rivera <alex@example-recruiting.test>",
        subject="Your job search",
        text=WESTBURY,
    )
    classified = inbox.classify(item)
    assert classified.kind == "agency_pitch"


def test_agency_pitch_does_not_enter_the_queue(conn):
    item = mail(
        sender="Alex Rivera <alex@example-recruiting.test>",
        subject="Your job search",
        text=WESTBURY,
        urls=["https://westbury.example.com/learn-more"],
    )
    result = inbox.ingest_items([item], conn=conn, unwrap=lambda u: u, enrich_descriptions=False)
    assert result.skipped_nonjobs == 1
    assert result.job_alerts == 0
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0


def test_jobot_email_is_parsed_and_ingested(conn):
    item = mail(
        sender="Jobot Alerts <alerts@jobot.com>",
        subject="Jobot: Data Engineer at Acme",
        text=JOBOT,
        html='<a href="https://job-boards.greenhouse.io/acme/jobs/4242">Apply</a>',
        message_id="jobot-1",
    )
    result = inbox.ingest_items([item], conn=conn, unwrap=lambda u: u, enrich_descriptions=False)
    assert result.job_alerts == 1
    assert result.ingested_new == 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["title"] == "Data Engineer"
    assert row["company"] == "Acme"
    assert "greenhouse.io" in row["url"]
    assert row["ats"] == "email"
    assert row["board_slug"] == "jobot"
    assert row["cover_letter"] in (None, "")


def test_linkedin_alert_normalizes_view_url(conn):
    item = mail(
        sender="LinkedIn Job Alerts <jobalerts@linkedin.com>",
        subject="Machine Learning Engineer at Northwind",
        text=LINKEDIN,
        message_id="li-1",
    )
    draft = inbox.parse_job(item, "linkedin", unwrap=inbox.unwrap_url)
    assert draft is not None
    assert draft.title == "Machine Learning Engineer"
    assert "linkedin.com/jobs/view/9988776655" in draft.url
    result = inbox.ingest_items(
        [item], conn=conn, unwrap=inbox.unwrap_url, enrich_descriptions=False
    )
    assert result.ingested_new == 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["board_slug"] == "linkedin"
    assert row["title"] == "Machine Learning Engineer"


def test_indeed_alert_keeps_viewjob_url():
    item = mail(
        sender="Indeed <alert@indeed.com>",
        subject="Analytics Engineer - Southwind",
        text=INDEED,
        message_id="ind-1",
    )
    classified = inbox.classify(item)
    assert classified.kind == "job_alert"
    assert classified.source == "indeed"
    draft = inbox.parse_job(item, "indeed", unwrap=lambda u: u)
    assert draft is not None
    assert draft.title == "Analytics Engineer"
    assert "jk=abc123def" in draft.url


def test_email_jobs_go_through_gate_and_score(conn):
    item = mail(
        sender="Jobot Alerts <alerts@jobot.com>",
        subject="Jobot: Data Engineer at Acme",
        text=JOBOT,
        message_id="jobot-gate",
    )
    inbox.ingest_items([item], conn=conn, unwrap=lambda u: u, enrich_descriptions=False)
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["score"] > 0
    assert row["gate"] in {"open", "equivalent_ok", "soft_degree"}
    assert row["knockout"] is None


def test_unwrap_prefers_query_dest_over_tracker():
    wrapped = "https://e.jobot.com/click?u=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F9"
    assert "greenhouse.io" in inbox.unwrap_url(wrapped)


# ── Wellfound ──────────────────────────────────────────────────────────────────
# One alert email holds several listings, unlike every other source here, which
# is why it gets its own block of tests rather than reusing the single-draft
# assertions above.

WELLFOUND_DIGEST = """\
<https://angel.co>

Hi Quintin! I've found 4 new jobs that might interest you!

 Ready to Interview  Open to offers  Closed to Offers

Principal Data Engineer

EnergyHub / 51-200 Employees

 $180-220k | Remote only, United States | 10 years of exp | Full-time


Actively Hiring B2B Growth Stage


Learn More
<https://wellfound.com/jobs?job_listing_slug=4562812-principal-data-engineer>



Data Engineer

ACCO Engineered Systems / 501-1000 Employees

 $106-143k | In office, Pasadena | years of exp | Full-time


Actively Hiring


Our Take

A commercial HVAC company is hiring a Data Engineer to build pipelines
supporting internal reporting. Southern California residency required.


Learn More <https://wellfound.com/jobs?job_listing_slug=4552685-data-engineer>



Data Engineer

The Campbell's Foundation / 5000+ Employees

 $110-152k | Remote only, Camden | 4 years of exp | Full-time


Actively Hiring


Learn More <https://wellfound.com/jobs?job_listing_slug=4566613-data-engineer>



Lead Data Engineer

Trimble Navigation / 5000+ Employees

 $136-187k | Remote only, Texas | 1 years of exp | Full-time


Actively Hiring


Learn More
<https://wellfound.com/jobs?job_listing_slug=4556425-lead-data-engineer>



 Not finding what you had in mind? Try updating your preferences
<https://wellfound.com/profile/edit/preferences>
"""


def test_wellfound_sender_is_classified_as_a_job_alert():
    item = mail(sender='"Wellfound" <team@hi.wellfound.com>',
                subject="New jobs: Principal Data Engineer at EnergyHub and 3 more jobs",
                text=WELLFOUND_DIGEST)
    classified = inbox.classify(item)
    assert classified.kind == "job_alert"
    assert classified.source == "wellfound"


def test_one_digest_email_yields_every_listing():
    item = mail(text=WELLFOUND_DIGEST)
    drafts = inbox.parse_wellfound_digest(item)
    assert [d.title for d in drafts] == [
        "Principal Data Engineer", "Data Engineer", "Data Engineer", "Lead Data Engineer",
    ]
    assert [d.company for d in drafts] == [
        "EnergyHub", "ACCO Engineered Systems", "The Campbell's Foundation",
        "Trimble Navigation",
    ]


def test_each_listing_gets_its_own_url_not_a_neighbours(conn=None):
    """The regression case: Campbell's listing must not come back with
    Trimble's slug just because Trimble's own "Learn More" happens to be the
    next one the pattern can match."""
    drafts = {d.company: d.url for d in inbox.parse_wellfound_digest(mail(text=WELLFOUND_DIGEST))}
    assert drafts["The Campbell's Foundation"].endswith("4566613-data-engineer")
    assert drafts["Trimble Navigation"].endswith("4556425-lead-data-engineer")


def test_an_our_take_paragraph_does_not_break_the_next_listing():
    """ACCO's listing carries an extra paragraph most others don't; both the
    listing before and after it still have to come out intact."""
    drafts = {d.company: d for d in inbox.parse_wellfound_digest(mail(text=WELLFOUND_DIGEST))}
    assert drafts["ACCO Engineered Systems"].location == "In office, Pasadena"
    assert drafts["EnergyHub"].url.endswith("4562812-principal-data-engineer")
    assert drafts["The Campbell's Foundation"].location == "Remote only, Camden"


def test_a_same_line_learn_more_url_is_still_found():
    """ACCO and Campbell's format the URL on the same line as "Learn More";
    EnergyHub and Trimble put it on the next line. Both must parse."""
    drafts = {d.company: d.url for d in inbox.parse_wellfound_digest(mail(text=WELLFOUND_DIGEST))}
    assert drafts["ACCO Engineered Systems"]        # same-line style
    assert drafts["EnergyHub"]                       # next-line style


def test_wellfound_listings_reach_the_queue_deduplicated(conn):
    item = mail(sender='"Wellfound" <team@hi.wellfound.com>', text=WELLFOUND_DIGEST)
    result = inbox.ingest_items([item], conn=conn, enrich_descriptions=False)

    assert result.job_alerts == 4
    assert result.ingested_new == 4
    rows = conn.execute("SELECT company, board_slug, ats FROM jobs").fetchall()
    assert {r["company"] for r in rows} == {
        "EnergyHub", "ACCO Engineered Systems", "The Campbell's Foundation",
        "Trimble Navigation",
    }
    assert all(r["board_slug"] == "wellfound" for r in rows)
    assert all(r["ats"] == "email" for r in rows)

    # Re-ingesting the identical email must not duplicate the listings.
    second = inbox.ingest_items([item], conn=conn, enrich_descriptions=False)
    assert second.ingested_new == 0
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 4


def test_a_digest_with_no_recognisable_listing_counts_as_a_parse_failure(conn):
    item = mail(sender='"Wellfound" <team@hi.wellfound.com>',
                text="Hi Quintin! No new jobs to show you this week.")
    result = inbox.ingest_items([item], conn=conn, enrich_descriptions=False)
    assert result.job_alerts == 0
    assert result.parse_failed == 1

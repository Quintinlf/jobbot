"""Reply detection. No live Gmail, no credentials, nothing sent."""

from __future__ import annotations

import pytest

from jobbot import inbox, outcomes, replies, store
from jobbot.boards import Posting
from jobbot.scoring import score_posting


@pytest.fixture
def conn(tmp_path):
    with store.session(tmp_path / "test.db") as c:
        yield c


def mail(**kwargs) -> inbox.MailItem:
    defaults = dict(
        message_id="mid-1",
        sender="careers@acme.com",
        subject="Your application to Acme",
        text="",
        html="",
        received="2026-08-18T15:24:00+00:00",
        urls=[],
    )
    defaults.update(kwargs)
    if not defaults["urls"]:
        defaults["urls"] = inbox.extract_urls(defaults["html"], defaults["text"])
    return inbox.MailItem(**defaults)


def applied(conn, ext_id="gh:acme:1", company="Acme", title="Data Engineer",
            url="https://boards.greenhouse.io/acme/jobs/1"):
    posting = Posting(
        external_id=ext_id,
        title=title,
        company=company,
        location="Los Angeles, CA",
        url=url,
        description="Python and SQL.",
        ats="greenhouse",
        board_slug=company.lower(),
    )
    score, verdict, site = score_posting(posting.title, posting.description, posting.location)
    store.upsert_job(conn, posting.as_row(), score, verdict, site)
    store.set_status(conn, ext_id, store.Status.APPLIED)
    conn.execute(
        "UPDATE jobs SET applied_at='2026-08-01T00:00:00+00:00' WHERE external_id=?",
        (ext_id,),
    )
    conn.execute(
        "UPDATE applications SET applied_at='2026-08-01T00:00:00+00:00'"
        " WHERE external_id=?",
        (ext_id,),
    )
    return ext_id


REJECTION = """\
Hi Quintin,

Thank you for your interest in the Data Engineer role at Acme.

After reviewing your application, we have decided to move forward with other
candidates whose experience more closely aligns with the requirements.

We wish you the best in your job search.
"""

INVITE = """\
Hi Quintin,

Thanks for applying to the Data Engineer role. We would like to schedule a
30-minute phone screen with our hiring manager. Could you share your
availability for next week?
"""

ACK = """\
We have received your application for Data Engineer at Acme.
Please do not reply to this email.
"""


def test_rejection_language_is_recognised():
    verdict = replies.classify_reply(mail(text=REJECTION))
    assert verdict.kind == outcomes.REJECTED
    assert verdict.confidence >= 0.8


def test_interview_invite_beats_the_thanks_for_applying_boilerplate():
    verdict = replies.classify_reply(mail(text=INVITE))
    assert verdict.kind == outcomes.INTERVIEW


def test_receipt_is_an_ack_not_an_outcome():
    assert replies.classify_reply(mail(text=ACK)).kind == outcomes.ACK


def test_job_alert_digest_is_not_a_reply():
    """"Unfortunately that role closed" in a digest must not become a rejection."""
    item = mail(
        sender="jobalerts-noreply@linkedin.com",
        subject="30 new jobs for Data Engineer",
        text="Unfortunately some roles may have closed. Here are jobs for you.",
    )
    assert replies.classify_reply(item).kind == ""


def test_ordinary_mail_is_not_a_reply():
    assert replies.classify_reply(mail(subject="Lunch?", text="Free at noon?")).kind == ""


def test_rejection_is_matched_and_recorded(conn):
    applied(conn)
    result = replies.ingest_replies([mail(text=REJECTION)], conn)

    assert result.replies_seen == 1
    assert [kind for _, kind, _ in result.recorded] == [outcomes.REJECTED]
    row = conn.execute("SELECT * FROM jobs WHERE external_id='gh:acme:1'").fetchone()
    assert row["status"] == store.Status.REJECTED.value


def test_match_uses_the_posting_url_when_the_email_echoes_it(conn):
    applied(conn, company="Globex", ext_id="gh:globex:7",
            url="https://boards.greenhouse.io/globex/jobs/7")
    item = mail(
        sender="no-reply@my.greenhouse.io",
        subject="Update on your application",
        text=REJECTION.replace("Acme", "Globex")
        + "\nhttps://boards.greenhouse.io/globex/jobs/7\n",
    )
    result = replies.ingest_replies([item], conn)
    assert [i for i, _, _ in result.recorded] == ["gh:globex:7"]


def test_two_applications_at_one_company_are_not_guessed(conn):
    applied(conn, ext_id="gh:acme:1", title="Data Engineer")
    applied(conn, ext_id="gh:acme:2", title="Data Analyst",
            url="https://boards.greenhouse.io/acme/jobs/2")

    generic = mail(text="Thank you for applying to Acme. Unfortunately we have "
                        "decided to move forward with other candidates.")
    result = replies.ingest_replies([generic], conn)

    assert result.recorded == []
    assert len(result.ambiguous) == 1
    assert {c["external_id"] for c in result.ambiguous[0]["candidates"]} == {
        "gh:acme:1", "gh:acme:2"
    }
    assert conn.execute("SELECT COUNT(*) n FROM outcomes").fetchone()["n"] == 0


def test_the_title_disambiguates_when_the_email_names_it(conn):
    applied(conn, ext_id="gh:acme:1", title="Data Engineer")
    applied(conn, ext_id="gh:acme:2", title="Security Analyst",
            url="https://boards.greenhouse.io/acme/jobs/2")

    result = replies.ingest_replies([mail(text=REJECTION)], conn)
    assert [i for i, _, _ in result.recorded] == ["gh:acme:1"]


def test_a_reply_dated_before_the_application_is_ignored(conn):
    applied(conn)
    early = mail(text=REJECTION, received="2026-07-01T00:00:00+00:00")
    result = replies.ingest_replies([early], conn)
    assert result.recorded == []
    assert result.unmatched == 1


def test_rereading_the_mailbox_does_not_duplicate(conn):
    applied(conn)
    item = mail(text=REJECTION)
    replies.ingest_replies([item], conn)
    second = replies.ingest_replies([item], conn)

    assert second.recorded == []
    assert second.duplicates == 1
    assert conn.execute("SELECT COUNT(*) n FROM outcomes").fetchone()["n"] == 1


def test_no_applications_means_nothing_is_scanned(conn):
    result = replies.ingest_replies([mail(text=REJECTION)], conn)
    assert result.replies_seen == 0
    assert result.recorded == []


def test_the_reply_text_is_kept_for_reason_mining(conn):
    applied(conn)
    body = REJECTION + "\nThis role requires a bachelor's degree.\n"
    replies.ingest_replies([mail(text=body)], conn)

    row = conn.execute("SELECT * FROM outcomes").fetchone()
    assert "bachelor" in row["detail"]
    assert "degree" in row["signals"]


def test_company_normalisation_ignores_legal_suffixes():
    assert replies.normalize_company("Acme, Inc.") == "acme"
    assert replies.normalize_company("Globex Technologies LLC") == "globex"


def test_an_unmatched_ack_is_dropped_not_queued_for_confirmation(conn):
    """A receipt is not a label. Asking about one is a chore with no payoff."""
    applied(conn)
    item = mail(sender="no-reply@us.greenhouse-mail.io",
                subject="Your application to Globex",
                text="We have received your application for Data Engineer at Globex.")
    result = replies.ingest_replies([item], conn)

    assert result.recorded == []
    assert result.ambiguous == []
    assert result.orphans == []
    assert result.unmatched == 1


def test_a_rejection_for_an_unknown_application_is_surfaced(conn):
    """Applications made outside jobbot must not silently drop out of the data."""
    applied(conn)
    item = mail(
        sender="Hermeus <no-reply@hire.lever.co>",
        subject="Thanks for your interest in Hermeus, Quintin",
        text="Thank you for applying to Hermeus. We regret to inform you that "
             "the position you applied for has been filled.",
    )
    result = replies.ingest_replies([item], conn)

    assert result.recorded == []
    assert len(result.orphans) == 1
    assert result.orphans[0]["kind"] == outcomes.REJECTED
    assert result.orphans[0]["company"] == "Hermeus"


def test_company_guess_ignores_the_ats_that_sent_it():
    assert replies.guess_company(mail(sender="Hermeus <no-reply@hire.lever.co>")) == "Hermeus"
    assert replies.guess_company(
        mail(sender='"affirm.com Recruiting" <no-reply@appreview.gem.com>')
    ) == "affirm"
    guessed = replies.guess_company(
        mail(sender="no-reply@us.greenhouse-mail.io",
             subject="Thanks for your interest in Anduril")
    )
    assert guessed == "Anduril"


def test_registering_an_outside_application_lets_its_reply_land(conn):
    """The Hermeus case, end to end."""
    ext = outcomes.log_external_application(
        conn, company="Hermeus", title="Software Engineer",
        applied_at="2026-08-01T00:00:00+00:00",
    )
    item = mail(
        sender="Hermeus <no-reply@hire.lever.co>",
        subject="Thanks for your interest in Hermeus, Quintin",
        text="Thank you for applying to Hermeus. We regret to inform you that "
             "the position you applied for has been filled.",
    )
    result = replies.ingest_replies([item], conn)

    assert [i for i, _, _ in result.recorded] == [ext]
    row = outcomes.dataset(conn)[0]
    assert row["outcome"] == outcomes.REJECTED
    assert "role_closed" in row["reason_signals"]
    assert row["features"]["ats"] == "manual"


def test_registering_the_same_outside_application_twice_is_one_row(conn):
    a = outcomes.log_external_application(conn, company="Hermeus", title="Software Engineer")
    b = outcomes.log_external_application(conn, company="Hermeus", title="Software Engineer")
    assert a == b
    assert conn.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"] == 1


def test_an_outside_application_needs_both_company_and_title(conn):
    with pytest.raises(ValueError):
        outcomes.log_external_application(conn, company="Hermeus", title="")


def test_a_confirmation_email_never_becomes_a_job_posting(conn):
    """Thumbtack's receipt was ingested as a posting titled "(US-Based)".

    ATS confirmations carry an apply-style link, so the job-alert classifier
    read one as a new job. The fabricated row then out-matched the real posting
    when the confirmation was traced back, and the application was recorded
    against a job that does not exist.
    """
    item = mail(
        sender="Thumbtack Hiring Team <no-reply@ashbyhq.com>",
        subject="Application received - Software Engineer, AI/ML Infrastructure\n"
                " (US-Based)  at Thumbtack",
        text="Thanks for applying to Thumbtack. We have received your application.\n"
             "https://jobs.ashbyhq.com/thumbtack/3efb1a7b\n",
    )
    result = inbox.ingest_items([item], conn=conn, enrich_descriptions=False)

    assert result.ingested_new == 0
    assert result.skipped_kinds.get("employer_reply") == 1
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0


def test_a_confirmation_records_the_application_jobbot_missed(conn):
    """The employer's receipt is better evidence than a keystroke."""
    ext = "ashby:thumbtack:abc"
    posting = Posting(
        external_id=ext,
        title="Software Engineer, AI/ML Infrastructure",
        company="thumbtack",
        location="Remote, United States",
        url="https://jobs.ashbyhq.com/thumbtack/abc",
        description="Python. No degree required.",
        ats="ashby",
        board_slug="thumbtack",
    )
    s_, v_, site_ = score_posting(posting.title, posting.description, posting.location)
    store.upsert_job(conn, posting.as_row(), s_, v_, site_)

    item = mail(
        sender="Thumbtack Hiring Team <no-reply@ashbyhq.com>",
        subject="Application received - Software Engineer, AI/ML Infrastructure",
        text="We have received your application for Software Engineer, AI/ML "
             "Infrastructure at Thumbtack.",
        received="2026-08-21T21:50:08+00:00",
    )
    result = replies.register_applications_from_mail([item], conn)

    assert [i for i, _, _ in result.recorded] == [ext]
    row = conn.execute("SELECT * FROM jobs WHERE external_id=?", (ext,)).fetchone()
    assert row["status"] == store.Status.APPLIED.value
    # Dated from the confirmation, not from when the scan ran: the gap between
    # applying and hearing back is a feature the model uses.
    assert row["applied_at"] == "2026-08-21T21:50:08+00:00"


def test_a_confirmation_does_not_redate_an_application_already_on_file(conn):
    ext = applied(conn)
    before = conn.execute(
        "SELECT applied_at FROM jobs WHERE external_id=?", (ext,)
    ).fetchone()["applied_at"]

    item = mail(text="We have received your application for Data Engineer at Acme.",
                received="2026-08-21T21:50:08+00:00")
    result = replies.register_applications_from_mail([item], conn)

    assert result.recorded == []
    after = conn.execute(
        "SELECT applied_at FROM jobs WHERE external_id=?", (ext,)
    ).fetchone()["applied_at"]
    assert after == before


def test_a_confirmation_cannot_settle_two_different_jobs(conn):
    """The real bug: a Reddit confirmation for "Software Engineer, Ads" first
    resolved correctly to one posting. A duplicate-titled req scraped in
    afterward, and re-reading the SAME email on a later run matched it to
    that second, wrong posting too — because the dedupe key was
    (external_id, message_id), which a NEW external_id always satisfies.
    """
    first = applied(conn, ext_id="gh:acme:1", title="Software Engineer, Ads")
    item = mail(
        message_id="msg-ads-confirmation",
        text="We have received your application for Software Engineer, Ads at Acme.",
    )
    register = replies.register_applications_from_mail([item], conn)
    assert [i for i, _, _ in register.recorded] == []  # already applied, not in the pool
    ingest = replies.ingest_replies([item], conn)
    assert [i for i, _, _ in ingest.recorded] == [first]

    # A duplicate req with the identical title shows up on a later scrape —
    # exactly what happened for real. Distinct URL, or store.upsert_job's own
    # dedupe would (correctly) fold it into the first posting instead of
    # creating the second, genuinely-separate row this test needs.
    duplicate = applied(conn, ext_id="gh:acme:2", title="Software Engineer, Ads",
                        url="https://boards.greenhouse.io/acme/jobs/2")
    conn.execute("UPDATE jobs SET status='new', applied_at=NULL WHERE external_id=?",
                 (duplicate,))
    conn.execute("DELETE FROM applications WHERE external_id=?", (duplicate,))

    second_pass = replies.register_applications_from_mail([item], conn)

    assert second_pass.recorded == []
    assert second_pass.duplicates == 1
    row = conn.execute("SELECT status FROM jobs WHERE external_id=?", (duplicate,)).fetchone()
    assert row["status"] == "new"
    assert conn.execute(
        "SELECT COUNT(*) n FROM outcomes WHERE message_id='msg-ads-confirmation'"
    ).fetchone()["n"] == 1


def test_message_already_used_is_true_once_any_job_has_it(conn):
    add_ext = applied(conn)
    outcomes.record(conn, add_ext, outcomes.ACK, message_id="m-1")
    assert outcomes.message_already_used(conn, "m-1") is True
    assert outcomes.message_already_used(conn, "unrelated") is False


def test_an_empty_message_id_is_never_treated_as_used(conn):
    """Manual log entries carry no message id; they must not block each other."""
    assert outcomes.message_already_used(conn, "") is False


def test_conditional_hedge_language_is_not_a_rejection(conn):
    """The exact Figma bug: a real confirmation email got misread as a
    rejection because it contains the boilerplate "If you are not selected
    for this position, keep an eye on our jobs page" — a hedge about a
    decision that has not happened yet, not a rejection.
    """
    applied(conn, ext_id="gh:figma:1", company="Figma", title="Software Engineer")
    item = mail(
        sender="no-reply@figma.com",
        subject="Thank you for your application to Figma",
        text=(
            "Thank you for your interest in Figma! We wanted to let you know "
            "we received your application for Software Engineer, and we are "
            "delighted that you would consider joining our team.\n\n"
            "While we're not able to respond to every applicant, our "
            "recruiting team will contact you if your skills and experience "
            "are a strong match for the role. If you are not selected for "
            "this position, keep an eye on our jobs page as we're growing "
            "and adding openings."
        ),
    )
    verdict = replies.classify_reply(item)
    assert verdict.kind == outcomes.ACK


def test_a_genuine_past_tense_rejection_still_matches():
    for text in (
        "We have not selected for this role at this time.",
        "Unfortunately you were not selected for this position.",
    ):
        assert replies.classify_reply(mail(text=text)).kind == outcomes.REJECTED


def test_a_tie_is_broken_by_which_posting_jobbot_actually_opened(conn):
    """Two identically-titled reqs at one company, and a confirmation that
    names neither. The message cannot separate them, but jobbot knows which
    one it opened a tab for -- and a reply belongs to the posting that was on
    screen, not to the one it never showed him.

    Real case 2026-08-29: a Hightouch confirmation matched 10 open postings.
    """
    a = applied(conn, "gh:acme:1", title="Software Engineer",
                url="https://boards.greenhouse.io/acme/jobs/1")
    b = applied(conn, "gh:acme:2", title="Software Engineer",
                url="https://boards.greenhouse.io/acme/jobs/2")

    item = mail(subject="Thank you for applying to Acme", text=REJECTION)

    # Nothing opened yet: refuses to guess, and says so.
    match = replies.match_application(conn, item)
    assert match.row is None
    assert len(match.candidates) == 2

    # Once one of them is the tab that was actually prepared, it wins.
    store.log_event(conn, b, "prepared", "tab filled and left open for review")
    match = replies.match_application(conn, item)
    assert match.row is not None
    assert match.row["external_id"] == b
    assert "only one jobbot opened" in match.why


def test_two_prepared_candidates_still_refuse_to_guess(conn):
    """The tiebreaker narrows; it never invents certainty. If jobbot opened
    both, it is back to having nothing that separates them."""
    a = applied(conn, "gh:acme:1", title="Software Engineer",
                url="https://boards.greenhouse.io/acme/jobs/1")
    b = applied(conn, "gh:acme:2", title="Software Engineer",
                url="https://boards.greenhouse.io/acme/jobs/2")
    for ext in (a, b):
        store.log_event(conn, ext, "prepared", "tab filled")

    match = replies.match_application(
        conn, mail(subject="Thank you for applying to Acme", text=REJECTION)
    )
    assert match.row is None
    assert len(match.candidates) == 2


NEWSLETTER = """\
Home, Real Estate & Financing News - September 2026, Vol. 98

A Well-Planned Home; When's the Right Time to Refi; Mortgage Review.
Space planning is underrated. If you're considering a home remodel and may
need financing, reach out to discuss what your options might be.
Call Lindsay to schedule a call with our team about your options.

Unsubscribe | View this in your browser
"""


def test_a_newsletter_is_never_a_reply(conn):
    """Recorded as a Render INTERVIEW on 2026-08-26. Somewhere past the 8,000
    characters that get stored it said "schedule a call with", which is one of
    the strong interview phrases. `inbox` had already classified it a
    newsletter; the reply path simply never asked."""
    applied(conn, "gh:render:1", company="Render",
            title="Software Engineer, Billing")
    verdict = replies.classify_reply(
        mail(subject="Home, Real Estate & Financing News - September 2026, Vol. 98",
             sender="news@promortgage.example", text=NEWSLETTER)
    )
    assert verdict.kind == "", f"classified as {verdict.kind!r}"


def test_a_real_rejection_still_lands_despite_an_unsubscribe_link(conn):
    """The guard needs both halves. ATS mail carries unsubscribe links too, so
    the link alone must not suppress a genuine reply."""
    applied(conn, "gh:acme:1", title="Data Engineer")
    verdict = replies.classify_reply(
        mail(subject="Your application to Acme",
             text=REJECTION + "\n\nUnsubscribe | View this in your browser\n")
    )
    assert verdict.kind == "rejected"


def test_sender_domain_must_contain_the_whole_company_name(conn):
    """The old rule also accepted `domain_compact in company`, so any domain
    token of 4+ characters hiding inside a company name scored 0.85 — which is
    how an unrelated sender resolved to "render"."""
    applied(conn, "gh:render:1", company="Render", title="Billing Engineer",
            url="https://boards.greenhouse.io/render/jobs/1")
    # A domain that merely shares a fragment must not match.
    m = replies.match_application(
        conn, mail(sender="hello@ender.example", subject="Hi", text=REJECTION)
    )
    assert m.row is None or "sender domain" not in m.why
    # The company's own domain still matches.
    m = replies.match_application(
        conn, mail(sender="careers@render.com", subject="Update",
                   text="Thank you for your application. " + REJECTION)
    )
    assert m.row is not None


REDDIT_REJECTION = """\
Update from Reddit

Hi Quintin,
Thanks for your interest in Reddit! The team has reviewed your application for
the Fullstack Software Engineer, Notifications Lifecycle role, and after
carefully considering your background and qualifications we have decided not to
move forward in the process at this time.

Thanks again for your interest in Reddit and we wish you the best of luck.
"""


def test_not_moving_forward_is_a_rejection_not_an_interview(conn):
    """Recorded as an INTERVIEW on 2026-09-04. The interview pattern
    "move forward in" had no negation guard, and rejections are built out of
    that exact phrase: "decided NOT to move forward in the process"."""
    verdict = replies.classify_reply(
        mail(subject="Update from Reddit", sender="no-reply@greenhouse.io",
             text=REDDIT_REJECTION)
    )
    assert verdict.kind == "rejected", f"classified as {verdict.kind!r}"


def test_a_genuine_interview_invite_still_reads_as_an_interview(conn):
    """The negation guard must not suppress the real thing."""
    verdict = replies.classify_reply(
        mail(subject="Next steps",
             text="Hi Quintin, we loved your application and would like to move "
                  "you forward to the next round. Please share your availability "
                  "for a technical screen with our team.")
    )
    assert verdict.kind == "interview"


def test_rejection_outranks_interview_when_a_message_matches_both(conn):
    """They were both priority 2, so the winner fell to iteration order --
    and INTERVIEW is iterated first. Mislabelling a rejection as an interview
    raises false hope and writes a wrong terminal label into the training set."""
    verdict = replies.classify_reply(
        mail(subject="Update",
             text="We have decided not to move forward in the process. We will "
                  "not be scheduling a call with you at this time.")
    )
    assert verdict.kind == "rejected"


def test_the_email_naming_a_role_beats_which_tab_was_opened(conn):
    """A Reddit rejection naming "Notifications Lifecycle" was attached to
    "Data Movement Platform" because only the latter had a `prepared` event.
    Direct evidence from the message must outrank which tab happened to be
    open; prepared is a last resort, not a trump card."""
    named = applied(conn, "gh:reddit:1", company="Reddit",
                    title="Fullstack Software Engineer, Notifications Lifecycle",
                    url="https://boards.greenhouse.io/reddit/jobs/1")
    opened = applied(conn, "gh:reddit:2", company="Reddit",
                     title="Software Engineer, Data Movement Platform",
                     url="https://boards.greenhouse.io/reddit/jobs/2")
    store.log_event(conn, opened, "prepared", "tab filled and left open")

    match = replies.match_application(
        conn, mail(subject="Update from Reddit", sender="no-reply@greenhouse.io",
                   text=REDDIT_REJECTION)
    )
    assert match.row is not None
    assert match.row["external_id"] == named, f"matched {match.row['title']!r}"
    assert "names this role" in match.why

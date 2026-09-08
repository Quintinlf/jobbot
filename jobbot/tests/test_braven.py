"""Tests for the Braven Opportunity Board source.

The board is read through Airtable's private interface endpoint, so the parsing
here is pinned against a captured shape of that response. Two things are worth
guarding: that a schema change produces a loud failure rather than 101 empty
postings, and that the board metadata we synthesize into the description does
not itself trip the eligibility classifier.

Run:  python -m pytest jobbot/tests/ -q
"""

from __future__ import annotations

import copy

import pytest

from jobbot import braven, enrich
from jobbot.gating import (
    UNVERIFIED_MARKER, Gate, classify, gate_unverified, posting_gone,
)
from jobbot.scoring import score_posting
from jobbot.worksite import Worksite
from jobbot.worksite import classify as classify_worksite


# ── A trimmed copy of the real response shape ──────────────────────────────────

TABLE_ID = "tblNxxYmr9gbKu4Sv"

SCHEMA = {
    "id": TABLE_ID,
    "name": "MCM",
    "primaryColumnId": "fldPosition",
    "columns": [
        {"id": "fldPosition", "type": "text", "name": "Position"},
        {"id": "fldEmployer", "type": "foreignKey", "name": "Employer (Linked)"},
        {
            "id": "fldRegion",
            "type": "select",
            "name": "Region",
            "typeOptions": {"choices": {"selBos": {"name": "Boston"}}},
        },
        {"id": "fldLocations", "type": "text", "name": "Location(s)"},
        {
            "id": "fldKind",
            "type": "select",
            "name": "Type of Position",
            "typeOptions": {"choices": {"selJob": {"name": "Job"},
                                        "selIntern": {"name": "Internship"}}},
        },
        {"id": "fldDeadline", "type": "date", "name": "Application Deadline"},
        {
            "id": "fldModel",
            "type": "select",
            "name": "Working model",
            "typeOptions": {"choices": {"selRemote": {"name": "Remote"},
                                        "selHybrid": {"name": "Hybrid"},
                                        "selPerson": {"name": "In person"}}},
        },
        {
            "id": "fldCommunity",
            "type": "multiSelect",
            "name": "Career Community",
            "typeOptions": {"choices": {"selStem": {"name": "STEM"}}},
        },
        {"id": "fldApply", "type": "button", "name": "Apply Directly"},
        {"id": "fldReferral", "type": "formula", "name": "Referral position"},
        {"id": "fldAdded", "type": "formula", "name": "Date Added"},
        # The real board carries four unrelated columns all named "Field".
        {"id": "fldNoise", "type": "text", "name": "Field"},
    ],
}


def _row(rec_id, **overrides):
    cells = {
        "fldPosition": "Data Analyst",
        "fldEmployer": [{"foreignRowId": "recX", "foreignRowDisplayName": "Acme"}],
        "fldRegion": "selBos",
        "fldLocations": "Boston, MA",
        "fldKind": "selJob",
        "fldDeadline": "2026-09-01T00:00:00.000Z",
        "fldModel": "selHybrid",
        "fldCommunity": ["selStem"],
        "fldApply": {"label": "Visit Job Posting", "url": "https://example.com/job/1"},
        "fldReferral": "Yes https://link.braven.org/ReferralRequest",
        "fldAdded": "2026-07-13T18:13:14.000Z",
        "fldNoise": "ignore me",
    }
    cells.update(overrides)
    return {"id": rec_id, "cellValuesByColumnId": cells}


def _payload(rows):
    # deepcopy: the restructure test edits the schema it is handed, and a
    # shared reference would leak that edit into every test after it.
    return {
        "tableSchemas": [copy.deepcopy(SCHEMA)],
        "preloadPageQueryResults": {
            "tableDataById": {TABLE_ID: {"partialRowById": {r["id"]: r for r in rows}}}
        },
    }


@pytest.fixture
def board(monkeypatch):
    """fetch_board() with the network calls replaced by a captured payload."""
    def build(rows):
        monkeypatch.setattr(braven, "_session", lambda: object())
        monkeypatch.setattr(braven, "_bootstrap", lambda s, u: None)
        monkeypatch.setattr(braven, "_read_board", lambda s, b: _payload(rows))
        return braven.fetch_board()
    return build


# ── Parsing ────────────────────────────────────────────────────────────────────

def test_row_becomes_a_posting(board):
    (posting,) = board([_row("rec1")])

    assert posting.external_id == "braven:rec1"
    assert posting.title == "Data Analyst"
    assert posting.company == "Acme"          # linked record, not a raw row id
    assert posting.location == "Boston, MA"
    assert posting.url == "https://example.com/job/1"
    assert posting.ats == "braven"
    assert posting.department == "STEM"


def test_select_ids_are_resolved_to_names(board):
    (posting,) = board([_row("rec1")])
    # A leaked "selBos" here would mean the whole board reads as gibberish.
    assert "sel" not in posting.description
    assert "Boston" in posting.description
    assert "Job" in posting.description


def test_region_used_when_no_explicit_location(board):
    (posting,) = board([_row("rec1", fldLocations="")])
    assert posting.location == "Boston"


def test_rows_without_an_apply_link_are_dropped(board):
    """A posting with nowhere to apply is not a posting."""
    rows = [_row("rec1"), _row("rec2", fldApply=None), _row("rec3", fldPosition="")]
    assert [p.external_id for p in board(rows)] == ["braven:rec1"]


def test_missing_position_column_raises(board, monkeypatch):
    """A restructured base must fail loudly, not return empty postings."""
    payload = _payload([_row("rec1")])
    payload["tableSchemas"][0]["columns"] = [
        {"id": "fldX", "type": "text", "name": "Renamed"}
    ]
    monkeypatch.setattr(braven, "_session", lambda: object())
    monkeypatch.setattr(braven, "_bootstrap", lambda s, u: None)
    monkeypatch.setattr(braven, "_read_board", lambda s, b: payload)

    with pytest.raises(RuntimeError, match="restructured"):
        braven.fetch_board()


def test_bad_share_url_rejected():
    with pytest.raises(ValueError, match="share URL"):
        braven._bootstrap(None, "https://example.com/not-airtable")


def test_quoted_json_unescapes_twice():
    html = r'{"accessPolicy":"{\"shareId\":\"shrABC\",\"expires\":\"2026-09-10\"}"}'
    assert braven._quoted_json(html, "accessPolicy")["shareId"] == "shrABC"


# ── Board metadata must not mislead the classifiers ────────────────────────────

def test_referral_is_surfaced(board):
    (posting,) = board([_row("rec1")])
    assert braven.has_referral(posting.description)
    assert "link.braven.org" in posting.description


def test_no_referral_line_when_not_a_referral_position(board):
    (posting,) = board([_row("rec1", fldReferral="No")])
    assert not braven.has_referral(posting.description)


def test_deadline_is_a_date_not_a_timestamp(board):
    (posting,) = board([_row("rec1")])
    assert "2026-09-01" in posting.description
    assert "T00:00:00" not in posting.description


@pytest.mark.parametrize("choice,expected", [
    ("selRemote", Worksite.REMOTE),
    ("selHybrid", Worksite.HYBRID),
    ("selPerson", Worksite.ONSITE),
])
def test_working_model_is_phrased_so_worksite_reads_it(board, choice, expected):
    """The board states the arrangement outright; don't make worksite.py guess.

    It only reads prose, so the field is restated as a sentence — "In person"
    on its own matches none of its patterns.
    """
    (posting,) = board([_row("rec1", fldModel=choice, fldLocations="Boston, MA")])
    assert classify_worksite(posting.description, posting.location).worksite is expected


def test_board_metadata_alone_does_not_invent_a_gate(board):
    """Synthesized text must not read as degree or enrollment language.

    "Type: Internship" and a career community called "Government, Law,
    Education, and NPOs" are both one careless pattern away from classifying
    every row as gated.
    """
    (posting,) = board([_row("rec1", fldKind="selIntern")])
    assert classify(posting.description, posting.title).gate is Gate.OPEN


def test_real_description_still_drives_the_gate(board):
    """Enrichment text is what the classifier should actually be reading."""
    (posting,) = board([_row("rec1")])
    posting.description += "\n\nYou must be currently enrolled in a degree program."
    assert classify(posting.description, posting.title).gate is Gate.ENROLLMENT


# ── Enrichment ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/123",
    "https://www.indeed.com/viewjob?jk=abc",
    "https://uk.indeed.com/viewjob?jk=abc",
])
def test_bot_protected_sites_are_never_fetched(url):
    """Reading around bot detection is the line this project doesn't cross."""
    assert enrich.is_blocked(url)
    assert enrich.describe(url) == ""


def test_ordinary_hosts_are_not_blocked():
    assert not enrich.is_blocked("https://boards.greenhouse.io/acme/jobs/1")
    # Substring, not suffix: a host merely containing a blocked name is fine.
    assert not enrich.is_blocked("https://indeed.com.example.org/job/1")


def test_thin_pages_are_rejected_rather_than_classified(monkeypatch):
    """A cookie banner is not a job description, and gating it produces noise."""
    monkeypatch.setattr(enrich, "describe", lambda url, session=None: "Accept cookies")
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)

    posting = _posting("https://example.com/job/1")
    result = enrich.attach_descriptions([posting])

    assert result.too_thin == 1
    assert result.enriched == 0
    assert "Accept cookies" not in posting.description
    assert posting.description.startswith("[Braven Opportunity Board]\nType: Job")


def test_enrichment_appends_and_keeps_board_metadata(monkeypatch):
    body = "We are hiring. " * 100
    monkeypatch.setattr(enrich, "describe", lambda url, session=None: body)
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)

    posting = _posting("https://example.com/job/1")
    result = enrich.attach_descriptions([posting])

    assert result.enriched == 1
    assert posting.description.startswith("[Braven Opportunity Board]")
    assert "We are hiring." in posting.description


def test_unreachable_hosts_are_counted_per_host(monkeypatch):
    monkeypatch.setattr(enrich, "describe", lambda url, session=None: "")
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)

    postings = [
        _posting("https://delta.avature.net/job/1"),
        _posting("https://delta.avature.net/job/2"),
    ]
    result = enrich.attach_descriptions(postings)

    assert result.failed == 2
    assert result.hosts_failed == {"delta.avature.net": 2}


# ── An unread posting must not pass as a checked one ───────────────────────────

def test_unread_posting_is_marked_unverified(monkeypatch):
    monkeypatch.setattr(enrich, "describe", lambda url, session=None: "")
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)

    posting = _posting("https://delta.avature.net/job/1")
    enrich.attach_descriptions([posting])

    assert gate_unverified(posting.description)


def test_successfully_read_posting_is_not_marked(monkeypatch):
    monkeypatch.setattr(enrich, "describe", lambda url, session=None: "We are hiring. " * 100)
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)

    posting = _posting("https://example.com/job/1")
    enrich.attach_descriptions([posting])

    assert not gate_unverified(posting.description)


def test_unverified_posting_says_so_instead_of_showing_no_evidence():
    """Blank evidence on an unread posting reads as 'checked, nothing found'."""
    verdict = classify(f"Some board metadata\n{UNVERIFIED_MARKER}", "Data Analyst")

    assert verdict.gate is Gate.OPEN
    assert verdict.evidence and "could not be read" in verdict.evidence[0]


def test_unverified_posting_does_not_collect_the_open_bonus():
    """Otherwise not reading a posting is worth +10 points."""
    title = "Data Analyst"
    checked, _, _ = score_posting(title=title, description="We are hiring an analyst.")
    unread, _, _ = score_posting(
        title=title, description=f"We are hiring an analyst.\n{UNVERIFIED_MARKER}"
    )

    assert checked.total - unread.total == 10
    assert any("not checked" in r for r in unread.reasons)


def test_marker_does_not_suppress_a_gate_that_was_actually_found():
    """A thin page can still contain the knockout; the marker must not mask it."""
    text = f"Must be currently enrolled in a degree program.\n{UNVERIFIED_MARKER}"
    assert classify(text).gate is Gate.ENROLLMENT


# ── Real phrasings found on this board ─────────────────────────────────────────

def test_comparable_qualifications_opens_the_gate():
    """From the Massachusetts Life Sciences Center program, verbatim.

    The opening is worded around "qualifications" rather than "experience",
    with a verb between "or" and the adjective, so none of the original
    equivalent-experience patterns reached it and it read as soft_degree.
    """
    text = (
        "The MLSC will reimburse host organizations for pay rates of up to $20 "
        "per hour for interns pursuing or having completed their Bachelor's "
        "degree or possessing comparable data science qualifications."
    )
    assert classify(text).gate is Gate.EQUIVALENT_OK


def test_comparable_qualifications_does_not_fire_on_unrelated_text():
    text = "Compare your qualifications against the role before applying."
    assert classify(text).gate is not Gate.EQUIVALENT_OK


# ── Postings that have been taken down ─────────────────────────────────────────

def test_gone_posting_is_marked_and_knocked_out(monkeypatch):
    def gone(url, session=None):
        raise enrich.PostingGone(url)

    monkeypatch.setattr(enrich, "describe", gone)
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)

    posting = _posting("https://example.com/job/1")
    result = enrich.attach_descriptions([posting])

    assert result.gone == 1
    assert posting_gone(posting.description)
    # Not also counted as unreachable — the two mean different things.
    assert result.failed == 0
    assert not gate_unverified(posting.description)

    score, _, _ = score_posting(title=posting.title, description=posting.description)
    assert score.knockout == "posting no longer accepting applications"


@pytest.mark.parametrize("status", [404, 410])
def test_gone_statuses_raise(monkeypatch, status):
    class Resp:
        status_code = status
        headers = {"Content-Type": "text/html"}
        text = ""

    session = type("S", (), {"get": lambda self, url, **kw: Resp()})()
    with pytest.raises(enrich.PostingGone):
        enrich._page_text(session, "https://example.com/job/1")


@pytest.mark.parametrize("status", [202, 403, 500])
def test_other_failures_are_not_treated_as_gone(monkeypatch, status):
    """A bot-check 202 or a 403 says nothing about whether the job is open."""
    import requests

    class Resp:
        status_code = status
        headers = {"Content-Type": "text/html"}
        text = "<html><body></body></html>"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

    session = type("S", (), {"get": lambda self, url, **kw: Resp()})()
    try:
        enrich._page_text(session, "https://example.com/job/1")
    except enrich.PostingGone:
        pytest.fail("non-404 status must not be reported as a removed posting")
    except requests.HTTPError:
        pass


def _posting(url: str):
    from jobbot.boards import Posting

    return Posting(
        external_id="braven:rec1",
        title="Data Analyst",
        company="Acme",
        location="Boston, MA",
        url=url,
        description="[Braven Opportunity Board]\nType: Job",
        ats="braven",
        board_slug="braven",
    )

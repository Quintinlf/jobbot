"""Reading UCLA's board, which is not Workday and was missing because of it.

Every Workday tenant/site slug guessed for UCLA returned HTTP 422, and the
conclusion drawn from that was "the slug has to be read off the real careers
URL". That was wrong about the cause: UCLA is on iCIMS, behind a Radancy front
end at jobs.ucla.edu, so no Workday slug was ever going to resolve. The front
end has a public JSON endpoint — the one its own page calls — which answers
with the whole posting inline.
"""

from __future__ import annotations

import pytest

from jobbot import boards_local


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _job(req_id="11437", title="Research Data Analyst 1", **over):
    data = {
        "req_id": req_id,
        "title": title,
        "description": "<p>Work with Python and SQL.</p>",
        "qualifications": "<p>Bachelor's degree or equivalent experience.</p>",
        "full_location": "Los Angeles, California",
        "apply_url": f"https://careers-ucla.icims.com/jobs/{req_id}/login",
        "department": "Research",
        "posted_date": "2026-09-05T00:59:00+0000",
    }
    data.update(over)
    return {"data": data}


def _serve(pages, calls=None):
    """A fake SESSION.get that returns each page in turn."""
    def get(url, **kwargs):
        if calls is not None:
            calls.append(url)
        return _Response({"jobs": pages.pop(0) if pages else []})
    return get


def test_a_posting_is_read_whole(monkeypatch):
    monkeypatch.setattr(boards_local.SESSION, "get", _serve([[_job()]]))
    posting = boards_local.fetch_radancy("ucla", "https://jobs.ucla.edu")[0]

    assert posting.external_id == "radancy:ucla:11437"
    assert posting.title == "Research Data Analyst 1"
    assert posting.company == "ucla"
    assert posting.location == "Los Angeles, California"
    assert posting.url == "https://careers-ucla.icims.com/jobs/11437/login"
    assert posting.ats == "radancy"


def test_qualifications_are_part_of_the_description(monkeypatch):
    """The degree language lives in `qualifications`, not `description`, and
    the gate classifier only reads one field. Dropping it would classify every
    UCLA posting as having no degree language at all."""
    monkeypatch.setattr(boards_local.SESSION, "get", _serve([[_job()]]))
    posting = boards_local.fetch_radancy("ucla", "https://jobs.ucla.edu")[0]

    assert "Python and SQL" in posting.description
    assert "equivalent experience" in posting.description


def test_pagination_stops_on_a_short_page(monkeypatch):
    calls: list[str] = []
    full = [_job(req_id=str(i)) for i in range(3)]
    monkeypatch.setattr(
        boards_local.SESSION, "get", _serve([full, [_job(req_id="99")]], calls)
    )
    got = boards_local.fetch_radancy("ucla", "https://jobs.ucla.edu", limit=3)

    assert len(got) == 4
    assert len(calls) == 2  # stopped once a page came back short


def test_an_empty_first_page_is_raised_not_swallowed(monkeypatch):
    """A board that silently returns zero rows forever is the failure mode
    this whole module was written to avoid."""
    monkeypatch.setattr(boards_local.SESSION, "get", _serve([[]]))
    with pytest.raises(RuntimeError, match="needs re-checking"):
        boards_local.fetch_radancy("ucla", "https://jobs.ucla.edu")


def test_one_board_failing_does_not_take_the_others(monkeypatch):
    def get(url, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(boards_local.SESSION, "get", get)
    assert boards_local.fetch_all_radancy([("ucla", "https://jobs.ucla.edu")]) == []


def test_rows_without_an_id_are_skipped(monkeypatch):
    monkeypatch.setattr(
        boards_local.SESSION, "get",
        _serve([[{"data": {"title": "No id here"}}, _job()]]),
    )
    got = boards_local.fetch_radancy("ucla", "https://jobs.ucla.edu")
    assert [p.external_id for p in got] == ["radancy:ucla:11437"]


def test_ucla_is_wired_into_the_local_sweep():
    assert ("ucla", "https://jobs.ucla.edu") in boards_local.RADANCY_BOARDS


# -- The local sweep must actually reach the database ---------------------------

def test_local_postings_survive_a_broken_progress_callback(monkeypatch):
    """`refresh` logged "local boards skipped" and threw away everything it had
    just fetched, because on_board was called with two arguments where every
    caller's callback takes three. The TypeError was caught by the same try
    that wrapped the fetch. 674 postings a run, silently — USC, LA County,
    UCLA, LA City, Long Beach, Pasadena. Bookkeeping must not lose data."""
    from jobbot import boards, pipeline

    sentinel = [
        boards.Posting(
            external_id="radancy:ucla:1",
            title="Slate Developer",
            company="ucla",
            location="Los Angeles, California",
            url="https://example.invalid/1",
            description="Python. Bachelor's degree or equivalent experience.",
            ats="radancy",
            board_slug="ucla",
        )
    ]
    # `refresh` refuses to run with no verified boards at all, and that check
    # comes first. Without this the test only passed because a real
    # data/verified_boards.json happened to be sitting on the machine.
    monkeypatch.setattr(boards, "load_verified", lambda: {"acme": "greenhouse"})
    monkeypatch.setattr(boards, "fetch_all", lambda *a, **k: [])
    monkeypatch.setattr(boards_local, "fetch_all_local", lambda: sentinel)

    seen: list[tuple] = []
    captured: list = []
    monkeypatch.setattr(
        pipeline, "ingest",
        lambda postings, conn: captured.extend(postings) or pipeline.RefreshResult(),
    )
    monkeypatch.setattr(pipeline, "retire_delisted", lambda conn, postings: 0)

    pipeline.refresh(
        on_board=lambda *args: seen.append(args),
        conn=object(),
    )

    assert [p.external_id for p in captured] == ["radancy:ucla:1"]
    assert seen and len(seen[-1]) == 3  # the callback every caller expects

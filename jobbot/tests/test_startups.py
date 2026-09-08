"""Finding small companies' boards, and not finding someone else's by mistake.

The seed list was 92 name-brand employers because it was typed by hand.
`startups.py` asks YC's own directory who its companies are instead. Two things
have to hold for that to be worth doing: the slug guesses have to find real
boards, and a generic slug must not silently attribute a stranger's board to a
twenty-person company.
"""

from __future__ import annotations

import json

import pytest

from jobbot import boards, config, startups


# -- Which companies are worth probing -----------------------------------------

def test_only_active_companies_in_the_size_window():
    directory = [
        {"slug": "a", "status": "Active", "teamSize": 12},
        {"slug": "b", "status": "Inactive", "teamSize": 12},   # dead
        {"slug": "c", "status": "Acquired", "teamSize": 12},   # absorbed
        {"slug": "d", "status": "Active", "teamSize": 1},      # solo founder
        {"slug": "e", "status": "Active", "teamSize": 4000},   # not small
        {"slug": "f", "status": "Active", "teamSize": None},   # unknown
    ]
    assert [c["slug"] for c in startups.small_companies(directory)] == ["a"]


def test_biggest_first():
    """An interrupted run should have found the boards most likely to be live."""
    directory = [
        {"slug": "small", "status": "Active", "teamSize": 3},
        {"slug": "big", "status": "Active", "teamSize": 48},
        {"slug": "mid", "status": "Active", "teamSize": 20},
    ]
    assert [c["slug"] for c in startups.small_companies(directory)] == [
        "big", "mid", "small",
    ]


# -- Slug guessing --------------------------------------------------------------

def test_yc_slug_is_tried_first():
    assert startups.candidate_slugs({"slug": "posh"})[0] == "posh"


@pytest.mark.parametrize(
    "yc_slug, website, expected",
    [
        # Real cases from the 60-company sample. Each of these was found only
        # because of a variant; the YC slug alone missed all four.
        ("finny-ai", "https://www.finny.ai/", "finny"),
        ("empirical-health", "https://empirical.health", "empirical"),
        ("imt-care", "https://imt.care", "imt"),
        ("tekton-dynamics", "https://tektondynamics.com", "tektondynamics"),
    ],
)
def test_variants_cover_the_names_that_were_missed(yc_slug, website, expected):
    assert expected in startups.candidate_slugs(
        {"slug": yc_slug, "website": website}
    )


def test_only_known_suffixes_are_stripped():
    """Dropping any trailing word would turn 'scale-computing' into 'scale'
    and find a completely different company's board."""
    assert "scale" not in startups.candidate_slugs({"slug": "scale-computing"})


def test_platform_hosts_are_not_treated_as_the_company():
    """A site on someone else's platform would otherwise contribute 'notion'
    as a slug, once per company that hosts there."""
    assert "notion" not in startups.candidate_slugs(
        {"slug": "tiny-co", "website": "https://tiny.notion.site"}
    )


# -- Slug collisions ------------------------------------------------------------

def test_a_board_far_bigger_than_the_company_is_a_collision():
    """YC's 'Agency' (50 people) resolved to a Greenhouse board with 829 open
    roles — a staffing firm of the same name."""
    assert startups.implausible_board(829, 50) is True


def test_a_small_company_hiring_hard_is_kept():
    """35 openings at 50 people is a company worth applying to, not a
    collision. This is the case the rule must not break."""
    assert startups.implausible_board(35, 50) is False


def test_the_floor_protects_the_smallest_companies():
    """Without a floor, a 3-person company could only ever have 6 postings."""
    assert startups.implausible_board(12, 3) is False
    assert startups.implausible_board(400, 3) is True


# -- Probing --------------------------------------------------------------------

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def test_a_collision_is_cached_as_a_miss(data_dir, monkeypatch):
    """The board is real, it is just not this company's. Re-probing it next
    run would find the same wrong answer at the same cost."""
    monkeypatch.setattr(
        boards, "discover",
        lambda slug, order=None: ("greenhouse", [object()] * 829),
    )
    cache: dict = {}
    found = list(startups.discover_boards(
        [{"name": "Agency", "slug": "agency", "teamSize": 50}],
        cache=cache, pause=0,
    ))

    assert found == []
    assert cache["agency"] is None


def test_a_real_board_is_yielded_and_cached(data_dir, monkeypatch):
    monkeypatch.setattr(
        boards, "discover",
        lambda slug, order=None: ("ashby", [object()] * 16),
    )
    cache: dict = {}
    found = list(startups.discover_boards(
        [{"name": "Hedge", "slug": "hedge", "teamSize": 2}], cache=cache, pause=0,
    ))

    assert found == [("hedge", "ashby", 16)]
    assert cache["hedge"] == "ashby"


def test_companies_already_verified_are_not_probed(data_dir, monkeypatch):
    """A full pass is tens of thousands of requests; spending any of them
    re-confirming companies.txt is waste."""
    calls = []

    def spy(slug, order=None):
        calls.append(slug)
        return ("ashby", [object()])

    monkeypatch.setattr(boards, "discover", spy)
    found = list(startups.discover_boards(
        [{"name": "Supabase", "slug": "supabase", "teamSize": 40}],
        known={"supabase": "ashby"}, cache={}, pause=0,
    ))

    assert found == []
    assert calls == []


def test_a_cached_miss_is_not_probed_again(data_dir, monkeypatch):
    calls = []

    def spy(slug, order=None):
        calls.append(slug)
        return None

    monkeypatch.setattr(boards, "discover", spy)
    list(startups.discover_boards(
        [{"name": "Nope", "slug": "nope", "teamSize": 10}],
        cache={"nope": None}, pause=0,
    ))

    assert calls == []


def test_the_cache_survives_a_round_trip(data_dir):
    startups.save_probe_cache({"hedge": "ashby", "agency": None})
    assert startups.load_probe_cache() == {"hedge": "ashby", "agency": None}


def test_a_corrupt_cache_is_not_fatal(data_dir):
    (data_dir / "startup_probes.json").write_text("{not json", encoding="utf-8")
    assert startups.load_probe_cache() == {}


# -- Ordering -------------------------------------------------------------------

def test_ashby_is_probed_first():
    """9 of the 12 boards in the sample were Ashby, and a miss costs one
    request per system tried before the hit."""
    assert startups.ATS_ORDER[0] == "ashby"


def test_discover_honours_the_order():
    tried = []

    def fake(slug):
        tried.append(slug)
        return []

    original = dict(boards.FETCHERS)
    try:
        boards.FETCHERS.clear()
        boards.FETCHERS.update({k: fake for k in ("greenhouse", "lever", "ashby")})
        boards.discover("x", order=("ashby", "lever"))
    finally:
        boards.FETCHERS.clear()
        boards.FETCHERS.update(original)

    assert len(tried) == 2  # only the two named, not all three

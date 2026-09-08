"""The hackathon clock. Hours have to be measured, not remembered."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobbot import hackathon


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "hackathons.json"
    monkeypatch.setattr(hackathon, "HACKATHONS_PATH", p)
    return p


def ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )


def test_a_closed_session_counts_its_own_hours():
    s = hackathon.Session(started=ago(3), ended=ago(1))
    assert 1.9 < s.hours < 2.1
    assert s.running is False


def test_a_running_session_counts_up_to_now():
    s = hackathon.Session(started=ago(2))
    assert 1.9 < s.hours < 2.1
    assert s.running is True


def test_starting_twice_does_not_double_count():
    """Two open sessions would each count to now, inventing hours."""
    h = hackathon.Hackathon(name="X")
    hackathon.start(h)
    hackathon.start(h)
    assert sum(1 for s in h._sessions() if s.running) == 1


def test_stop_closes_the_clock_and_keeps_the_total():
    h = hackathon.Hackathon(name="X", sessions=[hackathon.Session(started=ago(4))])
    hackathon.stop(h)
    assert h.running is False
    assert 3.9 < h.hours_spent < 4.1

    # Stopping again must not extend anything.
    before = h.hours_spent
    hackathon.stop(h)
    assert h.hours_spent == pytest.approx(before, abs=0.01)


def test_hours_accumulate_across_sessions():
    h = hackathon.Hackathon(name="X", sessions=[
        hackathon.Session(started=ago(10), ended=ago(8)),
        hackathon.Session(started=ago(5), ended=ago(2)),
    ])
    assert 4.9 < h.hours_spent < 5.1


def test_budget_shrinks_when_the_odds_are_bad():
    """A huge pool you will not win is worth less than a small one you will.

    This is the whole point of the calculation — a $60k prize pool is the most
    tempting thing on the page and usually the worst use of the time.
    """
    long_shot = hackathon.suggest_budget(60000, 0.01)
    realistic = hackathon.suggest_budget(500, 0.40)
    assert long_shot == 30.0
    assert realistic == 10.0
    assert hackathon.suggest_budget(1500, 0.0) == 0.0
    assert hackathon.suggest_budget(0, 0.5) == 0.0


def test_pace_says_how_hard_you_would_have_to_go():
    deadline = (datetime.now(timezone.utc) + timedelta(days=4)).strftime("%Y-%m-%d")
    h = hackathon.Hackathon(name="X", deadline=deadline, budget_hours=20)
    pace = h.pace()
    assert pace is not None
    assert 4.5 < pace < 7.0


def test_a_passed_deadline_has_no_pace():
    h = hackathon.Hackathon(name="X", deadline="2020-01-01", budget_hours=20)
    assert h.days_to_deadline < 0
    assert h.pace() is None


def test_round_trip_through_disk(path):
    h = hackathon.Hackathon(name="Prometheus", prize_value=1500, budget_hours=11)
    hackathon.start(h)
    hackathon.stop(h)
    hackathon.save([h])

    loaded = hackathon.load()
    assert len(loaded) == 1
    assert loaded[0].name == "Prometheus"
    assert loaded[0].budget_hours == 11
    assert loaded[0].running is False
    assert len(loaded[0]._sessions()) == 1


def test_find_will_not_guess_between_two(path):
    items = [hackathon.Hackathon(name="AI Challenge One"),
             hackathon.Hackathon(name="AI Challenge Two")]
    assert hackathon.find(items, "AI Challenge") is None
    assert hackathon.find(items, "One").name == "AI Challenge One"


def test_stop_all_clears_every_running_clock():
    items = [hackathon.Hackathon(name="A"), hackathon.Hackathon(name="B")]
    for h in items:
        hackathon.start(h)
    stopped = hackathon.stop_all(items)
    assert set(stopped) == {"A", "B"}
    assert not any(h.running for h in items)


def test_render_flags_going_over_budget():
    h = hackathon.Hackathon(name="X", budget_hours=2,
                            sessions=[hackathon.Session(started=ago(9), ended=ago(1))])
    assert "OVER BUDGET" in hackathon.render(h)

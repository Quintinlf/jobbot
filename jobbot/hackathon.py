"""Hackathon shortlist and an honest clock.

Two questions this answers, both of which are easy to get wrong by feel:

  * how many hours this is worth — decided once, in advance, before sunk cost
    starts arguing
  * how many hours have actually gone in — measured, not estimated after the
    fact, because the estimate is always low

The budget is not a prediction of how long the build takes. It is the point at
which the expected prize stops being worth the time, which is a different
number and the one that matters when the goal is a specific piece of hardware
rather than a trophy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jobbot import config

HACKATHONS_PATH = config.DATA_DIR / "hackathons.json"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class Session:
    started: str
    ended: str = ""
    note: str = ""

    @property
    def hours(self) -> float:
        start, end = _parse(self.started), _parse(self.ended) or now()
        if not start:
            return 0.0
        return max(0.0, (end - start).total_seconds() / 3600)

    @property
    def running(self) -> bool:
        return not self.ended


@dataclass
class Hackathon:
    name: str
    url: str = ""
    deadline: str = ""
    prize: str = ""
    prize_value: float = 0.0
    budget_hours: float = 0.0
    notes: str = ""
    status: str = "shortlisted"     # shortlisted | building | submitted | done
    sessions: list = field(default_factory=list)

    def _sessions(self) -> list[Session]:
        out = []
        for s in self.sessions:
            out.append(s if isinstance(s, Session) else Session(**s))
        return out

    @property
    def hours_spent(self) -> float:
        return sum(s.hours for s in self._sessions())

    @property
    def running(self) -> bool:
        return any(s.running for s in self._sessions())

    @property
    def hours_left(self) -> float:
        return max(0.0, self.budget_hours - self.hours_spent)

    @property
    def days_to_deadline(self):
        end = _parse(self.deadline)
        if not end:
            return None
        return (end - now()).total_seconds() / 86400

    @property
    def dollars_per_hour(self):
        """What the time is worth IF you win. Not an expected value."""
        if not self.hours_spent:
            return None
        return self.prize_value / self.hours_spent if self.prize_value else 0.0

    def pace(self):
        """Hours per day needed to spend the budget before the deadline."""
        days = self.days_to_deadline
        if days is None or days <= 0:
            return None
        return self.hours_left / days


def load(path: Path | None = None) -> list[Hackathon]:
    path = path or HACKATHONS_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for entry in data:
        entry = dict(entry)
        entry["sessions"] = [Session(**s) for s in entry.get("sessions", [])]
        known = {f for f in Hackathon.__dataclass_fields__}
        out.append(Hackathon(**{k: v for k, v in entry.items() if k in known}))
    return out


def save(items: list[Hackathon], path: Path | None = None) -> Path:
    config.ensure_dirs()
    path = path or HACKATHONS_PATH
    payload = []
    for h in items:
        entry = asdict(h)
        entry["sessions"] = [asdict(s) if not isinstance(s, dict) else s
                             for s in h._sessions()]
        payload.append(entry)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def find(items: list[Hackathon], needle: str) -> Hackathon | None:
    text = (needle or "").strip().lower()
    if not text:
        return None
    for h in items:
        if h.name.lower() == text:
            return h
    matches = [h for h in items if text in h.name.lower()]
    return matches[0] if len(matches) == 1 else None


def start(hack: Hackathon, note: str = "") -> Session:
    """Clock in. Stops any session already running so hours cannot double-count."""
    stop(hack)
    session = Session(started=now().isoformat(timespec="seconds"), note=note)
    hack.sessions.append(session)
    hack.status = "building"
    return session


def stop(hack: Hackathon) -> float:
    """Clock out. Returns hours added, 0.0 if nothing was running."""
    added = 0.0
    for session in hack._sessions():
        if session.running:
            added += session.hours
    hack.sessions = [
        asdict(s) if not isinstance(s, dict) else s for s in hack._sessions()
    ]
    for entry in hack.sessions:
        if not entry.get("ended"):
            entry["ended"] = now().isoformat(timespec="seconds")
    return added


def stop_all(items: list[Hackathon]) -> list[str]:
    stopped = []
    for hack in items:
        if hack.running:
            stop(hack)
            stopped.append(hack.name)
    return stopped


def suggest_budget(prize_value: float, win_odds: float, floor_rate: float = 20.0) -> float:
    """Hours worth spending, given what winning pays and how likely it is.

    expected value = prize x odds; budget = that, divided by the hourly rate
    you would otherwise accept. Deliberately blunt: the useful part is that it
    collapses when the odds are bad, which is exactly when a big prize pool is
    most tempting. A $60,000 pool at 1% is worth about thirty hours; a $500
    prize at 40% is worth ten, and you actually get the second one.
    """
    if prize_value <= 0 or win_odds <= 0:
        return 0.0
    return round((prize_value * win_odds) / max(floor_rate, 1.0), 1)


def render(hack: Hackathon) -> str:
    lines = [f"{hack.name}  [{hack.status}]"]
    if hack.url:
        lines.append(f"  {hack.url}")
    if hack.prize:
        lines.append(f"  prize    : {hack.prize}")

    spent, budget = hack.hours_spent, hack.budget_hours
    bar = ""
    if budget:
        filled = min(20, int(20 * spent / budget))
        over = spent > budget
        bar = f"  [{'#' * filled}{'.' * (20 - filled)}]{'  OVER BUDGET' if over else ''}"
    lines.append(f"  hours    : {spent:.1f}"
                 + (f" of {budget:g}{bar}" if budget else ""))

    days = hack.days_to_deadline
    if days is not None:
        if days < 0:
            lines.append(f"  deadline : {hack.deadline} — PASSED")
        else:
            lines.append(f"  deadline : {hack.deadline}  ({days:.1f} days left)")
            pace = hack.pace()
            if pace:
                lines.append(f"  pace     : {pace:.1f} h/day to spend the budget in time")
    if hack.running:
        lines.append("  RUNNING — clock is on")
    if hack.notes:
        lines.append(f"  notes    : {hack.notes}")
    return "\n".join(lines)

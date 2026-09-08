"""Classify postings by the house of the chart they belong to, and check
whether that classification predicts anything.

The horary cast 21 Aug 2026 11:59 PDT (ASC 9Sco09, hour ruler Mars = lord of
the ascendant, so radical by Lilly's test) was judged three ways off the same
figure:

    10th house (career-grade employment)  -> NO PERFECTION.
        No aspect, translation or collection joins Mars (L1) to the Sun (L10).
        Lilly: an absence of perfection means the matter does not come about
        of itself.
    6th house (subordinate day-work)      -> perfection by collection.
    2nd house (money)                     -> perfection by collection.

Collection is the detail that matters. It is not "you get there eventually";
it is specifically a third body gathering the light of two significators that
cannot reach each other directly. In plain terms: someone else brings it
about. The one job the querent has ever actually landed came exactly that way
-- daily physical presence at the store plus a worker who lobbied the boss --
and no cold application has ever produced even an interview.

So this module does one narrow thing: it labels a posting SIXTH or TENTH and
says why. It does NOT score, rank, gate, or feed `outcomes`/`learn`. Same line
`consecrations.py` draws: that model's features are frozen checkable facts
about a posting, and a chart's judgement is not one. What a chart judgement
*can* do is be written down in advance and then checked, which is what
`report()` is for. If the split shows no signal after enough applications,
this module has falsified itself and should be deleted.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

SIXTH = "sixth"
TENTH = "tenth"
UNCLEAR = "unclear"

# --- Tenth-house markers: career grade, standing, authority over others. ------
# Roman/arabic level suffixes are matched as standalone tokens so "II" does not
# fire on "Optimization" and "I" does not fire on every capital-I word.
_LEVEL_RE = re.compile(r"\b(?:II|III|IV|V|2|3|4|5)\b")
_SENIOR_RE = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|lead|manager|head|director|architect"
    r"|distinguished|founding)\b",
    re.I,
)
# "3+ years", "at least 4 years", "4-6 years of experience"
_YEARS_RE = re.compile(
    r"\b(?:(\d+)\s*\+|\bat least\s+(\d+)|(\d+)\s*[-–]\s*\d+)\s*(?:years?|yrs?)\b",
    re.I,
)

# --- Sixth-house markers: service, routine, working under someone. -----------
# Title-level only. An earlier version also scanned the body and produced ten
# false positives out of ten: every competitive engineering posting mentions
# "intern programme", "support", or "associate" somewhere in its boilerplate,
# so body-text matching labelled Scale AI and GitLab roles as service work.
# The title is where a job ad states its grade honestly.
_ENTRY_RE = re.compile(
    r"\b(?:entry[- ]level|junior|jr\.?|assistant|apprentice|trainee"
    r"|technician|clerk|operator|coordinator|aide|data\s+entry"
    r"|helpdesk|help\s+desk|intern|internship"
    r"|lab\s+(?:assistant|tech\w*)|research\s+assistant)\b",
    re.I,
)
# Explicit, unambiguous phrases that survive body-text scanning because no
# career-grade posting says them.
_ENTRY_BODY_RE = re.compile(
    r"(?:no\s+(?:prior\s+)?experience\s+(?:required|necessary)|will\s+train"
    r"|new\s+grad(?:uate)?s?\s+(?:welcome|encouraged)"
    r"|0\s*[-–]\s*1\s*years?|entry[- ]level\s+(?:role|position))",
    re.I,
)
_DEGREE_RE = re.compile(
    r"\b(?:bachelor|b\.?s\.?\b|b\.?a\.?\b|master|m\.?s\.?\b|phd|ph\.?d\.?"
    r"|degree\s+required|accredited\s+(?:college|university))\b",
    re.I,
)


@dataclass
class Judgement:
    house: str
    reasons: list[str] = field(default_factory=list)
    years_required: int | None = None
    degree_mentioned: bool = False

    @property
    def perfects(self) -> bool:
        """Whether the chart found a perfection for this house.

        6th perfects by collection; 10th does not perfect at all. `unclear` is
        reported as not perfecting so an ambiguous posting is never counted as
        support for the astrology.
        """
        return self.house == SIXTH


def _max_years(text: str) -> int | None:
    best = None
    for m in _YEARS_RE.finditer(text):
        for g in m.groups():
            if g:
                n = int(g)
                best = n if best is None else max(best, n)
    return best


def classify(title: str, description: str = "") -> Judgement:
    """Label one posting. Title carries more weight than body text: a job ad
    says 'Senior' in the title when it means it, while the body mentions
    seniority for every level on the ladder."""
    title = title or ""
    description = description or ""
    reasons: list[str] = []
    tenth = sixth = 0

    if _SENIOR_RE.search(title):
        tenth += 2
        reasons.append("title carries a seniority word")
    if _LEVEL_RE.search(title):
        tenth += 2
        reasons.append("title carries a level suffix (II/III/…)")
    if _ENTRY_RE.search(title):
        sixth += 3
        reasons.append("title names an entry or service role")

    years = _max_years(description) or _max_years(title)
    if years is not None and years >= 2:
        tenth += 2
        reasons.append(f"asks for {years}+ years of experience")
    # A low stated minimum is NOT sixth-house evidence: competitive research
    # roles routinely say "1+ years" and still screen for far more. It only
    # withholds a tenth-house point.

    if _ENTRY_BODY_RE.search(description):
        sixth += 2
        reasons.append("body states no-experience / will-train / new-grad explicitly")

    degree = bool(_DEGREE_RE.search(description))

    # An untitled, unlevelled "Software Engineer" at a competitive company is
    # career-grade by default -- that is what the market means by the bare
    # title. Only positive 6th-house evidence pulls it down.
    if sixth == 0 and tenth == 0:
        if re.search(r"\bengineer\b|\bscientist\b|\bdeveloper\b", title, re.I):
            tenth += 1
            reasons.append("bare engineering title reads as career-grade by default")

    if tenth > sixth:
        house = TENTH
    elif sixth > tenth:
        house = SIXTH
    else:
        house = UNCLEAR
    return Judgement(house=house, reasons=reasons, years_required=years,
                     degree_mentioned=degree)


# ---------------------------------------------------------------------------
# Backtest: does the classification predict anything?
# ---------------------------------------------------------------------------

# Outcomes that mean a human moved the application forward. `ack` is an
# automated receipt and is explicitly NOT advancement -- counting it would
# make any classifier look good.
_ADVANCED = {"interview", "screen", "phone_screen", "offer", "assessment"}


def report(db_path: Path | str, verbose: bool = False) -> dict:
    """Cross the house classification against real outcomes.

    Returns counts, not a verdict. With a handful of applications this proves
    nothing and says so; the point is that it will keep saying so until it
    either accumulates evidence or doesn't.
    """
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT a.external_id, a.title, a.company, a.applied_at,
               COALESCE(j.description, '') AS description
        FROM applications a
        LEFT JOIN jobs j ON j.external_id = a.external_id
        """
    ).fetchall()

    outcomes: dict[str, set[str]] = {}
    for r in con.execute("SELECT external_id, outcome FROM outcomes"):
        outcomes.setdefault(r[0], set()).add((r[1] or "").lower())

    tally = {SIXTH: {"n": 0, "advanced": 0, "rejected": 0},
             TENTH: {"n": 0, "advanced": 0, "rejected": 0},
             UNCLEAR: {"n": 0, "advanced": 0, "rejected": 0}}
    detail = []
    for r in rows:
        j = classify(r["title"], r["description"])
        got = outcomes.get(r["external_id"], set())
        bucket = tally[j.house]
        bucket["n"] += 1
        if got & _ADVANCED:
            bucket["advanced"] += 1
        if "rejected" in got:
            bucket["rejected"] += 1
        detail.append((r["applied_at"][:10], j.house, r["company"], r["title"],
                       sorted(got) or ["-"]))

    con.close()

    total = sum(b["n"] for b in tally.values())
    print(f"{total} applications classified against the 21 Aug 2026 chart\n")
    print(f"{'house':10}{'applied':>9}{'advanced':>10}{'rejected':>10}   chart says")
    for house, label in ((SIXTH, "perfects (collection)"),
                         (TENTH, "NO perfection"),
                         (UNCLEAR, "not counted")):
        b = tally[house]
        print(f"{house:10}{b['n']:>9}{b['advanced']:>10}{b['rejected']:>10}   {label}")

    advanced_total = sum(b["advanced"] for b in tally.values())
    print()
    if advanced_total == 0:
        print("No application has advanced past an automated acknowledgment yet,")
        print("so this table cannot yet distinguish a real signal from an empty one.")
    if tally[SIXTH]["n"] == 0:
        print("Nothing has been applied to in the 6th house -- the one house the")
        print("chart said perfects. The prediction is untested, not disconfirmed.")

    if verbose:
        print("\n" + "-" * 78)
        for d, house, co, ti, got in sorted(detail):
            print(f"{d}  {house:8} {str(co)[:18]:18} {str(ti)[:38]:38} {','.join(got)}")

    return {"tally": tally, "total": total}


# ---------------------------------------------------------------------------
# Company scale. NOT astrology -- an empirical proxy, kept here because it is
# the same question ("which door") the house classification asks.
# ---------------------------------------------------------------------------
#
# No board exposes headcount, so this counts how many roles the company has
# open on its own ATS. A firm with two postings is small; one with a thousand
# is not. It is a proxy and it is wrong at the edges (a 40-person company
# hiring hard looks mid-sized), but it needs no new data source and it sorts
# openai/1031 from a two-posting shop correctly, which is the distinction that
# matters here: at a small company a founder reads the GitHub link.

SMALL = "small"
MID = "mid"
LARGE = "large"


def company_scale(open_postings: int) -> str:
    if open_postings <= 10:
        return SMALL
    if open_postings <= 60:
        return MID
    return LARGE


def posting_counts(conn) -> dict[str, int]:
    """{company: open postings} from the local jobs table."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT company, COUNT(*) FROM jobs GROUP BY company")}


def channel_split(rows, counts: dict[str, int]) -> dict:
    """Summarise a candidate batch by house and company scale.

    `rows` are sqlite Rows (or any mapping) with title/company/description.
    """
    by_house: dict[str, int] = {SIXTH: 0, TENTH: 0, UNCLEAR: 0}
    by_scale: dict[str, int] = {SMALL: 0, MID: 0, LARGE: 0}
    for row in rows:
        j = classify(row["title"], (row["description"] if "description" in row.keys()
                                    else "") if hasattr(row, "keys") else "")
        by_house[j.house] += 1
        by_scale[company_scale(counts.get(row["company"], 0))] += 1
    return {"house": by_house, "scale": by_scale}


def format_split(split: dict) -> list[str]:
    """Lines for printing before a batch goes out."""
    h, s = split["house"], split["scale"]
    total = sum(h.values()) or 1
    return [
        "Targeting check (21 Aug 2026 chart: 6th perfects by collection, 10th does not)",
        f"  house:  6th {h[SIXTH]:>3}   10th {h[TENTH]:>3}   unclear {h[UNCLEAR]:>3}"
        f"    ({100 * h[SIXTH] // total}% in the house that perfects)",
        f"  scale:  small {s[SMALL]:>3}   mid {s[MID]:>3}   large {s[LARGE]:>3}"
        "    (small = <=10 open postings)",
    ]


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    report(here / "data" / "jobbot.db", verbose="-v" in sys.argv)

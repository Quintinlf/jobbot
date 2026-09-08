"""Small companies, found by asking Y Combinator who its companies are.

WHY THIS EXISTS

`data/companies.txt` is 92 hand-written slugs and every one of them is a
name-brand employer: OpenAI, Stripe, Anthropic, Databricks, Figma. Those are
exactly the boards with the worst applicant-to-opening ratio, and the ones
most likely to filter on a completed degree before a human reads anything. A
twenty-person company reads applications; a company with four thousand
engineers runs a screen.

Nothing about the fetching needed to change. `boards.py` already reads
Greenhouse, Lever and Ashby, which is where small venture-backed companies
post. What was missing was a list of who those companies ARE -- the seed list
was typed by hand, so it contained only companies someone had already heard of.

THE SOURCE

Y Combinator publishes its own directory, unauthenticated and paginated:

    GET https://api.ycombinator.com/v0.1/companies?page=1

6,200 companies at the time of writing, each with `slug`, `website`,
`teamSize`, `batch`, `status` and `locations`. This is YC's own endpoint --
the one their site calls -- not a scrape of the rendered page and not a
third-party mirror, so the provenance of every company here is checkable.

Measured 2026-09-07: 4,295 are Active, and 3,493 of those have between 2 and
50 people.

WHAT THIS IS NOT

Indeed and LinkedIn are still out, for the reason `boards_local.py` gives:
both block automated access, so a fetcher would be both fragile and against
the terms. Work at a Startup (YC's own job board) needs a login, which puts it
in the same category -- the honest answer there is to search it by hand.

HIT RATE, MEASURED

A YC slug is not an ATS slug, so this probes variants. On a random 60-company
sample of the 2-50 population:

    raw YC slug                     8/60   (13%)
    + hyphens removed, domain root  12/60  (20%)

Ashby accounted for 9 of those 12 boards, which is why `ATS_ORDER` puts it
first -- a miss costs one request per system, so on this population the order
is most of the run time.

20% of 3,493 is roughly 700 boards, against the 92 that existed before. The
rest genuinely have no public board: a three-person company often posts a job
in its own README, and there is nothing here to read.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Iterable, Iterator
from urllib.parse import urlparse

from jobbot import boards, config

logger = logging.getLogger(__name__)

YC_API = "https://api.ycombinator.com/v0.1/companies"

# Ashby first: 9 of the 12 boards found in the sample were Ashby, and a miss
# costs one request per system tried before the hit.
ATS_ORDER = ("ashby", "greenhouse", "lever")

# Team-size window. The lower bound drops solo founders, who have no board and
# no headcount to hire into. The upper bound is where "they will read your
# application" stops being true -- past about fifty people there is a recruiter
# and a screen, which is the population companies.txt already covers.
MIN_TEAM = 2
MAX_TEAM = 50

# Domain roots that say nothing about the company: a site hosted on someone
# else's platform would otherwise contribute "github" or "notion" as a slug
# and probe the same three wrong boards once per company.
GENERIC_HOSTS = {"github", "notion", "webflow", "vercel", "netlify", "carrd", "framer"}

# Suffixes companies bolt on to a name for uniqueness on YC but drop when
# registering with an ATS. Only these are stripped -- taking off any trailing
# word would turn "scale-computing" into "scale" and find a different company.
SLUG_SUFFIXES = {"ai", "app", "hq", "io", "labs", "inc", "co", "health", "care", "tech"}


def _probe_cache_path():
    """Resolved on call, not at import: the tests point DATA_DIR at a tmpdir."""
    return config.DATA_DIR / "startup_probes.json"


# -- The directory -------------------------------------------------------------

def fetch_yc_directory(
    on_page: Callable[[int, int, int], None] | None = None,
    pause: float = 0.2,
) -> list[dict]:
    """Every company in YC's public directory, following `nextPage`.

    Paginates on the link the API itself returns rather than on a page count,
    so a directory that grows between the first and last request is followed
    correctly instead of being truncated at a stale total.
    """
    out: list[dict] = []
    url = f"{YC_API}?page=1"
    page = 0
    while url:
        response = boards.SESSION.get(url, timeout=30)
        response.raise_for_status()
        payload = response.json()
        out.extend(payload.get("companies") or [])
        page += 1
        if on_page:
            on_page(page, payload.get("totalPages") or 0, len(out))
        url = payload.get("nextPage")
        if url:
            time.sleep(pause)
    return out


def small_companies(
    companies: Iterable[dict],
    min_team: int = MIN_TEAM,
    max_team: int = MAX_TEAM,
) -> list[dict]:
    """Active companies inside the team-size window, biggest first.

    Biggest first because team size is the best available proxy for whether
    anyone is hiring: a fifty-person company with a board almost certainly has
    something open, a three-person one often has a board with nothing on it.
    Running the probe in that order means an interrupted run has still found
    the boards most likely to carry postings.
    """
    kept = [
        c for c in companies
        if c.get("status") == "Active"
        and isinstance(c.get("teamSize"), int)
        and min_team <= c["teamSize"] <= max_team
    ]
    return sorted(kept, key=lambda c: -c["teamSize"])


# -- Slugs ---------------------------------------------------------------------

def candidate_slugs(company: dict) -> list[str]:
    """Slugs worth probing for one company, best guess first.

    A YC slug is not an ATS slug. "finny-ai" is `finny` on Ashby,
    "empirical-health" is `empirical`, "imt-care" is `imt` on Greenhouse --
    companies register the ATS under the bare name and YC under a
    disambiguated one. Four of the twelve boards in the sample were found this
    way and would have been missed by the YC slug alone.
    """
    out: list[str] = []

    def add(value: str | None) -> None:
        value = (value or "").strip().lower()
        if value and value not in out:
            out.append(value)

    slug = (company.get("slug") or "").strip().lower()
    add(slug)
    if "-" in slug:
        add(slug.replace("-", ""))
        head, _, tail = slug.rpartition("-")
        if head and tail in SLUG_SUFFIXES:
            add(head)

    host = (urlparse(company.get("website") or "").hostname or "").lower()
    host = re.sub(r"^www\.", "", host)
    root = host.split(".")[0] if host else ""
    if root and root not in GENERIC_HOSTS:
        add(root)

    return out


# -- Probing -------------------------------------------------------------------

# A board with far more open roles than the company has employees is not that
# company's board. Slugs are not unique across ATS systems, and auto-discovery
# probes generic words a hand-written list never would: "agency", "mesh",
# "flint", "latent", "agave". Measured 2026-09-07 -- YC's "Agency" (50 people)
# resolved to a Greenhouse board with 829 open postings, which is a staffing
# firm of the same name. Every other board found in that run was inside this
# bound, so it costs nothing real and catches the one that mattered.
#
# Deliberately generous: a fast-growing 50-person company advertising 35 roles
# is exactly the company worth applying to, so the rule only fires on counts
# that no company of that size could have.
COLLISION_FLOOR = 25


def implausible_board(count: int, team_size: int | None) -> bool:
    """Whether a board is too big to belong to a company this small."""
    if not count:
        return False
    ceiling = max(COLLISION_FLOOR, 2 * (team_size or 0))
    return count > ceiling


def load_probe_cache() -> dict:
    """Slugs already probed, and what they resolved to (null = nothing).

    Without this a second run repeats every request the first one made, and a
    full pass over the small-company population is tens of thousands of
    requests. Misses are cached as deliberately as hits -- the point is not to
    ask the same question twice.
    """
    path = _probe_cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_probe_cache(cache: dict) -> None:
    config.ensure_dirs()
    _probe_cache_path().write_text(
        json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8"
    )


def discover_boards(
    companies: Iterable[dict],
    known: dict | None = None,
    cache: dict | None = None,
    on_result: Callable[[dict, str | None, str | None, int], None] | None = None,
    pause: float = 0.1,
) -> Iterator[tuple[str, str, int]]:
    """Probe each company's candidate slugs; yield (slug, ats, posting count).

    Yields rather than returns so the caller can save as it goes. A full pass
    is long enough that an interrupted run must not lose what it found.
    """
    known = known or {}
    if cache is None:
        cache = {}

    for company in companies:
        found: tuple[str, str, int] | None = None
        for slug in candidate_slugs(company):
            if slug in known:
                # Already in companies.txt -- nothing to discover, and no
                # reason to spend a request confirming it.
                break
            if slug in cache:
                if cache[slug] is None:
                    continue
                found = (slug, cache[slug], 0)
                break
            result = boards.discover(slug, order=ATS_ORDER)
            time.sleep(pause)
            if result and implausible_board(len(result[1]), company.get("teamSize")):
                # Cache it as a miss: the board is real, it just is not this
                # company's, and re-probing it next run would find the same
                # wrong answer at the same cost.
                logger.info(
                    "%s: %s/%s has %d postings for a %s-person company — "
                    "treating as a slug collision",
                    company.get("name"), result[0], slug, len(result[1]),
                    company.get("teamSize"),
                )
                cache[slug] = None
                continue
            cache[slug] = result[0] if result else None
            if result:
                found = (slug, result[0], len(result[1]))
                break

        if on_result:
            on_result(
                company,
                found[0] if found else None,
                found[1] if found else None,
                found[2] if found else 0,
            )
        if found:
            yield found

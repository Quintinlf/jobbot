"""Applicant-tracking-system board fetchers.

Greenhouse, Lever, and Ashby all publish a documented public JSON endpoint for
each company's job board — the same data that renders on the company careers
page. We read those directly rather than scraping aggregator sites, which is
both more reliable and stays inside what the endpoints are published for.

A company is identified only by its board slug. Which ATS it uses is
discovered by probing, so adding a company means appending one line to
data/companies.txt — no per-company code.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Callable, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from jobbot import config

logger = logging.getLogger(__name__)


# ── HTML → text ────────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Minimal HTML stripper for job description bodies."""

    _BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK:
            self.parts.append("\n")

    def text(self) -> str:
        joined = "".join(self.parts)
        lines = [ln.strip() for ln in joined.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    # Greenhouse returns entity-encoded markup ("&lt;p&gt;Build things&lt;/p&gt;").
    # HTMLParser decodes charrefs inside *data*, so without unescaping first the
    # tags come back as literal "<p>" text in the output and every downstream
    # regex has to read around them.
    if "&lt;" in html:
        html = unescape(html)
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # malformed markup — return what we got
        pass
    return parser.text()


# ── Posting record ─────────────────────────────────────────────────────────────

@dataclass
class Posting:
    """One job posting, normalized across every ATS."""

    external_id: str
    title: str
    company: str
    location: str
    url: str
    description: str
    ats: str
    board_slug: str
    department: str = ""
    posted_at: str = ""
    # Posted pay band, when the board publishes one. The floor is the useful
    # half: it says which rung the requisition was written for, which the
    # title often does not.
    salary_min: int | None = None
    salary_max: int | None = None
    salary_summary: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    def as_row(self) -> dict:
        return {
            "external_id": self.external_id,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "url": self.url,
            "description": self.description,
            "ats": self.ats,
            "board_slug": self.board_slug,
            "department": self.department,
            "posted_at": self.posted_at,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_summary": self.salary_summary,
        }


# ── HTTP session ───────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=config.MAX_RETRIES,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


SESSION = _session()


def _get_json(url: str) -> dict | list | None:
    time.sleep(config.REQUEST_DELAY)
    try:
        resp = SESSION.get(url, timeout=config.REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, json.JSONDecodeError) as exc:
        logger.debug("fetch failed %s: %s", url, exc)
        return None


# ── Greenhouse ─────────────────────────────────────────────────────────────────

def fetch_greenhouse(slug: str) -> list[Posting]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = _get_json(url)
    if not isinstance(data, dict) or "jobs" not in data:
        return []

    postings = []
    for job in data.get("jobs") or []:
        offices = job.get("offices") or []
        location = (job.get("location") or {}).get("name") or ", ".join(
            o.get("name", "") for o in offices
        )
        departments = job.get("departments") or []
        postings.append(
            Posting(
                external_id=f"greenhouse:{slug}:{job.get('id')}",
                title=(job.get("title") or "").strip(),
                company=slug,
                location=(location or "").strip(),
                url=job.get("absolute_url") or "",
                description=html_to_text(job.get("content")),
                ats="greenhouse",
                board_slug=slug,
                department=departments[0].get("name", "") if departments else "",
                posted_at=job.get("updated_at") or job.get("first_published") or "",
                raw=job,
            )
        )
    return postings


# ── Lever ──────────────────────────────────────────────────────────────────────

def fetch_lever(slug: str) -> list[Posting]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = _get_json(url)
    if not isinstance(data, list):
        return []

    postings = []
    for job in data:
        categories = job.get("categories") or {}
        body = job.get("descriptionPlain") or html_to_text(job.get("description"))
        # Lever splits requirements into "lists"; fold them into the body so
        # gate detection sees the qualification bullets.
        for block in job.get("lists") or []:
            body += "\n" + (block.get("text") or "") + "\n"
            body += html_to_text(block.get("content"))

        created = job.get("createdAt")
        posted = ""
        if isinstance(created, (int, float)):
            posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()

        postings.append(
            Posting(
                external_id=f"lever:{slug}:{job.get('id')}",
                title=(job.get("text") or "").strip(),
                company=slug,
                location=(categories.get("location") or "").strip(),
                url=job.get("hostedUrl") or job.get("applyUrl") or "",
                description=body,
                ats="lever",
                board_slug=slug,
                department=categories.get("team") or categories.get("department") or "",
                posted_at=posted,
                raw=job,
            )
        )
    return postings


# ── Ashby ──────────────────────────────────────────────────────────────────────

def _ashby_salary(job: dict) -> tuple[int | None, int | None, str]:
    """The annual salary band from an Ashby posting, if it publishes one.

    `includeCompensation=true` has always been on the request URL and the
    answer was always thrown away. Equity components are skipped: a percentage
    is not a number this can compare against a salary floor.
    """
    comp = job.get("compensation") or {}
    for component in comp.get("summaryComponents") or []:
        if component.get("compensationType") != "Salary":
            continue
        if component.get("interval") not in ("1 YEAR", "YEAR", None):
            continue
        lo, hi = component.get("minValue"), component.get("maxValue")
        summary = (comp.get("scrapeableCompensationSalarySummary")
                   or comp.get("compensationTierSummary") or "")
        return (
            int(lo) if isinstance(lo, (int, float)) else None,
            int(hi) if isinstance(hi, (int, float)) else None,
            summary,
        )
    return None, None, ""


def fetch_ashby(slug: str) -> list[Posting]:
    url = (
        "https://api.ashbyhq.com/posting-api/job-board/"
        f"{slug}?includeCompensation=true"
    )
    data = _get_json(url)
    if not isinstance(data, dict) or "jobs" not in data:
        return []

    postings = []
    for job in data.get("jobs") or []:
        lo, hi, summary = _ashby_salary(job)
        postings.append(
            Posting(
                salary_min=lo,
                salary_max=hi,
                salary_summary=summary,
                external_id=f"ashby:{slug}:{job.get('id')}",
                title=(job.get("title") or "").strip(),
                company=slug,
                location=(job.get("location") or "").strip(),
                url=job.get("jobUrl") or job.get("applyUrl") or "",
                description=job.get("descriptionPlain")
                or html_to_text(job.get("descriptionHtml")),
                ats="ashby",
                board_slug=slug,
                department=job.get("department") or job.get("team") or "",
                posted_at=job.get("publishedAt") or "",
                raw=job,
            )
        )
    return postings


FETCHERS: dict[str, Callable[[str], list[Posting]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


# ── Discovery ──────────────────────────────────────────────────────────────────

def discover(
    slug: str, order: Iterable[str] | None = None
) -> tuple[str, list[Posting]] | None:
    """Probe each ATS for this slug and return the first that responds.

    Slugs are not globally unique across systems, but a collision would still
    return real postings, so first-match is safe.

    `order` puts the likeliest ATS first. A miss costs one request per system,
    so when a whole population leans one way the order is most of the cost:
    small YC companies are overwhelmingly on Ashby (9 of 12 boards found in a
    60-company sample), where the default order tries it last.
    """
    names = list(order) if order else list(FETCHERS)
    for ats in names:
        fetcher = FETCHERS.get(ats)
        if fetcher is None:
            continue
        try:
            postings = fetcher(slug)
        except Exception as exc:
            logger.debug("%s/%s raised: %s", ats, slug, exc)
            continue
        if postings:
            return ats, postings
    return None


def load_companies(path=None) -> list[str]:
    """Read board slugs from companies.txt, one per line. '#' comments allowed."""
    path = path or config.COMPANIES_PATH
    if not path.exists():
        return []
    slugs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            slugs.append(line.lower())
    # preserve order, drop duplicates
    return list(dict.fromkeys(slugs))


def load_verified() -> dict[str, str]:
    """Return {slug: ats} for slugs already confirmed to resolve."""
    if not config.VERIFIED_BOARDS_PATH.exists():
        return {}
    try:
        return json.loads(config.VERIFIED_BOARDS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_verified(mapping: dict[str, str]) -> None:
    config.ensure_dirs()
    config.VERIFIED_BOARDS_PATH.write_text(
        json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8"
    )


def verify_all(slugs: Iterable[str], on_result: Callable[[str, str | None, int], None] | None = None) -> dict[str, str]:
    """Probe every slug, returning {slug: ats} for the ones that resolve.

    Slugs that resolve to nothing are dropped rather than retried on every
    run — the seed list is a starting guess, and this prunes it to reality.
    """
    verified: dict[str, str] = {}
    for slug in slugs:
        result = discover(slug)
        if result:
            ats, postings = result
            verified[slug] = ats
            if on_result:
                on_result(slug, ats, len(postings))
        elif on_result:
            on_result(slug, None, 0)
    return verified


def fetch_all(
    verified: dict[str, str],
    on_board: Callable[[str, str, int], None] | None = None,
) -> list[Posting]:
    """Fetch every verified board using its known ATS."""
    out: list[Posting] = []
    for slug, ats in verified.items():
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            continue
        try:
            postings = fetcher(slug)
        except Exception as exc:
            logger.warning("fetch failed for %s (%s): %s", slug, ats, exc)
            postings = []
        out.extend(postings)
        if on_board:
            on_board(slug, ats, len(postings))
    return out

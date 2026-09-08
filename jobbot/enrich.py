"""Recover job description text for postings that arrive without one.

The Braven board lists a title, an employer, and a link. gating.py needs the
posting's actual text — without it every row classifies as OPEN with no
evidence, which is not "no eligibility gate", it is "we never looked". That is
exactly the failure mode the legacy CSV import has, and it is worth fixing here
because this board is 55% internships, where enrollment gates are densest.

So each posting is read from its own page. Where the link lands on an ATS that
publishes JSON we already speak, we use it. Otherwise we take the HTML and
strip it, which works on plain server-rendered career pages and returns little
on JavaScript-rendered ones. A posting we cannot read keeps its board metadata
and is reported as unenriched rather than silently treated as ungated.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from jobbot import config
from jobbot.boards import Posting, html_to_text
from jobbot.gating import GONE_MARKER, UNVERIFIED_MARKER

logger = logging.getLogger(__name__)


# Sites whose postings we deliberately do not fetch. Both put job listings
# behind bot detection, and reading around that is the thing this project has
# refused to do from the start — see the note in the README about
# undetected_chromedriver. A posting here keeps its board metadata.
BLOCKED_HOSTS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
)

# Below this, whatever came back is navigation chrome and a cookie banner
# rather than a posting, and feeding it to the classifier would produce
# confident nonsense.
MIN_DESCRIPTION_CHARS = 400

# Career pages carry entire site menus. Cap what we store.
MAX_DESCRIPTION_CHARS = 30_000


class PostingGone(Exception):
    """The posting's page answered 404/410 — the listing has been taken down."""


@dataclass
class EnrichResult:
    enriched: int = 0
    blocked: int = 0
    failed: int = 0
    too_thin: int = 0
    gone: int = 0
    hosts_failed: dict[str, int] = field(default_factory=dict)

    @property
    def attempted(self) -> int:
        return self.enriched + self.blocked + self.failed + self.too_thin + self.gone

    def summary(self) -> str:
        return (
            f"descriptions: {self.enriched} recovered · {self.too_thin} too thin · "
            f"{self.failed} unreachable · {self.gone} taken down · "
            f"{self.blocked} not fetched by policy"
        )


# ── ATS-aware readers ──────────────────────────────────────────────────────────

_GREENHOUSE_RE = re.compile(
    r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?P<slug>[^/]+)/jobs/(?P<job>\d+)"
)
_LEVER_RE = re.compile(r"jobs\.lever\.co/(?P<slug>[^/]+)/(?P<job>[0-9a-f-]{16,})")
_ASHBY_RE = re.compile(r"jobs\.ashbyhq\.com/(?P<slug>[^/]+)/(?P<job>[0-9a-f-]{16,})")


def _greenhouse(session: requests.Session, match: re.Match) -> str:
    url = (
        "https://boards-api.greenhouse.io/v1/boards/"
        f"{match['slug']}/jobs/{match['job']}"
    )
    data = _json(session, url)
    if not isinstance(data, dict):
        return ""
    return html_to_text(data.get("content"))


def _lever(session: requests.Session, match: re.Match) -> str:
    url = f"https://api.lever.co/v0/postings/{match['slug']}/{match['job']}"
    data = _json(session, url)
    if not isinstance(data, dict):
        return ""
    body = data.get("descriptionPlain") or html_to_text(data.get("description"))
    for block in data.get("lists") or []:
        body += "\n" + (block.get("text") or "") + "\n" + html_to_text(block.get("content"))
    return body


def _ashby(session: requests.Session, match: re.Match) -> str:
    url = (
        "https://api.ashbyhq.com/posting-api/job-board/"
        f"{match['slug']}?includeCompensation=true"
    )
    data = _json(session, url)
    if not isinstance(data, dict):
        return ""
    for job in data.get("jobs") or []:
        if job.get("id") == match["job"]:
            return job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml"))
    return ""


_ATS_READERS = (
    (_GREENHOUSE_RE, _greenhouse),
    (_LEVER_RE, _lever),
    (_ASHBY_RE, _ashby),
)


def _json(session: requests.Session, url: str):
    try:
        resp = session.get(url, timeout=config.REQUEST_TIMEOUT)
        if resp.status_code in _GONE_STATUSES:
            raise PostingGone(url)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.debug("json fetch failed %s: %s", url, exc)
        return None


# ── Generic HTML ───────────────────────────────────────────────────────────────

# Whole elements whose text is never part of a posting. Dropped before the
# stripper runs, so a site-wide nav does not end up in the classifier's input.
_STRIP_ELEMENTS = re.compile(
    r"<(script|style|noscript|svg|nav|header|footer)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


# A posting that answers 404/410 is gone, not merely unreadable. Every other
# status stays "we could not read it" — a 403 or a bot-check 202 says nothing
# about whether the job is still open.
_GONE_STATUSES = frozenset({404, 410})

# Most boards do not answer 404 for a closed posting. They serve 200 and a
# page that says so — CircleCI renders "Posting expired" above a list of other
# roles. Status codes alone therefore miss the common case, which is how an
# expired job sat at the top of the queue with a 97-point score.
#
# Kept deliberately tight. A false positive here retires a live posting, so
# these match the sentence a closed listing states outright, not any page that
# happens to mention applications.
CLOSED_PAGE_PATTERNS = [
    r"posting (?:has )?expired",
    r"no longer accepting applications",
    r"we are no longer accepting",
    r"this (?:job|position|posting|role|opening) is no longer (?:available|open|active|posted)",
    r"this (?:job|position|posting|role|opening) (?:has been|is) (?:closed|filled|removed)",
    r"(?:job|position) (?:has been|is now) filled",
    r"this (?:job|posting) (?:is|has been) (?:no longer )?closed",
    r"the (?:job|position|posting) you(?:'re| are) looking for (?:is no longer|no longer|has been)",
    r"job (?:posting )?not found",
    r"this opportunity is no longer",
]

_CLOSED = [re.compile(p, re.IGNORECASE) for p in CLOSED_PAGE_PATTERNS]


def looks_closed(text: str) -> str | None:
    """The sentence saying this posting is closed, if the page states one."""
    for pattern in _CLOSED:
        match = pattern.search(text or "")
        if match:
            snippet = re.sub(r"\s+", " ", text[max(0, match.start() - 40):match.end() + 60])
            return snippet.strip()
    return None


def _page_text(session: requests.Session, url: str) -> str:
    resp = session.get(
        url,
        timeout=config.REQUEST_TIMEOUT,
        headers={"Accept": "text/html,application/xhtml+xml"},
        allow_redirects=True,
    )
    if resp.status_code in _GONE_STATUSES:
        raise PostingGone(url)
    resp.raise_for_status()
    if "html" not in (resp.headers.get("Content-Type") or "").lower():
        return ""
    text = html_to_text(_STRIP_ELEMENTS.sub(" ", resp.text))
    if looks_closed(text):
        raise PostingGone(url)
    return text


# ── Driver ─────────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def is_blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_HOSTS)


def describe(url: str, session: requests.Session | None = None) -> str:
    """Best-effort job description text for one posting URL.

    Returns "" when the page cannot be read, is not HTML, or is behind bot
    detection we will not work around. Raises PostingGone if the page reports
    that the listing no longer exists.
    """
    if not url or is_blocked(url):
        return ""

    session = session or _session()

    for pattern, reader in _ATS_READERS:
        if match := pattern.search(url):
            if text := reader(session, match):
                return text
            break  # a known ATS that came back empty won't do better as HTML

    try:
        return _page_text(session, url)
    except requests.RequestException as exc:
        logger.debug("page fetch failed %s: %s", url, exc)
        return ""


def attach_descriptions(
    postings: list[Posting],
    on_result=None,
) -> EnrichResult:
    """Fetch each posting's page and append the real description in place.

    The board metadata already in `description` is kept and the fetched text is
    appended under it, so the queue always shows the deadline and referral
    status even when the employer's page could not be read.
    """
    result = EnrichResult()
    session = _session()

    for posting in postings:
        host = (urlparse(posting.url).hostname or "").lower()

        if is_blocked(posting.url):
            result.blocked += 1
            status = "blocked"
        else:
            time.sleep(config.REQUEST_DELAY)
            try:
                text = describe(posting.url, session)
            except PostingGone:
                result.gone += 1
                status = "gone"
            else:
                if not text:
                    result.failed += 1
                    result.hosts_failed[host] = result.hosts_failed.get(host, 0) + 1
                    status = "unreachable"
                elif len(text) < MIN_DESCRIPTION_CHARS:
                    result.too_thin += 1
                    result.hosts_failed[host] = result.hosts_failed.get(host, 0) + 1
                    status = "too thin"
                else:
                    posting.description = (
                        f"{posting.description}\n\n{text[:MAX_DESCRIPTION_CHARS]}"
                    )
                    result.enriched += 1
                    status = "ok"

        # Mark what we failed to read, so the classifier reports "not checked"
        # instead of "no gate found" and scoring withholds the OPEN bonus. The
        # markers are stored with the description, so a later `rescore` — which
        # never touches the network — still knows what happened here.
        if status == "gone":
            posting.description = f"{posting.description}\n\n{GONE_MARKER}"
        elif status != "ok":
            posting.description = f"{posting.description}\n\n{UNVERIFIED_MARKER}"

        if on_result:
            on_result(posting, status)

    return result

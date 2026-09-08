"""Local, non-tech-startup boards: universities, hospitals, county systems.

`boards.py` covers Greenhouse/Lever/Ashby, which is where venture-backed tech
companies post. That population is almost entirely career-grade engineering --
of 17,435 postings fetched that way, 343 (2%) classify sixth-house under
`horary.classify`, and nearly all of those are executive assistants and
marketing interns. The roles actually being looked for here -- lab tech, data
tech, research assistant, analyst, anything Python-adjacent at $22/hr within
about ten miles of Los Angeles -- are posted on Workday by universities,
hospitals, and public agencies, which that pipeline never touches.

Workday exposes the same documented CXS JSON endpoint every Workday careers
page calls to render itself:

    POST https://<tenant>.<host>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
    {"limit": 20, "offset": 0, "searchText": "python"}

Verified working against USC (26 hits for "python" at time of writing).

WHAT IS NOT HERE, AND WHY:

  Indeed -- no. The Publisher API was retired and the site actively blocks
  automated access. A fetcher would break and would be scraping against the
  terms; the honest answer is to search it by hand.

  GovernmentJobs / NeoGov (LA County, LA City) -- not yet. Probed for a JSON
  endpoint and found none; both documented guesses returned 404. It would need
  HTML parsing against an unverified structure, which is exactly the kind of
  thing that silently returns zero rows forever. Worth doing, but only after
  looking at the real markup.

  UCLA, Caltech, Cedars-Sinai -- their Workday tenant/site slugs could not be
  guessed (every candidate returned HTTP 422). They are not absent because
  they lack a Workday board; they are absent because the slug has to be read
  off the real careers URL. Use `parse_careers_url()` for that -- it is the
  same discovery-not-hardcoding approach `boards.discover()` takes for ATS.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from jobbot.boards import SESSION, Posting, html_to_text

logger = logging.getLogger(__name__)

WORKDAY_ATS = "workday"

# (tenant, host, site). Only entries actually confirmed to return HTTP 200.
# Add to this by running `parse_careers_url()` on a real careers page URL and
# checking it with `probe()`.
VERIFIED_WORKDAY: list[tuple[str, str, str]] = [
    ("usc", "wd5", "ExternalUSCCareers"),
]

# Searched separately so a board that buries Python roles under a generic title
# still surfaces them. Empty string = fetch everything the board has.
DEFAULT_QUERIES = ("python", "data", "research assistant", "laboratory", "analyst")

_CAREERS_RE = re.compile(
    r"^https?://(?P<tenant>[^.]+)\.(?P<host>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/?#]+)",
    re.I,
)


def parse_careers_url(url: str) -> tuple[str, str, str] | None:
    """Pull (tenant, host, site) out of a Workday careers page URL.

    Open the institution's careers page, copy the address bar, pass it here.
    A URL like

        https://ucla.wd1.myworkdayjobs.com/en-US/UCLA_Careers

    yields ("ucla", "wd1", "UCLA_Careers"). Returns None if the URL is not a
    Workday careers page, so a wrong paste fails loudly instead of being
    silently added as a board that always returns nothing.
    """
    m = _CAREERS_RE.match((url or "").strip())
    if not m:
        return None
    return m.group("tenant").lower(), m.group("host").lower(), m.group("site")


def _endpoint(tenant: str, host: str, site: str) -> str:
    return f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


def probe(tenant: str, host: str, site: str) -> int | None:
    """Total postings on this board, or None if the slug combination is wrong.

    Workday answers a bad tenant/site with HTTP 422 rather than 404, which is
    easy to mistake for a malformed request. Treat any non-200 as "not this
    board" and move on.
    """
    try:
        r = SESSION.post(
            _endpoint(tenant, host, site),
            json={"limit": 1, "offset": 0, "searchText": ""},
            timeout=15,
        )
    except Exception as exc:
        logger.warning("workday probe failed for %s/%s: %s", tenant, site, exc)
        return None
    if r.status_code != 200:
        return None
    try:
        return int(r.json().get("total", 0))
    except Exception:
        return None


def fetch_workday(
    tenant: str,
    host: str,
    site: str,
    query: str = "",
    limit: int = 20,
    max_pages: int = 10,
) -> list[Posting]:
    """One Workday board, one search term.

    The list endpoint returns titles and locations but not descriptions --
    Workday keeps the body on a per-posting endpoint. Rather than issue a
    second request per row (hundreds of calls per board), the description is
    left empty here and `enrich.py` can fill it on demand for rows that
    survive scoring. `horary.classify` reads the title first for exactly this
    reason, so an empty description does not break the targeting check.
    """
    out: list[Posting] = []
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    for page in range(max_pages):
        try:
            r = SESSION.post(
                _endpoint(tenant, host, site),
                json={"limit": limit, "offset": page * limit, "searchText": query},
                timeout=20,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            logger.warning("workday fetch %s/%s q=%r page=%d: %s",
                           tenant, site, query, page, exc)
            break

        postings = payload.get("jobPostings") or []
        if not postings:
            break

        for p in postings:
            path = p.get("externalPath") or ""
            req = (p.get("bulletFields") or [None])[0] or path.rsplit("_", 1)[-1]
            out.append(
                Posting(
                    external_id=f"{WORKDAY_ATS}:{tenant}:{req}",
                    title=(p.get("title") or "").strip(),
                    company=tenant,
                    location=(p.get("locationsText") or "").strip(),
                    url=f"{base}/{site}{path}" if path else base,
                    description="",
                    ats=WORKDAY_ATS,
                    board_slug=f"{tenant}/{site}",
                )
            )
        if len(postings) < limit:
            break

    # One posting can match several queries; the external_id is the ATS
    # requisition number, so de-duplicating on it is safe.
    seen: dict[str, Posting] = {}
    for p in out:
        seen.setdefault(p.external_id, p)
    return list(seen.values())


def fetch_all_workday(
    boards: list[tuple[str, str, str]] | None = None,
    queries: tuple[str, ...] = DEFAULT_QUERIES,
) -> list[Posting]:
    """Every verified Workday board, across every search term."""
    boards = boards if boards is not None else VERIFIED_WORKDAY
    found: dict[str, Posting] = {}
    for tenant, host, site in boards:
        for q in queries:
            for p in fetch_workday(tenant, host, site, query=q):
                found.setdefault(p.external_id, p)
        logger.info("workday %s/%s: %d postings", tenant, site, len(found))
    return list(found.values())


# ── NeoGov / GovernmentJobs (LA County, LA City, most CA public agencies) ─────
#
# The public careers page is a JavaScript shell with no postings in its HTML.
# The fragment its own front end requests does contain them:
#
#     GET https://www.governmentjobs.com/careers/home/index?agency=<slug>&page=N
#     X-Requested-With: XMLHttpRequest
#
# 20 postings per page, each as an anchor of the form
# /careers/<agency>/jobs/<id>/<title-slug>. The slug carries the title, so a
# listing needs no second request; `enrich.py` can pull the body later for rows
# that survive scoring.
#
# This is HTML scraping against a structure nobody documents, so it is written
# to fail loudly: `fetch_neogov` raises if a page it believes has postings
# yields none, rather than quietly returning an empty list forever.

NEOGOV_ATS = "neogov"
NEOGOV_AGENCIES = ("lacounty", "lacity", "culvercity", "longbeach", "pasadena")

_NEOGOV_JOB_RE = re.compile(
    r'href="(/careers/(?P<agency>[a-z0-9-]+)/jobs/(?P<id>\d+)/(?P<slug>[^"?#]+))"'
)


def _title_from_slug(slug: str) -> str:
    words = slug.replace("-", " ").split()
    # Roman-numeral levels and short acronyms should not be title-cased to "Ii".
    return " ".join(
        w.upper() if re.fullmatch(r"(?:i{1,3}|iv|v|vi{0,3}|ix|x|nc|it|hr)", w)
        else w.capitalize()
        for w in words
    )


def fetch_neogov(agency: str = "lacounty", max_pages: int = 12) -> list[Posting]:
    seen: dict[str, Posting] = {}
    for page in range(1, max_pages + 1):
        url = ("https://www.governmentjobs.com/careers/home/index"
               f"?agency={agency}&page={page}")
        try:
            r = SESSION.get(url, timeout=20,
                            headers={"X-Requested-With": "XMLHttpRequest"})
            r.raise_for_status()
        except Exception as exc:
            logger.warning("neogov %s page %d: %s", agency, page, exc)
            break

        matches = list(_NEOGOV_JOB_RE.finditer(r.text))
        if not matches:
            if page == 1:
                raise RuntimeError(
                    f"neogov/{agency}: page 1 returned {len(r.text)} bytes with no "
                    "job anchors — the page structure has changed and this "
                    "fetcher needs re-checking, not silently ignoring"
                )
            break

        before = len(seen)
        for m in matches:
            jid = m.group("id")
            seen.setdefault(
                jid,
                Posting(
                    external_id=f"{NEOGOV_ATS}:{agency}:{jid}",
                    title=_title_from_slug(m.group("slug")),
                    company=agency,
                    location="Los Angeles County, CA",
                    url="https://www.governmentjobs.com" + m.group(1),
                    description="",
                    ats=NEOGOV_ATS,
                    board_slug=agency,
                ),
            )
        if len(seen) == before:  # pagination looped back on itself
            break
    return list(seen.values())


def fetch_all_neogov(agencies: tuple[str, ...] = NEOGOV_AGENCIES) -> list[Posting]:
    out: list[Posting] = []
    for agency in agencies:
        try:
            got = fetch_neogov(agency)
        except Exception as exc:
            logger.warning("neogov %s skipped: %s", agency, exc)
            continue
        logger.info("neogov %s: %d postings", agency, len(got))
        out.extend(got)
    return out


# -- Radancy / Jibe front ends (UCLA) ------------------------------------------
#
# UCLA is not on Workday, which is why every tenant/site slug guessed for it
# returned HTTP 422: the ATS underneath is iCIMS, and jobs.ucla.edu is a
# Radancy front end sitting in front of it. iCIMS has no public board API, but
# the front end has one, and it is the endpoint the page itself calls to
# render:
#
#     GET https://jobs.ucla.edu/api/jobs?page=1&limit=100
#
# It answers with the whole posting inline -- title, description,
# qualifications, city, apply URL, posted date, salary -- so unlike the NeoGov
# fetcher nothing has to be parsed out of markup, and unlike Workday no second
# request per posting is needed. Verified 2026-09-07: 95 postings, all fields
# present.
#
# Kept general on purpose. This front end is used by other large employers, so
# a second campus needs a RADANCY_BOARDS entry rather than a new fetcher --
# but each one has to be verified by hand before being added, the same rule
# `boards.discover()` follows for ATS slugs.

RADANCY_ATS = "radancy"

RADANCY_BOARDS: list[tuple[str, str]] = [
    ("ucla", "https://jobs.ucla.edu"),
]


def fetch_radancy(slug: str, base: str, limit: int = 100,
                  max_pages: int = 10) -> list[Posting]:
    """One Radancy-fronted board, paginated until it stops returning rows."""
    out: list[Posting] = []
    for page in range(1, max_pages + 1):
        url = f"{base}/api/jobs?page={page}&limit={limit}"
        try:
            r = SESSION.get(url, timeout=25,
                            headers={"Accept": "application/json"})
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            logger.warning("radancy %s page %d: %s", slug, page, exc)
            break

        rows = payload.get("jobs") or []
        if not rows:
            if page == 1:
                raise RuntimeError(
                    f"radancy/{slug}: page 1 returned no jobs — the endpoint "
                    "has changed and needs re-checking, not silently ignoring"
                )
            break

        for row in rows:
            job = row.get("data") or {}
            req = str(job.get("req_id") or job.get("slug") or "").strip()
            if not req:
                continue
            # Both halves matter to the gate classifier: "description" carries
            # the role, "qualifications" carries the degree language.
            body = html_to_text(
                (job.get("description") or "")
                + "\n"
                + (job.get("qualifications") or "")
            )
            out.append(
                Posting(
                    external_id=f"{RADANCY_ATS}:{slug}:{req}",
                    title=(job.get("title") or "").strip(),
                    company=slug,
                    location=(job.get("full_location")
                              or job.get("short_location") or "").strip(),
                    url=job.get("apply_url") or f"{base}/jobs/{req}",
                    description=body,
                    ats=RADANCY_ATS,
                    board_slug=slug,
                    department=(job.get("department") or "").strip(),
                    posted_at=job.get("posted_date") or job.get("create_date") or "",
                    raw=job,
                )
            )

        if len(rows) < limit:
            break
    return out


def fetch_all_radancy(
    board_list: list[tuple[str, str]] | None = None,
) -> list[Posting]:
    out: list[Posting] = []
    for slug, base in (board_list if board_list is not None else RADANCY_BOARDS):
        try:
            got = fetch_radancy(slug, base)
        except Exception as exc:
            logger.warning("radancy %s skipped: %s", slug, exc)
            continue
        logger.info("radancy %s: %d postings", slug, len(got))
        out.extend(got)
    return out


def fetch_all_local() -> list[Posting]:
    """Everything in this module: Workday, public agencies, Radancy boards."""
    return fetch_all_workday() + fetch_all_neogov() + fetch_all_radancy()


if __name__ == "__main__":  # quick manual check
    import sys

    if len(sys.argv) > 1:
        parsed = parse_careers_url(sys.argv[1])
        if not parsed:
            print("not a Workday careers URL")
            raise SystemExit(1)
        tenant, host, site = parsed
        total = probe(tenant, host, site)
        print(f"{tenant}.{host} / {site} -> "
              + (f"{total} postings" if total is not None else "no board there"))
        if total:
            print(f'  add ("{tenant}", "{host}", "{site}") to VERIFIED_WORKDAY')
    else:
        for p in fetch_all_workday():
            print(f"  {p.company:10} {p.title[:60]:60} {p.location[:28]}")


def describe_workday(url: str) -> str:
    """Fetch one Workday posting's body.

    The list endpoint omits descriptions, so `fetch_workday` leaves them empty
    and this fills them in for rows worth the extra request. The CXS detail
    endpoint mirrors the listing URL: the same /job/<location>/<slug>_<req>
    path, under /wday/cxs/<tenant>/<site> instead of the public site root.
    """
    m = re.match(
        r"^https://(?P<tenant>[^.]+)\.(?P<host>wd\d+)\.myworkdayjobs\.com/"
        r"(?P<site>[^/]+)(?P<path>/job/.+)$",
        url or "",
    )
    if not m:
        return ""
    cxs = (f"https://{m.group('tenant')}.{m.group('host')}.myworkdayjobs.com"
           f"/wday/cxs/{m.group('tenant')}/{m.group('site')}{m.group('path')}")
    try:
        r = SESSION.get(cxs, timeout=20, headers={"Accept": "application/json"})
        r.raise_for_status()
        return html_to_text(r.json().get("jobPostingInfo", {}).get("jobDescription", ""))
    except Exception as exc:
        logger.warning("workday describe %s: %s", url[:70], exc)
        return ""

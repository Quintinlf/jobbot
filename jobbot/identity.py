"""Canonical identity for a job posting across discovery sources.

The same role can arrive from a Greenhouse board, a Jobot alert, and a
LinkedIn email. `external_id` is unique per source, so matching has to look at
the apply URL and a normalized company/title/location fingerprint instead.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

# Tracking params that make two URLs look different when they are not.
_DROP_QUERY = re.compile(
    r"^(utm_|hsa_|mc_|fbclid|gclid|yclid|msclkid|igshid|ref$|ref_|source$|"
    r"trk|tracking|ehid|jid$|jaid|jobot)",
    re.IGNORECASE,
)

_PUNCT = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")

_GREENHOUSE_JOB = re.compile(
    r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/[^/]+/jobs/(\d+)",
    re.IGNORECASE,
)
_LEVER_JOB = re.compile(
    r"jobs\.lever\.co/[^/]+/([0-9a-f-]{16,})",
    re.IGNORECASE,
)
_ASHBY_JOB = re.compile(
    r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{16,})",
    re.IGNORECASE,
)
_INDEED_JK = re.compile(r"[?&]jk=([a-z0-9]+)", re.IGNORECASE)
_LINKEDIN_JOB = re.compile(r"linkedin\.com/jobs/view/(\d+)", re.IGNORECASE)

ATS_HOST_HINTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "icims.com",
    "greenhouse.com",
)


def _parse(url: str):
    """urlparse that cannot take the run down.

    Marketing mail contains URLs that are not URLs. One message carrying a
    stray "[" in a link raises ValueError("Invalid IPv6 URL") deep inside
    urlsplit, and because this is called while classifying every message, one
    malformed link in one email aborted the whole inbox run before a single
    job was ingested. A link we cannot parse is simply not a link we can match.
    """
    try:
        return urlparse(url or "")
    except ValueError:
        return None


def canonical_url(url: str) -> str:
    """Strip tracking junk so the same apply link matches across sources."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = _parse(raw)
    if parsed is None:
        return ""
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/"
    kept = []
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        if _DROP_QUERY.match(key):
            continue
        for value in values:
            kept.append(f"{key}={value}")
    query = "&".join(sorted(kept))
    return urlunparse((scheme, host, path, "", query, ""))


def ats_key(url: str) -> str:
    """Stable ATS job id when the URL is one we recognise, else empty."""
    raw = url or ""
    if match := _GREENHOUSE_JOB.search(raw):
        return f"greenhouse:{match.group(1)}"
    if match := _LEVER_JOB.search(raw):
        return f"lever:{match.group(1)}"
    if match := _ASHBY_JOB.search(raw):
        return f"ashby:{match.group(1)}"
    if match := _INDEED_JK.search(raw):
        return f"indeed:{match.group(1)}"
    if match := _LINKEDIN_JOB.search(raw):
        return f"linkedin:{match.group(1)}"
    return ""


def is_ats_url(url: str) -> bool:
    parsed = _parse(url)
    host = ((parsed.hostname if parsed else "") or "").lower()
    return any(host == hint or host.endswith("." + hint) for hint in ATS_HOST_HINTS)


def prefer_url(current: str, incoming: str) -> str:
    """Keep an ATS apply URL over a tracking/aggregator link."""
    if is_ats_url(incoming) and not is_ats_url(current):
        return incoming
    if incoming and not current:
        return incoming
    return current or incoming


def fingerprint(company: str, title: str, location: str = "") -> str:
    """Normalized company|title|location. Empty company or title → no match."""
    company_n = _norm(company)
    title_n = _norm(title)
    if not company_n or not title_n:
        return ""
    return f"{company_n}|{title_n}|{_norm(location)}"


def _norm(value: str) -> str:
    text = _WS.sub(" ", (value or "").lower()).strip()
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()

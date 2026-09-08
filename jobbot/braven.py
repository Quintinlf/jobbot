"""The Braven Opportunity Board.

Braven staff curate roles into an Airtable base and publish it as a shared
interface page. It is not an ATS: there is no per-posting API, no job
description, and the grid is drawn to a canvas, so there is no DOM to read
either. What there *is* is the interface's own data call, which returns the
whole board — schema and rows — in one JSON response.

Three details make that call work, and all three come from the share page HTML
rather than being guessable:

  * `accessPolicy` — a signed grant naming the share, with an expiry
  * `pageLoadId`  — issued per page load
  * `csrfToken`   — issued per session

Sending the request without the page-load id and CSRF token returns 401, even
with a valid access policy, and even when replaying a request the browser just
made successfully. So every fetch loads the share page first and reads the
three values out of it. That is also why this cannot be a static URL in
companies.txt — hence its own module.

What the board does *not* carry is a job description, which is the text
gating.py needs to do the only job that matters here. Descriptions are
recovered separately, from each posting's own page, by enrich.py.
"""

from __future__ import annotations

import json
import logging
import random
import re
import string
from dataclasses import dataclass
from typing import Any

import requests

from jobbot import config
from jobbot.boards import Posting

logger = logging.getLogger(__name__)


# ── Share page bootstrap ───────────────────────────────────────────────────────

_SHARE_URL_RE = re.compile(r"airtable\.com/(app\w+)/(shr\w+)")


@dataclass
class _Bootstrap:
    """Everything the data call needs, read out of the share page."""

    app_id: str
    share_id: str
    page_id: str
    access_policy: dict
    page_load_id: str
    csrf_token: str


def _quoted_json(html: str, key: str) -> Any:
    """Pull `"key":"{\\"json\\": ...}"` out of the page and parse it twice.

    Airtable embeds the access policy as JSON *inside* a JSON string, so the
    captured text has to be unescaped as a string literal before it parses as
    an object.
    """
    match = re.search(rf'"{key}":"((?:[^"\\]|\\.)*)"', html)
    if not match:
        raise ValueError(f"{key} not found in share page")
    return json.loads(json.loads(f'"{match.group(1)}"'))


def _plain(html: str, key: str) -> str:
    match = re.search(rf'"{key}":"([^"]+)"', html)
    if not match:
        raise ValueError(f"{key} not found in share page")
    return match.group(1)


def _request_id() -> str:
    """Airtable expects `req` followed by 14 alphanumerics."""
    alphabet = string.ascii_letters + string.digits
    return "req" + "".join(random.choice(alphabet) for _ in range(14))


def _bootstrap(session: requests.Session, share_url: str) -> _Bootstrap:
    match = _SHARE_URL_RE.search(share_url)
    if not match:
        raise ValueError(
            f"Not an Airtable share URL: {share_url!r}. Expected something like "
            "https://airtable.com/appXXXXXXXX/shrXXXXXXXX"
        )
    app_id, share_id = match.groups()

    resp = session.get(share_url, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    html = resp.text

    return _Bootstrap(
        app_id=app_id,
        share_id=share_id,
        page_id=_plain(html, "sharedPageId"),
        access_policy=_quoted_json(html, "accessPolicy"),
        page_load_id=_plain(html, "pageLoadId"),
        csrf_token=_plain(html, "csrfToken"),
    )


def _read_board(session: requests.Session, boot: _Bootstrap) -> dict:
    params = {
        "includeDataForPageId": boot.page_id,
        "shouldIncludeSchemaChecksum": True,
        "expectedPageLayoutSchemaVersion": config.BRAVEN_LAYOUT_VERSION,
        "shouldPreloadQueries": True,
        "shouldPreloadAllPossibleContainerElementQueries": True,
        "urlSearch": "",
        "includePageLayoutTypeInfo": True,
        "includeDataForExpandedRowPageFromQueryContainer": True,
        "includeDataForAllReferencedExpandedRowPagesInLayout": True,
        "navigationMode": "view",
        # The interface asks for msgpack; JSON is the same payload and saves us
        # carrying a msgpack decoder for one caller.
        "allowMsgpackOfResultIfEnabled": False,
    }
    resp = session.get(
        f"https://airtable.com/v0.3/application/{boot.app_id}/readForSharedPages",
        params={
            "stringifiedObjectParams": json.dumps(params, separators=(",", ":")),
            "requestId": _request_id(),
            "accessPolicy": json.dumps(boot.access_policy, separators=(",", ":")),
        },
        headers={
            "Accept": "application/json, text/plain, */*",
            "X-Airtable-Application-Id": boot.app_id,
            "X-Airtable-Inter-Service-Client": "webClient",
            "X-Airtable-Page-Load-Id": boot.page_load_id,
            "X-Requested-With": "XMLHttpRequest",
            "X-Time-Zone": "America/Los_Angeles",
            "X-User-Locale": "en",
            "X-CSRF-Token": boot.csrf_token,
            "Referer": f"https://airtable.com/{boot.app_id}/{boot.share_id}",
        },
        timeout=config.REQUEST_TIMEOUT * 3,
    )
    if resp.status_code == 401:
        raise RuntimeError(
            "Airtable refused the board request (401). The share link has "
            "probably been rotated or its access policy expired — open "
            f"{config.BRAVEN_BOARD_URL} in a browser and check it still loads, "
            "then update BRAVEN_BOARD_URL in config.py if Braven changed it."
        )
    resp.raise_for_status()
    return resp.json()["data"]


# ── Schema handling ────────────────────────────────────────────────────────────

# Board column names → what we do with them. Matched case-insensitively against
# the live schema rather than by field id, because field ids are private to
# Braven's base and a restructure would silently produce empty postings.
_WANTED = {
    "position": "position",
    "employer (linked)": "employer",
    "employer": "employer",
    "region": "region",
    "location(s)": "locations",
    "type of position": "kind",
    "application deadline": "deadline",
    "working model": "working_model",
    "career community": "community",
    "apply directly": "apply_url",
    "referral position": "referral",
    "date added": "added",
}


def _choice_names(table: dict) -> dict[str, str]:
    """Map every select-option id in the table to its display name."""
    names: dict[str, str] = {}
    for column in table.get("columns") or []:
        choices = (column.get("typeOptions") or {}).get("choices") or {}
        for choice_id, choice in choices.items():
            if isinstance(choice, dict) and choice.get("name"):
                names[choice_id] = choice["name"]
    return names


def _pick_table(schemas: list[dict]) -> dict | None:
    """The postings table is the one with a Position column."""
    for table in schemas:
        for column in table.get("columns") or []:
            if (column.get("name") or "").strip().lower() == "position":
                return table
    return None


def _render(value: Any, choices: dict[str, str]) -> str:
    """Flatten one Airtable cell to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return choices.get(value, value)
    if isinstance(value, bool):
        return "Yes" if value else ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        # Button columns carry {"label": ..., "url": ...}.
        return value.get("url") or value.get("foreignRowDisplayName") or value.get("label") or ""
    if isinstance(value, list):
        parts = [_render(item, choices) for item in value]
        return ", ".join(p for p in parts if p)
    return str(value)


# ── Board metadata → description text ──────────────────────────────────────────

# The board has no job description, so the only text a posting starts with is
# its own board metadata. Two of those fields are things the existing
# classifiers already know how to read, provided they are phrased the way a
# posting would phrase them — so restate them in prose rather than inventing a
# format nothing parses. This is the board's own answer, reworded, not a guess.
_WORKING_MODEL_PROSE = {
    "remote": "This role is remote.",
    "hybrid": "This is a hybrid role.",
    "in person": "This is an in-person role.",
    "in-person": "This is an in-person role.",
    "onsite": "This is an in-person role.",
}


def _deadline(raw: str) -> str:
    """Airtable dates arrive as full ISO timestamps; the day is the useful part."""
    return (raw or "").split("T")[0]


def board_summary(fields: dict[str, str]) -> str:
    """The board's own facts about a posting, as text.

    Kept separate from any fetched description and labelled, so nothing here
    can be mistaken for something the employer wrote.
    """
    lines = ["[Braven Opportunity Board]"]
    for label, key in (
        ("Employer", "employer"),
        ("Type", "kind"),
        ("Region", "region"),
        ("Location(s)", "locations"),
        ("Working model", "working_model"),
        ("Career community", "community"),
    ):
        if fields.get(key):
            lines.append(f"{label}: {fields[key]}")

    if deadline := _deadline(fields.get("deadline", "")):
        lines.append(f"Application deadline: {deadline}")

    # "Yes https://link.braven.org/ReferralRequest" — a Braven staff referral is
    # available. Worth carrying through to the queue; it is the one thing this
    # board offers that a public ATS feed cannot.
    referral = fields.get("referral", "")
    if referral.lower().startswith("yes"):
        lines.append(f"Braven referral available: {referral[3:].strip()}")

    prose = _WORKING_MODEL_PROSE.get(fields.get("working_model", "").strip().lower())
    if prose:
        lines.append(prose)

    return "\n".join(lines)


def has_referral(description: str) -> bool:
    return "Braven referral available" in (description or "")


# ── Fetch ──────────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    return session


def fetch_board(share_url: str | None = None) -> list[Posting]:
    """Fetch every posting on the Braven board.

    Descriptions are the board metadata only — run enrich.attach_descriptions()
    to fill in the real posting text before scoring.
    """
    share_url = share_url or config.BRAVEN_BOARD_URL
    session = _session()
    boot = _bootstrap(session, share_url)
    data = _read_board(session, boot)

    table = _pick_table(data.get("tableSchemas") or [])
    if table is None:
        raise RuntimeError(
            "No table with a 'Position' column on the Braven board — the base "
            "has been restructured. Check the column names in braven._WANTED."
        )

    choices = _choice_names(table)
    by_id = {
        column["id"]: _WANTED[(column.get("name") or "").strip().lower()]
        for column in table.get("columns") or []
        if (column.get("name") or "").strip().lower() in _WANTED
    }

    rows = (
        data.get("preloadPageQueryResults", {})
        .get("tableDataById", {})
        .get(table["id"], {})
        .get("partialRowById", {})
    )

    postings: list[Posting] = []
    for record_id, row in rows.items():
        cells = row.get("cellValuesByColumnId") or {}
        fields = {
            key: _render(cells.get(column_id), choices)
            for column_id, key in by_id.items()
        }

        title = fields.get("position", "").strip()
        url = fields.get("apply_url", "").strip()
        if not title or not url:
            # A row with no posting link cannot be applied to.
            continue

        location = fields.get("locations", "").strip() or fields.get("region", "").strip()

        postings.append(
            Posting(
                external_id=f"braven:{record_id}",
                title=title,
                company=fields.get("employer", "").strip() or "unknown employer",
                location=location,
                url=url,
                description=board_summary(fields),
                ats="braven",
                board_slug="braven",
                department=fields.get("community", "").strip(),
                posted_at=fields.get("added", "").strip(),
                raw=fields,
            )
        )

    logger.info("braven board: %d postings", len(postings))
    return postings

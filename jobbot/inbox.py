"""Read-only Gmail ingest for job-alert emails.

Jobot / LinkedIn / Indeed alerts (and similar) become `boards.Posting` rows
and go through the same gate/score path as board jobs (`store.upsert_job`).

Westbury-style career-service pitches are classified `agency_pitch` and never
become application candidates. This module does not send mail.
"""

from __future__ import annotations

import email as email_lib
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import requests

from jobbot import config, enrich, pipeline, store
from jobbot.boards import Posting, html_to_text
from jobbot.identity import ats_key, canonical_url, is_ats_url

logger = logging.getLogger(__name__)

HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRACKING_HOSTS = (
    "jobot.com",
    "e.jobot.com",
    "lnkd.in",
    "indeedemail.com",
    "click.indeed.com",
)

AGENCY_PATTERNS = [
    r"reply.{0,40}learn more",
    r"we take care of your job applications",
    r"taking care of your job applications",
    r"guarantee(?: a)? suitable career",
    r"career coaching",
    r"career consulting",
    r"application service",
    r"we represent and support you throughout your job search",
    r"senior hr (?:management|career manager)",
    r"paid (?:career )?program",
    r"schedule a google meet consultation",
]
_AGENCY = [re.compile(p, re.IGNORECASE) for p in AGENCY_PATTERNS]

NEWSLETTER_PATTERNS = [
    r"unsubscribe",
    r"view (?:this|it) in (?:your )?browser",
    r"weekly roundup",
]
_NEWSLETTER = [re.compile(p, re.IGNORECASE) for p in NEWSLETTER_PATTERNS]


@dataclass
class MailItem:
    message_id: str
    sender: str
    subject: str
    text: str
    html: str
    received: str
    urls: list[str] = field(default_factory=list)


@dataclass
class Classified:
    kind: str
    reason: str
    source: str = ""


@dataclass
class JobDraft:
    title: str
    company: str
    location: str
    url: str
    description: str
    source: str
    message_id: str
    received: str = ""


@dataclass
class InboxResult:
    fetched: int = 0
    job_alerts: int = 0
    ingested_new: int = 0
    duplicates: int = 0
    skipped_nonjobs: int = 0
    parse_failed: int = 0
    gated: int = 0
    reachable: int = 0
    skipped_kinds: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"fetched {self.fetched} · jobs {self.job_alerts} · "
            f"new {self.ingested_new} · duplicates {self.duplicates} · "
            f"non-jobs {self.skipped_nonjobs} · unparsed {self.parse_failed} · "
            f"reachable {self.reachable} · gated {self.gated}"
        )


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_parts(msg) -> tuple[str, str]:
    text = html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain" and not text:
                text = decoded
            elif ctype == "text/html" and not html:
                html = decoded
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        decoded = payload.decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            html = decoded
        else:
            text = decoded
    return text, html


def parse_rfc822(raw: bytes | str) -> MailItem:
    blob = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    msg = email_lib.message_from_bytes(blob)
    text, html = _body_parts(msg)
    mid = (msg.get("Message-ID") or "").strip() or hashlib.sha1(
        (msg.get("Subject") or "").encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    received = ""
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        received = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        received = ""
    return MailItem(
        message_id=mid.strip("<>"),
        sender=_decode_header(msg.get("From")),
        subject=_decode_header(msg.get("Subject")),
        text=text,
        html=html,
        received=received,
        urls=extract_urls(html, text),
    )


def extract_urls(*blobs: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for blob in blobs:
        if not blob:
            continue
        for match in HREF_RE.finditer(blob):
            url = unescape(unquote(match.group(1))).strip()
            if url.startswith("http") and url not in seen:
                seen.add(url)
                found.append(url)
        stripped = html_to_text(blob) if "<" in blob else blob
        for match in URL_RE.finditer(stripped):
            url = match.group(0).rstrip(").,;]")
            if url not in seen:
                seen.add(url)
                found.append(url)
    return found


def classify(item: MailItem) -> Classified:
    hay = f"{item.sender}\n{item.subject}\n{item.text}\n{html_to_text(item.html)}"
    sender = item.sender.lower()
    subject = item.subject.lower()

    for pattern in _AGENCY:
        if pattern.search(hay):
            return Classified("agency_pitch", pattern.pattern)

    source = ""
    if "jobot" in sender or "jobot" in subject:
        source = "jobot"
    elif "linkedin" in sender or "linkedin" in subject:
        source = "linkedin"
    elif "indeed" in sender or "indeed" in subject:
        source = "indeed"
    elif "wellfound" in sender or "angel.co" in sender or "wellfound" in subject:
        source = "wellfound"

    if source:
        return Classified("job_alert", f"sender/subject:{source}", source)

    applyish = any(
        ats_key(url) or is_ats_url(url) or "viewjob" in url or "/jobs/" in url
        for url in item.urls
    )
    if applyish and not any(p.search(hay) for p in _NEWSLETTER):
        return Classified("job_alert", "apply-url-in-body", "email")

    if any(p.search(hay) for p in _NEWSLETTER) and not applyish:
        return Classified("newsletter", "newsletter-markers")
    return Classified("unknown", "no-job-signal")


def _plain(item: MailItem) -> str:
    return (item.text or html_to_text(item.html) or "").strip()


def _field(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def pick_apply_url(urls: list[str]) -> str:
    scored: list[tuple[int, str]] = []
    for url in urls:
        lowered = url.lower()
        if any(skip in lowered for skip in ("unsubscribe", "privacy", "mailto:")):
            continue
        score = 0
        if ats_key(url) or is_ats_url(url):
            score += 50
        host = (urlparse(url).hostname or "").lower()
        if "jobot.com" in host:
            score += 8
        if "linkedin.com/jobs" in lowered or "linkedin.com/comm/jobs" in lowered:
            score += 20
        if "indeed.com/viewjob" in lowered or "indeed.com/rc/clk" in lowered:
            score += 20
        if "apply" in lowered:
            score += 5
        if score:
            scored.append((score, url))
    scored.sort(reverse=True)
    return scored[0][1] if scored else (urls[0] if urls else "")


def unwrap_url(url: str, session: requests.Session | None = None) -> str:
    """Follow one redirect off a tracker. Never used to bypass bot checks."""
    if not url:
        return ""
    linkedin = re.search(
        r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", url, re.IGNORECASE
    )
    if linkedin:
        return f"https://www.linkedin.com/jobs/view/{linkedin.group(1)}"
    if ats_key(url) or is_ats_url(url):
        return url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("url", "u", "dest", "redirect", "goto"):
        if qs.get(key):
            inner = unquote(qs[key][0])
            if inner.startswith("http"):
                return inner
    host = (parsed.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in TRACKING_HOSTS):
        sess = session or requests.Session()
        try:
            resp = sess.head(url, allow_redirects=True, timeout=config.REQUEST_TIMEOUT)
            final = str(resp.url or url)
            if final.startswith("http"):
                return final
        except requests.RequestException:
            try:
                resp = sess.get(
                    url,
                    allow_redirects=True,
                    timeout=config.REQUEST_TIMEOUT,
                    stream=True,
                )
                final = str(resp.url or url)
                resp.close()
                if final.startswith("http"):
                    return final
            except requests.RequestException:
                return url
    return url


def parse_job(item: MailItem, source: str, unwrap=unwrap_url) -> JobDraft | None:
    text = _plain(item)
    url = unwrap(pick_apply_url(item.urls)) if item.urls else ""
    title = company = location = ""

    if source == "jobot":
        title = _field(r"(?:role|position|job title)\s*[:\-]\s*(.+)", text)
        title = title or _jobot_title(item.subject)
        company = _field(r"company\s*[:\-]\s*(.+)", text) or _jobot_company(item.subject)
        location = _field(r"location\s*[:\-]\s*(.+)", text)
    elif source == "linkedin":
        at = re.search(r"(.+?)\s+at\s+(.+)", item.subject, re.IGNORECASE)
        if at and "job alert" not in item.subject.lower():
            title, company = at.group(1).strip(), at.group(2).strip()
        title = title or _field(r"(?:new job|job)\s*[:\-]\s*(.+)", text)
        company = company or _field(r"(?:company|hiring team)\s*[:\-]\s*(.+)", text)
        location = _field(r"location\s*[:\-]\s*(.+)", text)
    elif source == "indeed":
        at = re.search(r"(.+?)\s+(?:-|–|at)\s+(.+)", item.subject)
        if at and "job alert" not in item.subject.lower():
            title, company = at.group(1).strip(), at.group(2).strip()
        title = title or _field(r"job title\s*[:\-]\s*(.+)", text)
        company = company or _field(r"company\s*[:\-]\s*(.+)", text)
        location = _field(r"location\s*[:\-]\s*(.+)", text)
    else:
        at = re.search(r"(.+?)\s+at\s+(.+)", item.subject, re.IGNORECASE)
        if at:
            title, company = at.group(1).strip(), at.group(2).strip()
        title = title or _field(r"(?:role|position|title)\s*[:\-]\s*(.+)", text)
        company = company or _field(r"company\s*[:\-]\s*(.+)", text)
        location = _field(r"location\s*[:\-]\s*(.+)", text)

    title = _clean_line(title)
    company = _clean_line(company)
    location = _clean_line(location)
    if not title or not url:
        return None
    if not company:
        company = "unknown"
    return JobDraft(
        title=title[:200],
        company=company[:120],
        location=location[:120],
        url=url,
        description=text[:30000],
        source=source or "email",
        message_id=item.message_id,
        received=item.received,
    )


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).split("\n")[0][:200]


# ── Wellfound: one email, several jobs ──────────────────────────────────────────
# Wellfound's alert is a digest of 5-10 listings in one email, not one job per
# message the way Jobot/LinkedIn/Indeed send them. `parse_job` returns at most
# one JobDraft, so a digest needed its own parser rather than a bigger regex
# bolted onto that one. Each listing repeats the same shape:
#
#   <Title>
#
#   <Company> / <N-M Employees>
#
#    $Xk-Yk | <location> | <N years of exp> | <employment type>
#
#   [Actively Hiring ...tags...]
#   [Our Take  <paragraph>]
#
#   Learn More
#   <https://wellfound.com/jobs?job_listing_slug=NNNNNNN-title-slug>
#
# The plain-text body carries the real wellfound.com URL directly — unlike the
# HTML anchors, which route through a links.wellfound.com click-tracker with no
# listing id in it. Reading the URL out of the text avoids following that
# tracker at all.
_WELLFOUND_LISTING = re.compile(
    r"(?P<title>[^\n]{3,140})\n+"
    r"(?P<company>[^\n/]{1,90}?)\s*/\s*[\d,]+\+?(?:\s*-\s*[\d,]+)?\s*Employees[^\n]*\n+"
    r"(?P<compline>[^\n]{0,220})\n+"
    # "Our Take" paragraphs push the URL a dozen lines further down than the
    # plain listings, so this stays generous.
    r"(?:[^\n]*\n+){0,24}?"
    # Wellfound renders "Learn More" two different ways in the same digest —
    # sometimes "Learn More\n<url>" on its own line, sometimes
    # "Learn More <url>" inline with no line break. Requiring a newline before
    # the URL (as the first version did) matches the two-line style fine but
    # fails on the inline one; failing silently swallowed that listing's own
    # URL into the interior wildcard and let the match run on to grab the
    # NEXT listing's URL instead — the Campbell's Foundation listing came back
    # tagged with Trimble's URL. \s* covers both spacings without caring
    # which one a given listing uses.
    r"Learn More\s*<?"
    r"(?P<url>https?://wellfound\.com/jobs\?job_listing_slug=\d+-[a-z0-9-]+)>?",
    re.IGNORECASE,
)


def _wellfound_location(compline: str) -> str:
    parts = [p.strip() for p in (compline or "").split("|")]
    return parts[1] if len(parts) > 1 else ""


def parse_wellfound_digest(item: MailItem) -> list[JobDraft]:
    """Every listing in one Wellfound alert email, as its own draft."""
    text = _plain(item)
    drafts: list[JobDraft] = []
    seen: set[str] = set()

    for match in _WELLFOUND_LISTING.finditer(text):
        title = _clean_line(match.group("title"))
        company = _clean_line(match.group("company"))
        compline = (match.group("compline") or "").strip()
        location = _clean_line(_wellfound_location(compline))
        url = match.group("url").strip()

        if not title or not company or url in seen:
            continue
        # Reject matches where the "title" line is actually a stray fragment
        # of surrounding boilerplate ("Learn More", a filter tab) rather than
        # a job title — those never have a company right after them in real
        # digests, but a loose match on a short ambiguous line could still
        # happen on a reformatted email.
        if re.search(r"^(?:learn more|actively hiring|our take)$", title, re.IGNORECASE):
            continue
        seen.add(url)

        drafts.append(JobDraft(
            title=title[:200],
            company=company[:120],
            location=location[:120],
            url=url,
            description=f"{compline}\n\n(from a Wellfound job alert digest, "
                        f"not the posting itself)"[:30000],
            source="wellfound",
            message_id=item.message_id,
            received=item.received,
        ))
    return drafts


def _jobot_title(subject: str) -> str:
    text = re.sub(r"^jobot\s*[:\-]\s*", "", subject or "", flags=re.IGNORECASE)
    text = re.sub(r"^new (?:role|job)\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
    at = re.search(r"(.+?)\s+at\s+(.+)", text, re.IGNORECASE)
    return (at.group(1) if at else text).strip()


def _jobot_company(subject: str) -> str:
    at = re.search(r"\sat\s+(.+)$", subject or "", re.IGNORECASE)
    return at.group(1).strip() if at else ""


def draft_to_posting(draft: JobDraft) -> Posting:
    key = ats_key(draft.url) or canonical_url(draft.url) or draft.message_id
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return Posting(
        external_id=f"email:{draft.source}:{digest}",
        title=draft.title,
        company=draft.company,
        location=draft.location,
        url=draft.url,
        description=draft.description,
        ats="email",
        board_slug=draft.source,
        posted_at=draft.received,
    )


def enrich_draft(draft: JobDraft) -> JobDraft:
    if not draft.url or enrich.is_blocked(draft.url):
        return draft
    try:
        text = enrich.describe(draft.url)
    except enrich.PostingGone:
        return draft
    if text and len(text) > 200:
        draft.description = f"{draft.description}\n\n{text[:30000]}".strip()
    return draft


def ingest_items(
    items: list[MailItem],
    conn=None,
    unwrap=unwrap_url,
    enrich_descriptions: bool = True,
) -> InboxResult:
    """Classify and ingest already-parsed emails. Tests call this with fixtures."""
    result = InboxResult(fetched=len(items))
    postings: list[Posting] = []

    from jobbot import replies as replies_mod

    for item in items:
        # A reply from an employer is not a job to apply to. ATS confirmations
        # carry an apply-style link, so `classify` reads them as job alerts and
        # ingests them as postings — Thumbtack's "Application received -
        # Software Engineer, AI/ML Infrastructure\n (US-Based) at Thumbtack"
        # became a new posting titled "(US-Based)", which then out-matched the
        # real posting when the confirmation was traced back to a job.
        reply_kind = replies_mod.classify_reply(item).kind
        if reply_kind:
            result.skipped_nonjobs += 1
            result.skipped_kinds["employer_reply"] = (
                result.skipped_kinds.get("employer_reply", 0) + 1
            )
            continue

        classified = classify(item)
        if classified.kind != "job_alert":
            result.skipped_nonjobs += 1
            result.skipped_kinds[classified.kind] = (
                result.skipped_kinds.get(classified.kind, 0) + 1
            )
            continue

        if classified.source == "wellfound":
            # One email, several listings — `parse_job` returns at most one
            # draft, so a digest needs its own path rather than pretending it
            # is a single job. `job_alerts` counts listings here, not emails,
            # so it stays comparable to every other source's "jobs" line.
            drafts = parse_wellfound_digest(item)
            if not drafts:
                result.parse_failed += 1
                continue
            result.job_alerts += len(drafts)
            for draft in drafts:
                if enrich_descriptions:
                    draft = enrich_draft(draft)
                posting = draft_to_posting(draft)
                posting.raw["source_message_id"] = item.message_id
                postings.append(posting)
            continue

        result.job_alerts += 1
        draft = parse_job(item, classified.source, unwrap=unwrap)
        if draft is None:
            result.parse_failed += 1
            continue
        if enrich_descriptions:
            draft = enrich_draft(draft)
        posting = draft_to_posting(draft)
        posting.raw["source_message_id"] = item.message_id
        postings.append(posting)

    if not postings:
        return result

    def run(c):
        ingested = pipeline.ingest(postings, c)
        result.ingested_new = ingested.new
        result.duplicates = ingested.updated
        result.gated = ingested.gated
        result.reachable = ingested.reachable
        return result

    if conn is not None:
        return run(conn)
    with store.session() as c:
        return run(c)


def fetch_gmail(days: int | None = None, limit: int | None = None) -> list[MailItem]:
    """IMAP SELECT readonly. Never STORE, never SMTP."""
    address = config.gmail_address()
    password = config.gmail_app_password()
    if not address or not password:
        raise RuntimeError(
            "Gmail is not configured. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
            "in internship_code/.env (a Gmail app password, not your login "
            "password). Inbox access is read-only — jobbot never sends mail."
        )

    days = days if days is not None else config.INBOX_LOOKBACK_DAYS
    limit = limit if limit is not None else config.INBOX_MAX_MESSAGES
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")

    mail = imaplib.IMAP4_SSL(config.IMAP_HOST)
    try:
        mail.login(address, password)
        typ, _ = mail.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("Could not open INBOX as read-only.")
        typ, data = mail.search(None, f"(SINCE {since})")
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-limit:]
        items: list[MailItem] = []
        for msgid in ids:
            typ, payload = mail.fetch(msgid, "(RFC822)")
            if typ != "OK" or not payload:
                continue
            raw = payload[0][1]
            if isinstance(raw, bytes):
                try:
                    items.append(parse_rfc822(raw))
                except Exception as exc:
                    logger.warning("skipping message %s: %s", msgid, exc)
        return items
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def ingest_gmail(days: int | None = None, limit: int | None = None, conn=None) -> InboxResult:
    return ingest_items(fetch_gmail(days=days, limit=limit), conn=conn)

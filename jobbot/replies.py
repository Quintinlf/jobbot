"""Recognise employer replies to applications you already sent.

`inbox` reads mail looking for jobs to apply to. This module reads the same
mail looking for what came back — rejections, interview invites, and the
automated receipt that at least proves the application landed.

Two independent conditions must both hold before anything is recorded:

    the message reads like a reply   AND   it matches an application you sent

Either one alone produces garbage. "Unfortunately that role has closed" shows
up in job-alert digests, and a message from a company you applied to might be
a marketing newsletter. Requiring both, and requiring the match to be
unambiguous, is what keeps a mislabelled row out of the training set — a wrong
label is worse than a missing one, because it is invisible afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from jobbot import outcomes
from jobbot.boards import html_to_text
from jobbot.identity import ats_key, canonical_url

# Confidence at or above which a match is recorded without asking. Below it the
# message is held for confirmation rather than guessed at.
AUTO_MATCH = 0.80

# A reply cannot precede the application, and mail systems disagree about
# clocks and timezones by a few hours at most.
CLOCK_SLACK = timedelta(hours=12)


@dataclass
class ReplyVerdict:
    kind: str          # outcomes.REJECTED / INTERVIEW / OFFER / ACK, or ""
    confidence: float
    evidence: list[str] = field(default_factory=list)


@dataclass
class Match:
    row: object | None
    confidence: float
    why: str = ""
    candidates: list = field(default_factory=list)


@dataclass
class ReplyResult:
    scanned: int = 0
    replies_seen: int = 0
    recorded: list[tuple[str, str, str]] = field(default_factory=list)   # id, kind, label
    ambiguous: list[dict] = field(default_factory=list)
    orphans: list[dict] = field(default_factory=list)
    unmatched: int = 0
    duplicates: int = 0

    def summary(self) -> str:
        kinds: dict[str, int] = {}
        for _, kind, _ in self.recorded:
            kinds[kind] = kinds.get(kind, 0) + 1
        detail = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())) or "none"
        return (
            f"replies {self.replies_seen} · recorded {len(self.recorded)} ({detail}) · "
            f"needs confirming {len(self.ambiguous)} · unmatched {self.unmatched} · "
            f"already known {self.duplicates}"
        )


# ── Reply language ─────────────────────────────────────────────────────────────
# Strong patterns are near-unambiguous on their own. Weak ones only count as
# corroboration, because each of them appears in ordinary recruiting mail too.

_STRONG = {
    outcomes.REJECTED: (
        r"we (?:have )?(?:decided|elected|chosen) (?:not )?to (?:move forward|proceed) with other",
        r"(?:will |we )?not (?:be )?(?:moving|move|going) forward with your",
        r"we regret to inform",
        r"no longer (?:be )?(?:under consideration|considering your)",
        r"decided (?:not to (?:move|proceed|continue)|to pursue other candidates)",
        r"your application (?:was|has been) (?:not successful|unsuccessful|declined)",
        r"(?:won'?t|will not) be (?:progressing|advancing) (?:your|with your)",
        r"move forward with other candidates",
        # Declarative past tense only ("we have not selected you", "you were
        # not selected"). The bare "not selected for" version this replaced
        # also matched Figma's actual confirmation email — "If you are not
        # selected for this position, keep an eye on our jobs page" — which
        # is the opposite of a rejection: it is boilerplate inside a message
        # confirming the application went through. A real rejection commits
        # to what happened; a hedge like this is conditional on an outcome
        # that has not been decided yet.
        r"(?:have|has|were|was) not (?:been )?select(?:ed)? (?:for|to)",
    ),
    outcomes.INTERVIEW: (
        r"(?:like|love) to (?:schedule|set up|arrange) (?:a|an|some)",
        r"invit(?:e|ing) you to (?:an? )?(?:interview|first round|next round)",
        r"(?:phone|technical|initial) (?:screen|interview) with",
        r"(?:your|share your) availability (?:for|to)",
        r"schedule (?:a|an) (?:call|chat|conversation|interview|meeting) with",
        # Negation guard. Rejections are built out of this exact phrase --
        # "we have decided NOT to move forward in the process" -- and without
        # the lookbehind this pattern fires on every one of them. A Reddit
        # rejection was recorded as an interview on 2026-09-04 for precisely
        # this reason.
        r"(?<!not )(?<!won't be )(?<!will not be )(?<!unable to )"
        r"move(?:ing)? (?:you )?(?:forward|to the next) (?:to|in|stage|step|round)",
    ),
    outcomes.OFFER: (
        r"(?:pleased|delighted|happy) to (?:extend|offer) (?:you )?(?:an? )?(?:offer|position|role)",
        r"(?:formal )?offer of employment",
        r"we(?:'d| would) like to offer you",
    ),
    outcomes.ACK: (
        r"(?:we|thank you[^.]{0,40})?(?:have )?received your application",
        r"your application (?:has been|was) (?:received|submitted successfully)",
        r"thank(?:s| you) for (?:applying|your application|your interest in)",
        r"application (?:confirmation|received)",
    ),
}

_WEAK = {
    outcomes.REJECTED: (
        r"\bunfortunately\b",
        r"(?:many|a large number of) (?:qualified )?(?:applicants|candidates)",
        r"(?:wish|best of luck) (?:you )?(?:the best|luck) (?:in|with) your (?:job )?search",
        r"keep your (?:resume|application|details) on file",
    ),
    outcomes.INTERVIEW: (
        r"\bnext steps?\b",
        r"\bcalendar\b|\bcalendly\b|\bbook a time\b",
        r"looking forward to (?:speaking|meeting|talking) with you",
    ),
    outcomes.OFFER: (r"\bcompensation package\b", r"\bstart date\b"),
    outcomes.ACK: (r"do not reply to this (?:email|message)", r"applicant tracking"),
}

_COMPILED_STRONG = {
    kind: [re.compile(p, re.IGNORECASE) for p in pats] for kind, pats in _STRONG.items()
}
_COMPILED_WEAK = {
    kind: [re.compile(p, re.IGNORECASE) for p in pats] for kind, pats in _WEAK.items()
}

# Mail that is about jobs in general rather than about your application.
_BULK = re.compile(
    r"job alert|jobs? for you|recommended for you|new jobs? (?:matching|posted)|"
    r"\d+ new jobs|weekly (?:digest|roundup)|jobs you may(?: be interested)?",
    re.IGNORECASE,
)

# Marketing mail that is not about any application. A mortgage newsletter
# ("Home, Real Estate & Financing News, Vol. 98") was recorded as an INTERVIEW
# on 2026-08-26: somewhere past 8,000 characters it said "schedule a call
# with", which is one of the strong interview phrases, and `inbox` had already
# classified it a newsletter — the reply path just never asked.
#
# A real reply is *about the recipient's own application*, so that is what is
# required here rather than trying to enumerate every kind of marketing mail.
_NEWSLETTERISH = re.compile(
    r"unsubscribe|view (?:this|it) in (?:your )?browser|manage (?:your )?preferences|"
    r"\bvol\.?\s*\d+|newsletter",
    re.IGNORECASE,
)
_ABOUT_AN_APPLICATION = re.compile(
    r"your application|you applied|applying to|application (?:for|to|was|has)|"
    r"candidacy|your (?:candidate )?profile|the (?:role|position) you|"
    r"thank you for (?:your )?(?:interest|applying)|recruit(?:ing|er|ment)",
    re.IGNORECASE,
)


def _text_of(item) -> str:
    """Subject + body as one whitespace-normalised string.

    Every run of whitespace collapses to a single space, because email bodies
    are hard-wrapped at whatever column the sender's client chose and the
    patterns in this module are written with single spaces between words. A
    phrase like "decided not to move forward" is one string to a reader and
    two lines to a regex, which is how the negation guard on the interview
    pattern passed in production and failed on the same text re-wrapped.
    """
    raw = f"{item.subject}\n{item.text or ''}\n{html_to_text(item.html or '')}"
    return re.sub(r"\s+", " ", raw)


def classify_reply(item) -> ReplyVerdict:
    """Decide what kind of reply, if any, this message is.

    A strong phrase carries the verdict; weak phrases only raise confidence in
    a kind that already has a strong hit, except for rejections, where two weak
    hits together ("unfortunately" plus "best of luck in your search") are a
    recognisable enough shape to accept at reduced confidence.
    """
    blob = _text_of(item)
    if _BULK.search(item.subject or "") and not any(
        p.search(blob) for p in _COMPILED_STRONG[outcomes.REJECTED]
    ):
        return ReplyVerdict("", 0.0, ["bulk job-alert mail"])

    # Newsletter-shaped mail that never mentions an application is not a reply,
    # however many recruiting-sounding phrases happen to appear in it. Both
    # conditions are required: real ATS mail also carries an unsubscribe link,
    # so the unsubscribe alone must not suppress a genuine rejection.
    if _NEWSLETTERISH.search(blob) and not _ABOUT_AN_APPLICATION.search(blob):
        return ReplyVerdict("", 0.0, ["newsletter, not about an application"])

    scored: list[tuple[float, str, list[str]]] = []
    for kind in (outcomes.OFFER, outcomes.INTERVIEW, outcomes.REJECTED, outcomes.ACK):
        strong = [p.pattern for p in _COMPILED_STRONG[kind] if p.search(blob)]
        weak = [p.pattern for p in _COMPILED_WEAK[kind] if p.search(blob)]
        if strong:
            confidence = min(0.95, 0.75 + 0.1 * len(strong) + 0.05 * len(weak))
        elif kind == outcomes.REJECTED and len(weak) >= 2:
            confidence = 0.6
        else:
            continue
        scored.append((confidence, kind, strong + weak))

    if not scored:
        return ReplyVerdict("", 0.0, [])

    # A rejection that also says "thank you for applying" is a rejection. Order
    # the tie-break by how much the kind actually decides, not by score alone.
    #
    # REJECTED outranks INTERVIEW rather than tying with it. They were equal at
    # 2, which left a message matching both to be decided by confidence and
    # then by iteration order -- and INTERVIEW is iterated first, so it won.
    # The asymmetry is deliberate: an interview that is really a rejection
    # raises false hope AND writes a wrong terminal label into the training
    # set, while a rejection that is really an interview gets corrected by the
    # next email in the thread.
    priority = {outcomes.OFFER: 3, outcomes.REJECTED: 2, outcomes.INTERVIEW: 1, outcomes.ACK: 0}
    scored.sort(key=lambda s: (priority[s[1]], s[0]), reverse=True)
    confidence, kind, evidence = scored[0]
    return ReplyVerdict(kind, round(confidence, 2), evidence)


# ── Matching a reply to an application ─────────────────────────────────────────

_SUFFIXES = re.compile(
    r"\b(?:inc|llc|ltd|limited|corp|corporation|co|company|group|holdings|"
    r"technologies|technology|labs|systems|solutions|software|plc|gmbh|sa|ag)\b\.?",
    re.IGNORECASE,
)
_NOISE = re.compile(r"[^a-z0-9]+")
# Only words that carry no information about WHICH job. Role words like
# "engineer" and "analyst" stay in: they are generic across the job market but
# they are exactly what separates two applications at the same company, which
# is the only case where title matching has to do any work.
_STOP_TITLE = {
    "the", "and", "for", "with", "new", "remote", "hybrid", "onsite",
    "usa", "our", "team", "role", "position", "opening", "opportunity",
}


def normalize_company(name: str) -> str:
    cleaned = _SUFFIXES.sub(" ", (name or "").lower())
    return _NOISE.sub("", cleaned)


def _title_tokens(title: str) -> set[str]:
    tokens = {t for t in re.split(r"[^a-z0-9]+", (title or "").lower()) if len(t) > 2}
    return tokens - _STOP_TITLE


def _sender_domain(sender: str) -> str:
    match = re.search(r"[\w.+-]+@([\w.-]+)", sender or "")
    return (match.group(1) or "").lower() if match else ""


def _urls_keys(item) -> set[str]:
    keys = set()
    for url in getattr(item, "urls", []) or []:
        key = ats_key(url)
        if key:
            keys.add(key)
        canon = canonical_url(url)
        if canon:
            keys.add(canon)
        host = (urlparse(url).hostname or "").lower()
        if host:
            keys.add(f"host:{host}")
    return keys


def match_application(conn, item, applications=None) -> Match:
    """Find which application this message is replying to.

    Scored rather than first-match, because the strongest evidence (the posting
    URL echoed back in the email) is also the rarest, and the weakest (a
    company name that appears in the body) is ambiguous the moment you have
    applied to that company twice.
    """
    outcomes.ensure_schema(conn)
    rows = applications if applications is not None else conn.execute(
        """
        SELECT j.* FROM jobs j
        JOIN applications a ON a.external_id = j.external_id
        ORDER BY j.applied_at DESC
        """
    ).fetchall()
    if not rows:
        return Match(None, 0.0, "no applications on file")

    blob = _text_of(item).lower()
    blob_compact = _NOISE.sub("", blob)
    domain = _sender_domain(item.sender)
    domain_compact = _NOISE.sub("", domain.split(".")[0]) if domain else ""
    keys = _urls_keys(item)
    received = _received_dt(item)

    scored: list[tuple[float, object, str]] = []
    for row in rows:
        if received and row["applied_at"]:
            applied = _parse_dt(row["applied_at"])
            if applied and received + CLOCK_SLACK < applied:
                continue  # reply predates the application

        company = normalize_company(row["company"] or "")
        if not company:
            continue
        tokens = _title_tokens(row["title"] or "")
        overlap = sum(1 for t in tokens if t in blob)

        confidence, why = 0.0, ""
        row_key = ats_key(row["url"] or "") or canonical_url(row["url"] or "")
        if row_key and row_key in keys:
            confidence, why = 0.95, "posting url in the email"
        elif domain_compact and company and len(company) >= 4 and (
            company == domain_compact or company in domain_compact
        ):
            confidence, why = 0.85, f"sender domain {domain}"
        elif company in blob_compact:
            confidence, why = 0.55, "company named in the message"

        if not confidence:
            continue
        if overlap:
            confidence = min(0.97, confidence + 0.1 * min(overlap, 3))
            why += f" + {overlap} title word(s)"
        scored.append((round(confidence, 2), row, why))

    if not scored:
        return Match(None, 0.0, "no application matches this sender")

    if len(scored) == 1:
        # Exactly one application fits. The confidence floor exists to stop the
        # wrong one being picked out of several — with a single candidate there
        # is no choice to get wrong, only the question of whether this company
        # is right, which naming it in the message already answers. Combined
        # with `classify_reply` having called this a reply at all, that is
        # enough. Without this a rejection for your only application at a
        # company sits unrecorded, which is how the Hermeus letter was lost.
        confidence, row, why = scored[0]
        return Match(row, max(confidence, AUTO_MATCH + 0.05),
                     f"{why} (only application at this company)",
                     candidates=[(confidence, row)])

    scored.sort(key=lambda s: s[0], reverse=True)
    best_conf, best_row, why = scored[0]

    # Did jobbot itself open exactly one of these? That is direct evidence
    # about what happened, and it beats *coincidental* word overlap. A "Thank
    # you for applying to OpenAI" confirmation scored 32 candidates, and the
    # postings titled "Applied AI Engineer" out-ranked the one actually on
    # screen purely because the word "applying" appears in every such email.
    #
    # But it must NOT beat the email naming a role outright. A Reddit
    # rejection whose body said "your application for the Fullstack Software
    # Engineer, Notifications Lifecycle role" was attached to Data Movement
    # Platform instead, because that one happened to have been opened. The
    # email knew the answer and this check overrode it.
    #
    # So: skipped when the posting URL is echoed, and skipped when one
    # candidate has strictly more distinguishing title words in the message
    # than every other. Only when the text cannot separate them does "which
    # tab was open" get to decide.
    url_matched = any("posting url" in w for _, _, w in scored)
    shared_tokens = set.intersection(
        *[_title_tokens(r["title"] or "") for _, r, _ in scored]
    ) if scored else set()
    distinguishing = [
        sum(1 for t in _title_tokens(r["title"] or "") - shared_tokens if t in blob)
        for _, r, _ in scored
    ]
    top = max(distinguishing)
    text_names_one = top > 0 and distinguishing.count(top) == 1
    if text_names_one:
        idx = distinguishing.index(top)
        conf, row, why = scored[idx]
        return Match(row, max(conf, AUTO_MATCH + 0.05),
                     f"{why} + the message names this role",
                     candidates=[(conf, row)])

    if not url_matched:
        opened = [
            (c, r, w) for c, r, w in scored
            if conn.execute(
                "SELECT 1 FROM events WHERE external_id=? AND kind='prepared' LIMIT 1",
                (r["external_id"],),
            ).fetchone()
        ]
        if len(opened) == 1:
            conf, row, why = opened[0]
            return Match(row, max(conf, AUTO_MATCH + 0.05),
                         f"{why} (the only one jobbot opened)",
                         candidates=[(conf, row)])
        if len(opened) > 1:
            # Narrow to what was actually opened, then let the text heuristics
            # below choose among those rather than among everything.
            scored = opened
            best_conf, best_row, why = scored[0]

    tied = [s for s in scored if s[0] >= best_conf - 0.01]

    if len(tied) > 1:
        # Break the tie only on words that actually distinguish the tied
        # applications from each other. A token every candidate shares
        # ("acme", "engineer" when both are engineering roles) proves nothing
        # about which one the email means.
        shared = set.intersection(*[_title_tokens(r["title"] or "") for _, r, _ in tied])
        distinctive = [
            (sum(1 for t in _title_tokens(r["title"] or "") - shared if t in blob), c, r, w)
            for c, r, w in tied
        ]
        distinctive.sort(key=lambda d: d[0], reverse=True)
        if distinctive[0][0] > 0 and (
            len(distinctive) == 1 or distinctive[0][0] > distinctive[1][0]
        ):
            _, conf, row, why = distinctive[0]
            return Match(row, conf, f"{why} + distinguishing title word",
                         candidates=[(conf, row)])

        # Nothing in the message separates them — but jobbot knows something
        # the message does not: which of these postings it actually opened a
        # tab for. A confirmation from a company is far more likely to belong
        # to a posting it put in front of him than to one it never showed him.
        # This is the difference between "10 Hightouch jobs exist" and "1 of
        # them was on screen", and it is why prepare-applications logs a
        # `prepared` event per tab.
        prepared = [
            (c, r, w) for c, r, w in tied
            if conn.execute(
                "SELECT 1 FROM events WHERE external_id=? AND kind='prepared' LIMIT 1",
                (r["external_id"],),
            ).fetchone()
        ]
        if len(prepared) == 1:
            conf, row, why = prepared[0]
            return Match(row, conf, f"{why} + the only one jobbot opened",
                         candidates=[(conf, row)])
        if prepared:
            tied = prepared  # still ambiguous, but only among what was opened

        # Nothing separates them. Recording either would put a confident wrong
        # label in the training set, which is invisible once written.
        return Match(
            None,
            best_conf,
            f"{len(tied)} applications match equally well",
            candidates=[(c, r) for c, r, _ in tied],
        )
    return Match(best_row, best_conf, why, candidates=[(best_conf, best_row)])


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _received_dt(item):
    return _parse_dt(getattr(item, "received", "") or "")


# ── Ingest ─────────────────────────────────────────────────────────────────────

def ingest_replies(items, conn) -> ReplyResult:
    """Scan parsed mail for replies and record the confident ones.

    Ambiguous matches are returned rather than written. They surface as a
    single short prompt with the answer pre-filled, not as a research task.
    """
    result = ReplyResult(scanned=len(items))
    applications = conn.execute(
        """
        SELECT j.* FROM jobs j
        JOIN applications a ON a.external_id = j.external_id
        ORDER BY j.applied_at DESC
        """
    ).fetchall()
    if not applications:
        return result

    for item in items:
        verdict = classify_reply(item)
        if not verdict.kind:
            continue
        result.replies_seen += 1

        # This exact email already settled which job it was about, on an
        # earlier run. Re-matching it against whatever the candidate pool
        # looks like TODAY is how one Reddit confirmation ended up recorded
        # against two different postings — a duplicate-titled req scraped in
        # afterward gave the same message a second, wrong job to land on.
        if outcomes.message_already_used(conn, item.message_id):
            result.duplicates += 1
            continue

        match = match_application(conn, item, applications=applications)
        if match.row is None or match.confidence < AUTO_MATCH:
            # An acknowledgement that cannot be matched is dropped, not queued
            # for confirmation. It is not a label, so resolving it teaches the
            # model nothing — it would just be a chore with a deadline.
            if verdict.kind == outcomes.ACK:
                result.unmatched += 1
                continue
            if match.candidates:
                result.ambiguous.append({
                    "message_id": item.message_id,
                    "subject": item.subject,
                    "sender": item.sender,
                    "kind": verdict.kind,
                    "received": getattr(item, "received", ""),
                    "detail": _excerpt(item),
                    "candidates": [
                        {"external_id": r["external_id"], "company": r["company"],
                         "title": r["title"], "confidence": c}
                        for c, r in match.candidates
                    ],
                    "why": match.why,
                })
            else:
                # A real rejection for an application jobbot never sent — you
                # applied somewhere directly. Losing it would quietly bias the
                # training set toward the subset of your job search that
                # happened to go through this tool.
                result.orphans.append({
                    "message_id": item.message_id,
                    "sender": item.sender,
                    "subject": item.subject,
                    "kind": verdict.kind,
                    "received": getattr(item, "received", ""),
                    "company": guess_company(item),
                    "detail": _excerpt(item),
                })
                result.unmatched += 1
            continue

        wrote = outcomes.record(
            conn,
            match.row["external_id"],
            verdict.kind,
            source="email",
            detail=_excerpt(item),
            message_id=item.message_id,
            confidence=round(min(verdict.confidence, match.confidence), 2),
            at=getattr(item, "received", "") or None,
        )
        if wrote:
            result.recorded.append((
                match.row["external_id"],
                verdict.kind,
                f"{match.row['company']} — {match.row['title']}",
            ))
        else:
            result.duplicates += 1
    return result


_DISPLAY_NAME = re.compile(r'^\s*"?([^"<]+?)"?\s*<')
_ATS_SENDERS = ("greenhouse", "lever", "ashby", "workday", "gem.com", "no-reply", "noreply")


def guess_company(item) -> str:
    """Best guess at who sent a reply, for a pre-filled log-application command.

    The display name is the good signal — Lever sends as "Hermeus", Gem sends
    as "affirm.com Recruiting" — but it is only a suggestion, so it is never
    used for matching, only to save typing.
    """
    match = _DISPLAY_NAME.match(item.sender or "")
    name = (match.group(1) if match else "").strip()
    name = re.sub(r"\s*(?:recruiting|talent|careers?|hiring|team)\s*$", "", name,
                  flags=re.IGNORECASE).strip()
    name = re.sub(r"\.(?:com|io|co|ai|org)$", "", name, flags=re.IGNORECASE).strip()
    if name and not any(s in name.lower() for s in _ATS_SENDERS):
        return name
    subject = item.subject or ""
    at = re.search(r"(?:interest in|application to|applying to)\s+([A-Z][\w&.\- ]{1,40})",
                   subject)
    if at:
        return at.group(1).strip().rstrip(",")
    return ""


def register_applications_from_mail(items, conn) -> ReplyResult:
    """Record applications that the ATS confirmed but jobbot never logged.

    "Thank you for applying" from the employer's own system is stronger
    evidence than a keystroke in a terminal: it is the company saying they
    received it. Anything that interrupts the confirm loop — a crash, closing
    the window, quitting the run — loses applications that actually went out,
    and every one lost is a training row the model never gets.

    Only ever adds. An application already on file keeps its original date,
    because the first submission is the one whose features matter.
    """
    result = ReplyResult(scanned=len(items))
    outcomes.ensure_schema(conn)
    jobs = conn.execute(
        """
        SELECT * FROM jobs
        WHERE applied_at IS NULL AND knockout IS NULL
          AND status IN ('new', 'queued')
        """
    ).fetchall()
    if not jobs:
        return result

    for item in items:
        verdict = classify_reply(item)
        if verdict.kind != outcomes.ACK:
            continue
        result.replies_seen += 1

        # Same guarantee as ingest_replies, and needed here even more: this
        # function's whole candidate pool is "not yet applied" jobs, so a
        # duplicate-titled req scraped in after the fact is exactly the kind
        # of new, unique-looking single candidate that the confidence check
        # below would happily auto-accept — silently marking a job applied
        # that was never touched.
        if outcomes.message_already_used(conn, item.message_id):
            result.duplicates += 1
            continue

        match = match_application(conn, item, applications=jobs)
        if match.row is None or match.confidence < AUTO_MATCH:
            if match.candidates:
                result.ambiguous.append({
                    "message_id": item.message_id,
                    "subject": item.subject,
                    "sender": item.sender,
                    "kind": "applied",
                    "received": getattr(item, "received", ""),
                    "detail": _excerpt(item),
                    "candidates": [
                        {"external_id": r["external_id"], "company": r["company"],
                         "title": r["title"], "confidence": c}
                        for c, r in match.candidates
                    ],
                    "why": match.why,
                })
            else:
                result.unmatched += 1
            continue

        external_id = match.row["external_id"]
        when = getattr(item, "received", "") or outcomes.now()
        from jobbot import store

        store.set_status(conn, external_id, store.Status.APPLIED)
        # Date it from the confirmation, not from when this ran. The gap
        # between applying and hearing back is a model feature; stamping it
        # with today's date would quietly compress every one of them.
        conn.execute(
            "UPDATE jobs SET applied_at=? WHERE external_id=?", (when, external_id)
        )
        conn.execute(
            "UPDATE applications SET applied_at=? WHERE external_id=?",
            (when, external_id),
        )
        store.log_event(
            conn, external_id, "applied_from_confirmation",
            f"ATS confirmation: {item.subject[:150]}",
        )
        outcomes.record(
            conn, external_id, outcomes.ACK, source="email",
            detail=_excerpt(item), message_id=item.message_id,
            confidence=round(min(verdict.confidence, match.confidence), 2),
            at=when,
        )
        result.recorded.append((
            external_id, "applied", f"{match.row['company']} — {match.row['title']}"
        ))
        jobs = [r for r in jobs if r["external_id"] != external_id]
    return result


def _excerpt(item, limit: int = 1500) -> str:
    """Keep the reply's own words. `learn` mines these for stated reasons."""
    body = (item.text or html_to_text(item.html or "") or "").strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return f"{item.subject}\n\n{body}"[:limit]

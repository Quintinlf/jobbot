"""Batch export/import for drafting cover letters without the API.

The primary writer is ChatGPT Web, mediated by you:

    python -m jobbot export --limit 5      # ChatGPT-ready briefing
    # paste the file into chatgpt.com, save the filled file
    python -m jobbot import-letters <file> # reads drafts back into the DB

Nothing here logs into ChatGPT. Markers in the file are what import keys off,
so the prose can come from ChatGPT, Claude Code, or anything else that leaves
the comments in place.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from jobbot import config, horary, store
from jobbot.gating import Gate
from jobbot.profile import Profile
from jobbot.tailor import DEFAULT_FRAMING, FRAMINGS, SYSTEM_PROMPT, _profile_block

# Letters are separated by machine-readable markers so the import step can find
# them without depending on how the prose is formatted.
JOB_MARKER = "<!-- jobbot:job "
LETTER_OPEN = "<!-- jobbot:letter "
LETTER_CLOSE = "<!-- jobbot:end -->"

_JOB_RE = re.compile(
    re.escape(JOB_MARKER) + r"(?P<id>[^\s]+) -->(?P<body>.*?)(?=" + re.escape(JOB_MARKER) + r"|\Z)",
    re.DOTALL,
)
_LETTER_RE = re.compile(
    re.escape(LETTER_OPEN)
    + r"(?P<framing>[a-z_]+) -->(?P<letter>.*?)"
    + re.escape(LETTER_CLOSE),
    re.DOTALL,
)

CHATGPT_WEB_INSTRUCTIONS = """\
Paste this entire file into one ChatGPT conversation (chatgpt.com). Do not
split it across chats. ChatGPT Web is the writer; jobbot does not log in or
scrape the site.

For each job below there is exactly one empty letter block (unless this file
was exported with multiple education framings). Write the letter BETWEEN the
`jobbot:letter` and `jobbot:end` HTML comments. Leave those comments in place.
Do not edit any `jobbot:job` line — import keys off them.

How to process the batch:
1. Read the Candidate section once. That is the only background you may use.
2. For each job, read the full posting, then write one letter into that job's
   empty block.
3. After the last job, stop. Return the whole file, comments included.

Each letter must be:
- 250-400 words, three to four paragraphs
- specific to this employer and this role — name the company and the work
- grounded only in the candidate profile and resume context below
- a connection between what they have actually shipped and what this posting
  needs, not a restatement of the resume and not generic filler

Never invent experience, skills, coursework, employment, projects,
accomplishments, certifications, technologies, metrics, or employers.
If the profile is thin, write a shorter honest letter rather than padding.
No letterhead. No "Dear Hiring Manager" if you can open more directly.
"""


def export(
    profile: Profile,
    rows,
    out_dir: Path | None = None,
    desc_chars: int = 0,
    framings: list[str] | None = None,
    conn=None,
) -> Path:
    """Write a self-contained briefing file for ChatGPT Web.

    `framings` defaults to one letter per job (`projects_only`). Pass the
    full FRAMINGS list for the older three-variant file. `desc_chars=0`
    means include the whole stored posting.
    """
    config.ensure_dirs()
    out_dir = out_dir or config.OUTBOX_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    path = out_dir / f"batch_{date.today().isoformat()}.md"
    n = 1
    while path.exists():
        path = out_dir / f"batch_{date.today().isoformat()}_{n}.md"
        n += 1

    chosen = list(framings) if framings else [DEFAULT_FRAMING]
    for name in chosen:
        if name not in FRAMINGS:
            raise ValueError(f"unknown framing: {name}")

    parts = [
        f"# Cover letter batch — {date.today().isoformat()}",
        "",
        "## Instructions for ChatGPT Web",
        "",
        CHATGPT_WEB_INSTRUCTIONS.strip(),
        "",
        "### Rules",
        "",
        SYSTEM_PROMPT.strip(),
        "",
        "### Framing in this file",
        "",
    ]
    for name in chosen:
        parts += [f"**{name}** — {FRAMINGS[name]}", ""]
    if len(chosen) > 1:
        parts += [
            "This file has multiple education framings per job. Write a genuine",
            "letter into every block. They differ only in how education is",
            "handled, not in tone.",
            "",
        ]

    parts += [
        "---",
        "",
        "## Candidate",
        "",
        _profile_block(profile),
        "",
        "---",
        "",
        f"## Jobs ({len(rows)})",
        "",
    ]

    for row in rows:
        gate = row["gate"] or Gate.OPEN.value
        gate_label = (
            Gate(gate).label if gate in Gate._value2member_map_ else gate
        )
        description = (row["description"] or "").strip()
        if desc_chars and len(description) > desc_chars:
            description = description[:desc_chars] + "\n[…truncated]"

        parts += [
            f"{JOB_MARKER}{row['external_id']} -->",
            f"### {row['title']} — {row['company']}",
            "",
            f"- Location: {row['location'] or 'not listed'}",
            f"- Fit score: {row['score']}",
            f"- Eligibility: {gate_label}",
            f"- URL: {row['url']}",
            "",
            "<details><summary>Posting</summary>",
            "",
            description,
            "",
            "</details>",
            "",
        ]
        for name in chosen:
            parts += [f"{LETTER_OPEN}{name} -->", "", LETTER_CLOSE, ""]
        parts += ["---", ""]

    path.write_text("\n".join(parts), encoding="utf-8")

    # Say out loud which door this batch is aimed at, before any of it is sent.
    # Printed, not enforced: horary judgement never gates the queue.
    counts = horary.posting_counts(conn) if conn is not None else {}
    for line in horary.format_split(horary.channel_split(rows, counts)):
        print(line)

    return path


def parse(text: str) -> dict[str, dict[str, str]]:
    """Pull {external_id: {framing: letter}} out of a completed batch file.

    Blocks still holding the empty placeholder are skipped rather than saved as
    empty letters, so a partially finished batch imports cleanly.
    """
    out: dict[str, dict[str, str]] = {}
    for job_match in _JOB_RE.finditer(text):
        variants = {
            m.group("framing"): m.group("letter").strip()
            for m in _LETTER_RE.finditer(job_match.group("body"))
            if m.group("letter").strip()
        }
        if variants:
            out[job_match.group("id")] = variants
    return out


def import_letters(path: Path, conn) -> tuple[int, int]:
    """Load drafted letters into the database.

    Returns (jobs_imported, jobs_still_empty). Existing non-empty letters are
    not overwritten — that is `store.save_materials` behaviour.
    """
    text = Path(path).read_text(encoding="utf-8")
    parsed = parse(text)

    total_jobs = len(_JOB_RE.findall(text))
    imported = 0
    for external_id, variants in parsed.items():
        exists = conn.execute(
            "SELECT 1 FROM jobs WHERE external_id = ?", (external_id,)
        ).fetchone()
        if not exists:
            continue
        store.save_materials(conn, external_id, variants=variants)
        imported += 1

    return imported, total_jobs - imported

"""Per-job cover letter and resume-emphasis generation.

Uses the Anthropic API when ANTHROPIC_API_KEY is set. Without a key the rest of
the pipeline still works — you get a structured brief of what to emphasize
instead of finished prose, which is the part that actually needs judgement
anyway.

Nothing here submits anything. Output lands in the review queue for you to read
before it goes anywhere.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from jobbot import config, skills as skills_mod
from jobbot.gating import Gate
from jobbot.profile import Profile

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You write job application materials for an engineer with real shipped systems \
and no completed degree.

Rules that hold for every framing:
- Never invent experience, employers, credentials, dates, or metrics. Use only \
what the profile provides. If the profile is thin, write something shorter \
rather than padding it.
- Never apologize for the missing degree, in any framing. Do not use \
"despite", "although", "even though", or "unfortunately". No sentence may \
position the candidate as having something to make up for.
- Never state or imply a disability, diagnosis, or medical condition, even if \
the profile narrative hints at one. That disclosure is the candidate's to make \
in a specific letter, not something generated at scale.
- Concrete and specific over enthusiastic. No "I am passionate about". No \
"I was excited to see". No filler openers.
- Match the vocabulary of the job posting where it is honest to do so.
- Connect the candidate's actual projects, work, skills, and education to \
what this specific employer is hiring for. Do not restate the resume. Do not \
use generic filler that could be pasted onto any posting.
- 250-400 words, three to four paragraphs, no letterhead, no "Dear Hiring \
Manager" if you can open more directly. Professional, not robotic.
"""

# Three ways to handle education, chosen per job rather than globally. Some
# postings read as credential-focused and some don't, and the same letter is
# not right for both.
FRAMINGS: dict[str, str] = {
    "projects_only": (
        "FRAMING — projects only. Do not mention school, coursework, degrees, "
        "or education in any form. Open with the shipped project closest to "
        "this posting and what it measurably did. Education simply does not "
        "come up."
    ),
    "neutral": (
        "FRAMING — neutral mention. Reference the coursework and research work "
        "in one plain clause, positioned as context rather than as the "
        "qualification. No explanation of why it is unfinished. The projects "
        "still carry the letter."
    ),
    "narrative": (
        "FRAMING — narrative. Use the candidate's arc from the profile's "
        "narrative field: started college young, learned by building rather "
        "than through coursework, and shipped real systems in that time. Frame "
        "it as evidence of how they learn and what they do with time, never as "
        "an obstacle overcome and never as an appeal for sympathy. One short "
        "paragraph at most, then straight back to the work. If the profile has "
        "no narrative, fall back to the projects-only framing."
    ),
}

DEFAULT_FRAMING = "projects_only"


class CoverLetterProvider(Protocol):
    """Who writes the prose. The rest of jobbot only stores and pastes it.

    ChatGPT Web is the primary writer via the export/import file — not a
    browser login. The Anthropic API remains an optional in-app generator.
    Local models are not used for letters.
    """

    name: str


class ChatGPTWebExportProvider:
    name = "chatgpt_web"


class AnthropicAPIProvider:
    name = "anthropic_api"


PRIMARY_WRITER = ChatGPTWebExportProvider()


def _list_block(label: str, items) -> list[str]:
    values = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not values:
        return []
    return [f"\n{label}:"] + [f"- {v}" for v in values]


def _resume_excerpt(profile: Profile, limit: int = 4000) -> str:
    """Plain-text resume if the profile points at one, otherwise empty.

    PDF is not parsed here — drop a .txt next to the PDF (same stem) if you
    want the full resume in the ChatGPT briefing.
    """
    path = Path(profile.resume_path) if profile.resume_path else None
    if not path:
        return ""
    candidates = []
    if path.suffix.lower() == ".txt":
        candidates.append(path)
    candidates.append(path.with_suffix(".txt"))
    for cand in candidates:
        if cand.exists():
            text = cand.read_text(encoding="utf-8", errors="replace").strip()
            return text[:limit]
    return ""


def _profile_block(profile: Profile, include_narrative: bool = True) -> str:
    lines = [
        f"Name: {profile.full_name}",
        f"Location: {profile.city}, {profile.state}",
        f"Background: {profile.background}",
        f"Skills: {', '.join(profile.skills)}",
    ]
    if profile.github_url:
        lines.append(f"GitHub: {profile.github_url}")
    if profile.linkedin_url:
        lines.append(f"LinkedIn: {profile.linkedin_url}")
    if profile.education.strip():
        lines.append(f"Education: {profile.education.strip()}")
    if profile.major.strip():
        lines.append(f"Major: {profile.major.strip()}")
    lines += _list_block("Coursework", profile.coursework)
    lines += _list_block("Certifications", profile.certifications)
    lines += _list_block("Accomplishments", profile.accomplishments)
    if include_narrative and profile.narrative.strip():
        lines.append(f"\nNarrative (only for the narrative framing):\n{profile.narrative}")

    if profile.projects:
        lines.append("\nProjects:")
        for p in profile.projects:
            if not p.get("name"):
                continue
            stack = ", ".join(p.get("stack") or [])
            lines.append(
                f"- {p['name']} ({stack}): {p.get('description', '')} "
                f"Impact: {p.get('impact', 'n/a')}"
            )

    if profile.work_experience:
        lines.append("\nWork experience:")
        for w in profile.work_experience:
            if not w.get("company"):
                continue
            lines.append(
                f"- {w.get('title', '')} at {w['company']} "
                f"({w.get('start_date', '')}–{w.get('end_date', '')}): "
                f"{w.get('description', '')}"
            )

    excerpt = _resume_excerpt(profile)
    if excerpt:
        lines += ["\nResume:", excerpt]

    return "\n".join(lines)


def offline_brief(profile: Profile, title: str, company: str, description: str, gate: str) -> str:
    """A structured brief when no API key is configured.

    Deliberately not a fake cover letter — a template with your name pasted in
    reads worse than nothing and would go out under your name.
    """
    # Scan the whole posting. Truncating first made this report "no direct
    # overlap — reconsider this one" for a job listing Python, LightGBM,
    # XGBoost and MLflow, because the requirements section sat past the cutoff.
    desc = description or ""
    overlap = sorted(
        skills_mod.canonicalize(profile.skills) & set(skills_mod.extract(desc))
    )
    gate_note = {
        Gate.EQUIVALENT_OK.value: (
            "This posting accepts equivalent experience. Say so implicitly by "
            "leading with shipped work — no need to address education at all."
        ),
        Gate.OPEN.value: "No degree language in this posting. Lead with projects.",
        Gate.SOFT_DEGREE.value: (
            "Degree is preferred but not required. Do not raise it. Put your "
            "strongest shipped project in the first sentence."
        ),
    }.get(gate, "Check the gate before spending time on this one.")

    project_names = [p.get("name", "") for p in profile.projects if p.get("name")]

    # Built line by line rather than with textwrap.dedent on an f-string:
    # dedent strips the *common* leading whitespace, and an interpolated value
    # carrying its own newlines drops that common prefix to nothing, leaving
    # the entire brief indented and rendering as a code block.
    lines = [
        f"# Brief — {title} @ {company}",
        "",
        "(No ANTHROPIC_API_KEY set, so this is a checklist rather than a draft.)",
        "",
        "## Gate",
        gate_note,
        "",
        "## Skills that overlap this posting",
        ", ".join(overlap) if overlap else "No direct overlap found — reconsider this one.",
        "",
        "## Lead with",
        "Pick the project below that is closest to what this posting describes,",
        "and open with what it does and what it proves:",
    ]
    lines += [f"  - {name}" for name in project_names] or ["  (no projects in profile yet)"]
    lines += [
        "",
        "## Do not",
        "- Mention the degree.",
        '- Open with "I am passionate about".',
        "- Claim anything the profile does not support.",
    ]
    return "\n".join(lines)


def generate(
    profile: Profile,
    title: str,
    company: str,
    description: str,
    gate: str = "",
    framing: str = DEFAULT_FRAMING,
) -> tuple[str, str]:
    """Return (cover_letter, resume_notes) for one education framing.

    Falls back to an offline brief if no key is configured or the call fails,
    so a refresh run never dies on the tailoring step.
    """
    key = config.anthropic_key()
    if not key:
        brief = offline_brief(profile, title, company, description, gate)
        return brief, ""

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed; falling back to offline brief")
        return offline_brief(profile, title, company, description, gate), ""

    prompt = f"""\
Job posting
-----------
Title: {title}
Company: {company}

{description or ''}

Candidate profile
-----------------
{_profile_block(profile, include_narrative=(framing == "narrative"))}

{FRAMINGS.get(framing, FRAMINGS[DEFAULT_FRAMING])}

Produce exactly two sections, in this order, with these headers:

## Cover letter
(250-400 words)

## Resume emphasis
(3-5 bullets: which projects and skills to move to the top of the resume for
this specific posting, and which to cut. Reference only what is in the profile.)
"""

    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=2500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
    except Exception as exc:
        logger.warning("tailoring failed for %s @ %s: %s", title, company, exc)
        return offline_brief(profile, title, company, description, gate), ""

    if "## Resume emphasis" in text:
        letter, _, notes = text.partition("## Resume emphasis")
        return letter.replace("## Cover letter", "").strip(), notes.strip()
    return text.strip(), ""

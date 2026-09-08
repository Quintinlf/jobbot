"""What you have actually written, measured rather than remembered.

Applications keep asking "rate your proficiency with X". Answering from memory
goes wrong in both directions: you undersell what you use daily and oversell
something you touched once, and either way a technical screen finds out.

So this counts. It walks your code, tallies non-blank lines per language, and
stores the result. Two things then use it:

  * proficiency questions on forms
  * scoring, so a posting whose whole stack you have never written stops
    outranking one you could do tomorrow

The asymmetry in `answer_level` is deliberate and is the point of the module.
Zero lines is a fact — you can answer "never used this" with confidence. Forty
thousand lines is not proof of expertise, so a positive claim is only ever
recommended, never filled in for you.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from jobbot import config

EVIDENCE_PATH = config.DATA_DIR / "skill_evidence.json"

# Extension to language. Kept small: a language nobody asks you to rate is
# noise in the table.
EXTENSIONS: dict[str, str] = {
    ".py": "Python", ".ipynb": "Jupyter",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".swift": "Swift",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cc": "C++",
    ".scala": "Scala", ".r": "R", ".m": "MATLAB",
    ".sql": "SQL", ".sh": "Shell", ".ps1": "PowerShell",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS",
    ".yaml": "YAML", ".yml": "YAML", ".tf": "Terraform",
}

# Directories that hold other people's code. Counting these would report
# node_modules as your Ruby experience.
SKIP_DIRS = {
    "node_modules", ".git", "venv", ".venv", "env", "__pycache__", "dist",
    "build", "site-packages", ".next", ".nuxt", "vendor", "target", "coverage",
    ".pytest_cache", ".mypy_cache", ".cache", "chrome_profile",
    "browser_profile", "bower_components", "third_party", "migrations",
}

MAX_FILE_BYTES = 3_000_000

# Frameworks and tools, found by name in file contents rather than extension.
# Only ones a form is likely to ask about.
MARKERS: dict[str, tuple[str, ...]] = {
    "React": ("from 'react'", 'from "react"', "import React"),
    "Django": ("from django", "import django"),
    "Flask": ("from flask", "import flask"),
    "FastAPI": ("from fastapi", "import fastapi"),
    "pandas": ("import pandas", "from pandas"),
    "PyTorch": ("import torch", "from torch"),
    "TensorFlow": ("import tensorflow", "from tensorflow"),
    "scikit-learn": ("from sklearn", "import sklearn"),
    "Playwright": ("from playwright", "import playwright"),
    "Docker": ("FROM python", "FROM node", "FROM ubuntu"),
}

# Line counts at which a claim becomes defensible. Round numbers, not science —
# they exist so the boundary is visible and arguable rather than implied.
LEVELS = (
    (0, "none"),          # never written a line
    (1, "trace"),         # touched it, cannot claim it
    (500, "working"),     # has built something in it
    (5000, "strong"),     # sustained real work
    (25000, "primary"),   # one of your main languages
)


@dataclass
class Skill:
    name: str
    lines: int = 0
    files: int = 0
    kind: str = "language"

    @property
    def level(self) -> str:
        label = "none"
        for threshold, name in LEVELS:
            if self.lines >= threshold:
                label = name
        return label


@dataclass
class Evidence:
    scanned_at: str = ""
    roots: list[str] = field(default_factory=list)
    skills: dict[str, Skill] = field(default_factory=dict)

    def get(self, name: str) -> Skill:
        for key, skill in self.skills.items():
            if key.lower() == (name or "").lower():
                return skill
        return Skill(name=name)

    def total_lines(self) -> int:
        return sum(s.lines for s in self.skills.values() if s.kind == "language")

    def ranked(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda s: -s.lines)


def scan(roots: list[Path] | None = None, read_markers: bool = True) -> Evidence:
    """Walk the given directories and tally what is actually there."""
    from datetime import datetime, timezone

    roots = roots or [config.BASE_DIR.parent]
    tally: dict[str, Skill] = {}
    marker_hits: dict[str, set] = {name: set() for name in MARKERS}

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
            ]
            for filename in filenames:
                ext = Path(filename).suffix.lower()
                language = EXTENSIONS.get(ext)
                if not language:
                    continue
                path = Path(dirpath) / filename
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue

                count = sum(1 for line in text.splitlines() if line.strip())
                skill = tally.setdefault(language, Skill(name=language))
                skill.lines += count
                skill.files += 1

                if read_markers and ext in (".py", ".ts", ".tsx", ".js", ".jsx", ""):
                    lowered = text[:20000].lower()
                    for name, needles in MARKERS.items():
                        if any(n.lower() in lowered for n in needles):
                            marker_hits[name].add(str(path))

    for name, paths in marker_hits.items():
        if paths:
            tally[name] = Skill(name=name, files=len(paths),
                                lines=len(paths), kind="framework")

    return Evidence(
        scanned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        roots=[str(r) for r in roots],
        skills=tally,
    )


def save(evidence: Evidence, path: Path | None = None) -> Path:
    config.ensure_dirs()
    path = path or EVIDENCE_PATH
    payload = {
        "scanned_at": evidence.scanned_at,
        "roots": evidence.roots,
        "skills": {
            k: {"lines": s.lines, "files": s.files, "kind": s.kind}
            for k, s in evidence.skills.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load(path: Path | None = None) -> Evidence:
    path = path or EVIDENCE_PATH
    if not path.exists():
        return Evidence()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Evidence()
    skills = {
        k: Skill(name=k, lines=v.get("lines", 0), files=v.get("files", 0),
                 kind=v.get("kind", "language"))
        for k, v in (data.get("skills") or {}).items()
    }
    return Evidence(
        scanned_at=data.get("scanned_at", ""),
        roots=data.get("roots", []),
        skills=skills,
    )


# ── Answering a proficiency question ───────────────────────────────────────────

# Wording that means "I have not used this". These are the only options this
# module will ever select on its own.
_NONE_OPTION = (
    "no experience", "haven't used", "have not used", "never used",
    "neither", "none", "not familiar", "no exposure",
)


def languages_in(question: str) -> list[str]:
    """Which known skills a question names.

    Word boundaries are not optional here. "C" and "R" are real language names
    and also letters, so a plain substring test found both inside "Rate your
    proficiency" and reported 8,620 lines of C as evidence about Go. Short
    names additionally have to match the original casing, because "go" and "r"
    appear in ordinary prose and "Go" and "R" do not.
    """
    import re as _re

    text = question or ""
    found: list[str] = []
    for name in list(EXTENSIONS.values()) + list(MARKERS):
        if name in found:
            continue
        pattern = r"(?<![\w+#])" + _re.escape(name) + r"(?![\w+#])"
        flags = 0 if len(name) <= 2 else _re.IGNORECASE
        if _re.search(pattern, text, flags):
            found.append(name)
    return found


def answer_level(evidence: Evidence, question: str, options: list[str]):
    """Pick an option for a proficiency question, or explain why not.

    Returns (option_text_or_None, note). An option comes back only when every
    skill the question names has zero measured lines AND the option list has a
    clear "never used this" choice. Everything else is a judgement about how
    good you are, which a line count cannot settle — so it returns a note for
    the report and leaves the question alone.
    """
    named = languages_in(question)
    if not named:
        return None, ""

    measured = {name: evidence.get(name).lines for name in named}
    summary = ", ".join(f"{k}: {v:,} lines" for k, v in measured.items())

    if any(count > 0 for count in measured.values()):
        return None, f"proficiency question — your code says {summary}; you choose"

    for option in options:
        lowered = option.lower()
        if any(phrase in lowered for phrase in _NONE_OPTION):
            return option, f"no measured usage ({summary})"

    return None, f"no measured usage ({summary}) but no 'never used' option offered"


def stack_overlap(evidence: Evidence, description: str) -> tuple[list[str], list[str]]:
    """(languages the posting names that you have, ones you do not).

    Goes through `languages_in` for the same reason it exists: a plain
    substring scan of a job description finds "C" and "R" in every paragraph.
    """
    have, missing = [], []
    for name in languages_in(description or ""):
        if name not in EXTENSIONS.values():
            continue
        if evidence.get(name).lines >= 500:
            have.append(name)
        else:
            missing.append(name)
    return sorted(have), sorted(missing)

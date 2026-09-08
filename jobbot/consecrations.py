"""Surface the consecrated talismans sitting in `consecrations/`.

Three career-adjacent works were made in Astrology_project's talisman engine
and copied here (see `consecrations/README.md`): a Mars victory/protection
talisman, and two mansion-of-the-Moon talismans elected specifically for
employment (mansion 20 and mansion 24, sigil word EMPLOYMENT).

This module reads their own record JSON back verbatim — it does not
recompute orbs, re-derive quality scores, or invent a rating. The record
already says what happened (a clean election, or a retry with a wider orb);
repeating it, not reinterpreting it, is the honest move here. Nothing here
feeds `outcomes`/`learn` — that model's features are all frozen, checkable
facts about a posting, and a talisman's presence is neither.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from jobbot import config

CONSECRATIONS_DIR = config.BASE_DIR / "consecrations"

_MANSION_RE = re.compile(r"mansion (\d+) \(([^)]+)\)", re.IGNORECASE)


@dataclass
class Consecration:
    name: str
    intent: str
    planet: str
    consecrated_local: str
    status_note: str
    source: Path


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _victory(path: Path) -> Consecration | None:
    data = _load_json(path)
    if data is None:
        return None
    election = data.get("election", {})
    unresolved = [
        c["condition"] for c in election.get("conditions", [])
        if c.get("status") == "unverifiable"
    ]
    note = election.get("summary", "")
    if unresolved:
        note += f" (unresolved: {', '.join(unresolved)})"
    return Consecration(
        name=data.get("title", "VICTORY"),
        intent=data.get("intent", ""),
        planet=data.get("planet", ""),
        consecrated_local=data.get("moment_local", ""),
        status_note=note,
        source=path,
    )


def _mansion_job(path: Path) -> Consecration | None:
    data = _load_json(path)
    if data is None:
        return None
    notes = data.get("notes", "")
    m = _MANSION_RE.search(notes)
    label = f"Mansion {m.group(1)} ({m.group(2)})" if m else data.get("recipe_id", "")
    word = data.get("sigil", {}).get("word", {}).get("text", "")
    return Consecration(
        name=f"{label} — {word}" if word else label,
        intent=f"sigil word '{word}' traced over the Moon's kamea" if word else "",
        planet=data.get("planet", ""),
        consecrated_local=data.get("rendered_local") or data.get("elected_local", ""),
        status_note=notes,
        source=path,
    )


# (loader, relative path) — the fixed set of three works copied into this repo.
_KNOWN: tuple[tuple, ...] = (
    (_victory, "victory_20260806T1640.json"),
    (_mansion_job, "lunar_mansion_job/mansion_20_job_record.json"),
    (_mansion_job, "lunar_mansion_job/mansion_24_job_record.json"),
)


def load_all() -> list[Consecration]:
    """The consecrations found on disk. Missing files are skipped, not errors —
    these are copies of another repo's output and can be re-synced or absent."""
    found = []
    for loader, rel in _KNOWN:
        path = CONSECRATIONS_DIR / rel
        if not path.exists():
            continue
        item = loader(path)
        if item is not None:
            found.append(item)
    return found


def imprint_lines() -> list[str]:
    """One line per consecration: its name and its path relative to this repo.

    Relative, not absolute — an absolute path would bake this machine's
    Windows username into every résumé that goes out.
    """
    lines = []
    for item in load_all():
        rel = item.source.relative_to(CONSECRATIONS_DIR.parent).as_posix()
        lines.append(f"{item.name}: {rel}")
    return lines


def sigil_points(line: str, box: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """Turn one consecration path into a deterministic polyline.

    The classical way of putting a name on a talisman is not to write it out.
    It is to reduce it to a figure -- Agrippa's method walks the letters across
    a planetary square and keeps the line that traces between them. This is the
    same shape of operation on a digest of the path: the string decides the
    figure, the same string always gives the same figure, and nothing about it
    is text.

    That last part is the practical reason for the change. The paths used to be
    drawn as PDF text in render mode 3, which paints nothing but stays in the
    text layer -- so `extract_text()` returned them, and every ATS that parsed
    the resume read three occult file paths appended to the education section.
    Hidden text is also the exact signature of white-font keyword stuffing,
    which some parsers flag on sight. A stroked path has no such problem: there
    is no text object to extract and nothing for a keyword heuristic to find.
    """
    import hashlib

    x0, y0, x1, y1 = box
    digest = hashlib.sha256(line.encode("utf-8")).digest()
    points: list[tuple[float, float]] = []
    # Two bytes per point: one for each axis, mapped into the box.
    for i in range(0, 16, 2):
        fx = digest[i] / 255.0
        fy = digest[i + 1] / 255.0
        points.append((x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)))
    return points


def imprint_resume(pdf_path: Path | None = None, ink: float = 0.985) -> Path:
    """Draw the consecrations onto the resume as sigils, not as text.

    `autofill.py` uploads this exact file, so whatever is in it goes to every
    employer. It previously went as invisible text; see `sigil_points` for why
    that had to change and what replaced it.

    Two carriers, both outside the text layer:

      * one stroked polyline per consecration, in the bottom margin, at
        `ink` grey -- present in the page, not present in `extract_text()`.
      * the record paths in a custom PDF metadata key, which is where a
        document's provenance belongs and which no resume parser reads as
        resume content.
    """
    import pypdf
    from reportlab.pdfgen import canvas

    pdf_path = pdf_path or (config.RESUME_DIR / "resume.pdf")
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    lines = imprint_lines()
    if not lines:
        raise RuntimeError("no consecrations found in consecrations/ to imprint")

    from io import BytesIO

    writer = pypdf.PdfWriter(clone_from=str(pdf_path))
    if not writer.pages:
        raise RuntimeError(f"{pdf_path} has no pages to imprint")
    page0 = writer.pages[0]
    width, height = float(page0.mediabox.width), float(page0.mediabox.height)

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setStrokeColorRGB(ink, ink, ink)
    c.setLineWidth(0.2)
    # The bottom margin, below anything the resume itself sets type in.
    band = (36.0, 10.0, width - 36.0, 26.0)
    for offset, line in enumerate(lines):
        pts = sigil_points(line, band)
        path = c.beginPath()
        path.moveTo(*pts[0])
        for x, y in pts[1:]:
            path.lineTo(x, y)
        c.drawPath(path, stroke=1, fill=0)
        band = (band[0], band[1] + 0.4 * offset, band[2], band[3] + 0.4 * offset)
    c.save()
    buf.seek(0)
    overlay_page = pypdf.PdfReader(buf).pages[0]

    for page in writer.pages:
        page.merge_page(overlay_page)

    writer.add_metadata({"/Consecrations": " | ".join(lines)})

    out = BytesIO()
    writer.write(out)
    pdf_path.write_bytes(out.getvalue())
    return pdf_path


def render(item: Consecration) -> str:
    lines = [f"{item.name}  ({item.planet})"]
    if item.intent:
        lines.append(f"  {item.intent}")
    # The mansion records' own notes already open with "consecrated ..." /
    # "rendered ..." and the timestamp, so printing consecrated_local again
    # first would say the same moment twice.
    note = item.status_note
    if item.consecrated_local and not note.lower().startswith(("consecrated", "rendered")):
        lines.append(f"  consecrated {item.consecrated_local}")
    if note:
        lines.append(f"  {note}")
    return "\n".join(lines)

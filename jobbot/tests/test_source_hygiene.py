"""No control characters in the source.

Found 2026-09-08. `r"^name\b"` had been written into `autofill.py` as
`r"^name\x08"` — a literal backspace where a word boundary was meant. The
pattern compiles, matches nothing, and reports no error, so the full-name spec
for Ashby forms had never once fired and there was nothing to notice.

A regex that silently cannot match is the worst shape of bug this codebase
has: every symptom looks like "the form did not have that field".
"""

from __future__ import annotations

from pathlib import Path

import pytest

import jobbot

# Everything below 0x20 except the three that legitimately appear in source.
FORBIDDEN = {chr(c) for c in range(32)} - {"\n", "\t", "\r"}

SOURCES = sorted(Path(jobbot.__file__).parent.rglob("*.py"))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_control_characters(path):
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), 1):
        bad = sorted({ch for ch in line} & FORBIDDEN)
        assert not bad, (
            f"{path.name}:{line_no} contains {[hex(ord(c)) for c in bad]} — "
            "almost certainly an escape like \b that was written as the "
            "character it denotes"
        )

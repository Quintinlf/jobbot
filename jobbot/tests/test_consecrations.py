"""Reading the copied talisman records back — never inventing what's missing."""

from __future__ import annotations

import json

import pytest

from jobbot import consecrations


@pytest.fixture
def dir_(tmp_path, monkeypatch):
    # Nested under tmp_path, named "consecrations", so relative-path tests see
    # the same "consecrations/..." shape imprint_lines() produces in real use.
    path = tmp_path / "consecrations"
    monkeypatch.setattr(consecrations, "CONSECRATIONS_DIR", path)
    return path


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_missing_directory_yields_nothing(dir_):
    assert consecrations.load_all() == []


def test_reads_the_victory_record(dir_):
    _write(dir_ / "victory_20260806T1640.json", {
        "title": "VICTORY",
        "intent": "Protection and victory over what is arrayed against the carrier",
        "planet": "Mars",
        "moment_local": "2026-08-06T09:40:00-07:00",
        "election": {
            "summary": "ELECTION MET, pending unverifiable conditions",
            "conditions": [
                {"condition": "Thursday (planetary day)", "status": "satisfied"},
                {"condition": "operator in a state of ritual purity",
                 "status": "unverifiable"},
            ],
        },
    })
    items = consecrations.load_all()
    assert len(items) == 1
    item = items[0]
    assert item.name == "VICTORY"
    assert item.planet == "Mars"
    assert "ritual purity" in item.status_note


def test_reads_a_mansion_job_record(dir_):
    _write(dir_ / "lunar_mansion_job" / "mansion_24_job_record.json", {
        "recipe_id": "mansion-24-job",
        "planet": "Moon",
        "rendered_local": "2026-08-24T17:36:13-07:00",
        "notes": "rendered -- Moon in mansion 24 (Caadacohot / Sa'd al Su'ud), "
                 "waxing, on ascendant (orb 1.45 deg), malefics clear",
        "sigil": {"word": {"text": "EMPLOYMENT"}},
    })
    items = consecrations.load_all()
    assert len(items) == 1
    item = items[0]
    assert "Mansion 24" in item.name
    assert "EMPLOYMENT" in item.name
    assert item.planet == "Moon"


def test_partial_set_skips_files_that_are_not_there(dir_):
    _write(dir_ / "lunar_mansion_job" / "mansion_20_job_record.json", {
        "recipe_id": "mansion-20-job",
        "planet": "Moon",
        "elected_local": "2026-08-20T15:06:00-07:00",
        "notes": "Moon in mansion 20 (Nahaym / al-Naaim), on ascendant",
        "sigil": {"word": {"text": "EMPLOYMENT"}},
    })
    items = consecrations.load_all()
    assert len(items) == 1


def test_malformed_json_is_skipped_not_raised(dir_):
    path = dir_ / "victory_20260806T1640.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert consecrations.load_all() == []


def test_render_does_not_repeat_the_timestamp(dir_):
    _write(dir_ / "lunar_mansion_job" / "mansion_24_job_record.json", {
        "recipe_id": "mansion-24-job",
        "planet": "Moon",
        "rendered_local": "2026-08-24T17:36:13-07:00",
        "notes": "rendered 2026-08-24T17:36:13-07:00 -- Moon in mansion 24, clean",
        "sigil": {"word": {"text": "EMPLOYMENT"}},
    })
    item = consecrations.load_all()[0]
    text = consecrations.render(item)
    assert text.count("2026-08-24T17:36:13-07:00") == 1


def test_imprint_lines_are_relative_not_absolute(dir_):
    _write(dir_ / "victory_20260806T1640.json", {
        "title": "VICTORY", "intent": "x", "planet": "Mars",
        "moment_local": "2026-08-06T09:40:00-07:00", "election": {},
    })
    lines = consecrations.imprint_lines()
    assert len(lines) == 1
    assert "VICTORY: consecrations/victory_20260806T1640.json" == lines[0]
    assert str(dir_) not in lines[0]


def test_imprint_resume_leaves_no_extractable_occult_text(dir_, tmp_path):
    """The paths must not reach an ATS parser.

    They used to. The imprint drew them as PDF text in render mode 3, which
    paints nothing but stays in the text layer, so `extract_text()` returned
    three occult file paths appended to the education section of every resume
    uploaded to every employer. Hidden text is also the signature a parser
    looks for when it screens for white-font keyword stuffing.
    """
    pypdf = pytest.importorskip("pypdf")
    from reportlab.pdfgen import canvas as rl_canvas

    _write(dir_ / "victory_20260806T1640.json", {
        "title": "VICTORY", "intent": "x", "planet": "Mars",
        "moment_local": "2026-08-06T09:40:00-07:00", "election": {},
    })

    resume = tmp_path / "resume.pdf"
    c = rl_canvas.Canvas(str(resume), pagesize=(612, 792))
    c.drawString(72, 700, "Resume content that must survive the overlay")
    c.save()

    out = consecrations.imprint_resume(resume)
    assert out == resume

    reader = pypdf.PdfReader(str(resume))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text() or ""

    assert "Resume content that must survive the overlay" in text
    for leak in ("VICTORY", "consecrations/", "victory_20260806T1640"):
        assert leak not in text, f"{leak!r} is still extractable"


def test_the_consecrations_are_still_carried_in_metadata(dir_, tmp_path):
    """Not extractable as resume text is the point; not present at all is not.
    Provenance belongs in metadata, which no resume parser reads as content."""
    pypdf = pytest.importorskip("pypdf")
    from reportlab.pdfgen import canvas as rl_canvas

    _write(dir_ / "victory_20260806T1640.json", {
        "title": "VICTORY", "intent": "x", "planet": "Mars",
        "moment_local": "2026-08-06T09:40:00-07:00", "election": {},
    })
    resume = tmp_path / "resume.pdf"
    c = rl_canvas.Canvas(str(resume), pagesize=(612, 792))
    c.drawString(72, 700, "Resume")
    c.save()

    consecrations.imprint_resume(resume)

    meta = pypdf.PdfReader(str(resume)).metadata or {}
    assert "victory_20260806T1640.json" in str(meta.get("/Consecrations", ""))


def test_a_sigil_is_the_same_figure_every_time():
    """The string decides the figure, and the same string gives the same
    figure — otherwise it is decoration, not a consecration."""
    box = (0.0, 0.0, 100.0, 20.0)
    once = consecrations.sigil_points("consecrations/victory.json", box)
    twice = consecrations.sigil_points("consecrations/victory.json", box)
    other = consecrations.sigil_points("consecrations/mansion_20.json", box)

    assert once == twice
    assert once != other
    assert all(0.0 <= x <= 100.0 and 0.0 <= y <= 20.0 for x, y in once)


def test_imprint_resume_without_any_consecrations_raises(dir_, tmp_path):
    from reportlab.pdfgen import canvas as rl_canvas

    resume = tmp_path / "resume.pdf"
    rl_canvas.Canvas(str(resume), pagesize=(612, 792)).save()

    with pytest.raises(RuntimeError):
        consecrations.imprint_resume(resume)


def test_imprint_resume_missing_file_raises(dir_, tmp_path):
    _write(dir_ / "victory_20260806T1640.json", {
        "title": "VICTORY", "intent": "x", "planet": "Mars",
        "moment_local": "2026-08-06T09:40:00-07:00", "election": {},
    })
    with pytest.raises(FileNotFoundError):
        consecrations.imprint_resume(tmp_path / "does_not_exist.pdf")

"""Applicant profile.

The existing application_codes/applicant_profile.json was never filled in — it
still says "Your Name" and points resume_path at a file that does not exist.
Anything downstream that tailors a document is worthless until this is real, so
the profile validates itself loudly rather than silently emitting placeholders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jobbot import config

PLACEHOLDERS = {
    "",
    "your",
    "name",
    "your name",
    "your.email@example.com",
    "path/to/your/resume.pdf",
    "+1-234-567-8900",
    "https://linkedin.com/in/yourprofile",
    "https://github.com/yourusername",
}


@dataclass
class Profile:
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""

    # Deliberately not "education". Framing matters: what you can do is the
    # pitch, and coursework is one input to that rather than the headline.
    background: str = ""

    # Optional. The personal arc behind the résumé, used only by the
    # "narrative" letter framing. Keep this to the shape of the story — how you
    # learn, what you built while doing it. Anything you'd only disclose to a
    # specific employer belongs in that one letter, typed by you, not here:
    # this file feeds every letter the tool generates.
    narrative: str = ""
    projects: list[dict] = field(default_factory=list)
    work_experience: list[dict] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    # Optional context for cover-letter writers. Empty is fine — the export
    # just omits the section. Do not put anything here you would not want in
    # every letter; this file is the briefing, not a per-employer disclosure.
    education: str = ""
    major: str = ""
    coursework: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    accomplishments: list[str] = field(default_factory=list)

    resume_path: str = ""
    work_authorized: bool = True
    requires_sponsorship: bool = False

    # Voluntary EEO answers. Autofill uses a value here ONLY if you put one
    # here; an empty string means the field is left untouched for you to answer
    # on the form, every time. Blank is the default for every key.
    #
    # Leave a key blank when the answer isn't fixed — because it varies, or
    # because you want to decide in the moment for that particular employer.
    eeo: dict = field(default_factory=dict)

    # Weighted random EEO answers, for questions where more than one answer is
    # true of you and you would rather not decide each time. Format:
    #   {"race": [["Black or African American", 60], ["Two or more races", 40]]}
    # Every option listed must be an answer you would give honestly — this
    # picks between truths, it does not invent one. Overrides `eeo` for that key.
    eeo_random: dict = field(default_factory=dict)

    # Answers to the recurring custom questions these forms ask. Keys are
    # matched (case-insensitively, as substrings or regexes) against the
    # field's label; the first match wins, so put specific patterns first.
    custom_answers: dict = field(default_factory=dict)

    # Consent boxes: privacy policies, interview-recording opt-ins, SMS
    # preferences. Same bargain as `eeo` — blank means the tool leaves the box
    # alone and you tick it yourself, an entry here means you have decided
    # once instead of on every form. Valid keys are the ones in
    # autofill.AGREEMENT_FIELDS; a typo is reported rather than ignored.
    #
    # Affirmations about YOUR OWN AUTHORSHIP of the application ("everything I
    # submit reflects my own work") are deliberately not settable here. Whether
    # that is true depends on how much of a generated draft you rewrote, which
    # is not something this file can know on your behalf.
    agreements: dict = field(default_factory=dict)

    # Zip / postal code. Its own field rather than a custom answer because
    # forms ask for it under a dozen labels and it is not sensitive.
    postal_code: str = ""

    target_roles: list[str] = field(default_factory=list)
    excluded_companies: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def missing_fields(self) -> list[str]:
        """Fields still empty or holding template text."""
        missing = []
        for key in ("first_name", "last_name", "email", "phone", "city"):
            val = str(getattr(self, key, "")).strip().lower()
            if val in PLACEHOLDERS:
                missing.append(key)
        if not self.skills:
            missing.append("skills")
        if not (self.projects or self.work_experience):
            missing.append("projects or work_experience")
        if not self.background.strip():
            missing.append("background")
        resume = str(self.resume_path).strip().lower()
        if resume in PLACEHOLDERS:
            missing.append("resume_path")
        elif not Path(self.resume_path).exists():
            missing.append(f"resume_path (file not found: {self.resume_path})")
        return missing

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields()

    def save(self, path: Path | None = None) -> Path:
        config.ensure_dirs()
        path = path or config.PROFILE_PATH
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path


def load(path: Path | None = None) -> Profile:
    path = path or config.PROFILE_PATH
    if not path.exists():
        return Profile()
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f for f in Profile.__dataclass_fields__}
    return Profile(**{k: v for k, v in data.items() if k in known})


def scaffold(path: Path | None = None) -> Path:
    """Write a profile template with instructions, without overwriting a real one."""
    path = path or config.PROFILE_PATH
    if path.exists():
        existing = load(path)
        if existing.is_complete:
            return path

    template = Profile(
        first_name="",
        last_name="",
        email="",
        phone="",
        city="Los Angeles",
        state="CA",
        linkedin_url="",
        github_url="",
        portfolio_url="",
        background=(
            "2-4 sentences: what you build, what you're good at, what you're "
            "looking for. Written for a human, not a keyword filter. Lead with "
            "shipped work rather than schooling."
        ),
        projects=[
            {
                "name": "",
                "url": "",
                "stack": [],
                "description": "What it does, what you built, what it proves you can do.",
                "impact": "Users, scale, measured results — anything concrete.",
            }
        ],
        work_experience=[
            {"company": "", "title": "", "start_date": "", "end_date": "", "description": ""}
        ],
        skills=["Python", "SQL", "pandas"],
        resume_path=str(config.RESUME_DIR / "resume.pdf"),
        eeo={
            "gender": "",
            "hispanic_ethnicity": "",
            "race": "",
            "veteran_status": "",
            "disability": "",
        },
        target_roles=[
            "Software Engineer",
            "Data Engineer",
            "Data Analyst",
            "Analytics Engineer",
            "Machine Learning Engineer",
        ],
    )
    return template.save(path)

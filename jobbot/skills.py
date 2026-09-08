"""Skill overlap between a posting and your profile.

Without this, scoring ranks on job title and location alone, which is how a
Swift/iOS posting outscored a role asking for LightGBM, XGBoost, tabular
classification, and production Python — a near-literal description of the
candidate's own work.

Two signals come out of a posting:
  * overlap  — technologies it wants that you have
  * foreign  — technologies it leans on that you don't

Foreign tech only counts when it is load-bearing (named in the title, or
repeated in the body). A passing mention of Kubernetes in a list of nice-to-
haves should not sink an otherwise good match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Canonical name -> regex alternatives. Kept explicit rather than fuzzy: a
# false "you know Go" from matching the word "go" is worse than a miss.
TECH_VOCAB: dict[str, str] = {
    # Languages
    "python": r"\bpython\b",
    "sql": r"\bsql\b",
    "bash": r"\bbash\b|\bshell scripting\b",
    "java": r"\bjava\b(?!script)",
    "javascript": r"\bjavascript\b|\bjs\b",
    "typescript": r"\btypescript\b",
    "go": r"\bgolang\b|\bgo\b(?= programming| language| services)",
    "rust": r"\brust\b",
    "c++": r"c\+\+",
    "c#": r"\bc#\b|\.net\b",
    "scala": r"\bscala\b",
    "ruby": r"\bruby\b",
    "php": r"\bphp\b",
    "swift": r"\bswift\b",
    "kotlin": r"\bkotlin\b",
    "objective-c": r"objective[- ]c",
    "r": r"\bR\b(?= programming)",
    # Mobile / frontend
    "ios": r"\bios\b",
    "android": r"\bandroid\b",
    "react": r"\breact\b(?!ive)",
    "react native": r"react native",
    "vue": r"\bvue(?:\.js)?\b",
    "angular": r"\bangular\b",
    "css": r"\bcss\b",
    "html": r"\bhtml\b",
    "frontend": r"\bfront[- ]?end\b",
    "design systems": r"design system",
    # Backend / web
    "django": r"\bdjango\b",
    "fastapi": r"\bfastapi\b",
    "flask": r"\bflask\b",
    "rest": r"\brest(?:ful)? api|\brest\b",
    "graphql": r"\bgraphql\b",
    "grpc": r"\bgrpc\b",
    "microservices": r"microservices?",
    # Data stores
    "postgresql": r"\bpostgres(?:ql)?\b",
    "mysql": r"\bmysql\b",
    "sqlalchemy": r"\bsqlalchemy\b",
    "mongodb": r"\bmongo(?:db)?\b",
    "redis": r"\bredis\b",
    "snowflake": r"\bsnowflake\b",
    "bigquery": r"\bbigquery\b",
    "spark": r"\bspark\b|pyspark",
    "kafka": r"\bkafka\b",
    "airflow": r"\bairflow\b",
    "dbt": r"\bdbt\b",
    # ML
    "pytorch": r"\bpytorch\b|\btorch\b",
    "tensorflow": r"\btensorflow\b",
    "scikit-learn": r"scikit[- ]learn|\bsklearn\b",
    "lightgbm": r"\blightgbm\b",
    "xgboost": r"\bxgboost\b",
    "catboost": r"\bcatboost\b",
    "gradient boosting": r"gradient[- ]boost|boosted (?:decision )?trees|\bgbdt\b",
    "gaussian processes": r"gaussian process",
    "bayesian": r"\bbayesian\b",
    "time-series": r"time[- ]series",
    "forecasting": r"\bforecasting\b",
    "nlp": r"\bnlp\b|natural language processing",
    "computer vision": r"computer vision|\bcv\b",
    "llm": r"\bllm\b|large language model|\bgpt\b|\bopenai\b|\banthropic\b",
    "recommendation systems": r"recommend(?:ation|er) system|personalization",
    "deep learning": r"deep learning",
    "mlops": r"\bmlops\b|model (?:monitoring|serving|deployment)",
    "mlflow": r"\bmlflow\b",
    "feature engineering": r"feature engineering",
    "ab testing": r"\ba/b test|experimentation platform",
    # Data tooling
    "pandas": r"\bpandas\b",
    "numpy": r"\bnumpy\b",
    "scipy": r"\bscipy\b",
    "etl": r"\betl\b|\belt\b|data pipeline",
    "plotly": r"\bplotly\b",
    "matplotlib": r"\bmatplotlib\b",
    "tableau": r"\btableau\b",
    "looker": r"\blooker\b",
    # Infra
    "aws": r"\baws\b|amazon web services",
    "gcp": r"\bgcp\b|google cloud",
    "azure": r"\bazure\b",
    "docker": r"\bdocker\b|containeriz",
    "kubernetes": r"\bkubernetes\b|\bk8s\b",
    "terraform": r"\bterraform\b",
    "ci/cd": r"\bci/cd\b|\bcicd\b|continuous integration",
    "github actions": r"github actions",
    "git": r"\bgit\b(?!hub)",
    "testing": r"\bpytest\b|unit test|integration test|test automation|end[- ]to[- ]end test|\be2e\b",
}

# Tech that defines what a role *is*. A mismatch here is disqualifying in a way
# that a missing tool is not — you can learn Terraform on the job; you cannot
# quietly not know Swift on an iOS team.
CORE_TECH = {
    "swift", "ios", "objective-c", "kotlin", "android", "react native",
    "react", "vue", "angular", "frontend", "design systems", "css",
    "java", "c++", "c#", "scala", "ruby", "php", "rust", "go",
    "computer vision", "nlp",
}

_COMPILED = {name: re.compile(pat, re.IGNORECASE) for name, pat in TECH_VOCAB.items()}


@dataclass
class SkillMatch:
    overlap: list[str] = field(default_factory=list)
    foreign_core: list[str] = field(default_factory=list)
    points: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"overlap": self.overlap, "foreign_core": self.foreign_core}


def canonicalize(skills: list[str]) -> set[str]:
    """Map free-text profile skills onto the canonical vocabulary."""
    blob = " ".join(skills).lower()
    return {name for name, pat in _COMPILED.items() if pat.search(blob)}


def extract(text: str) -> dict[str, int]:
    """Canonical tech mentioned in a posting, with mention counts."""
    found: dict[str, int] = {}
    for name, pat in _COMPILED.items():
        n = len(pat.findall(text or ""))
        if n:
            found[name] = n
    return found


def compare(
    title: str,
    description: str,
    profile_skills: set[str],
    max_bonus: int = 30,
    max_penalty: int = 40,
) -> SkillMatch:
    """Score how well a posting's stack lines up with yours."""
    result = SkillMatch()
    if not profile_skills:
        return result

    title_lc = (title or "").lower()
    posting = extract(f"{title}\n{description}")
    if not posting:
        return result

    result.overlap = sorted(set(posting) & profile_skills)
    if result.overlap:
        # Diminishing returns: matching eight tools is not twice as meaningful
        # as matching four.
        bonus = min(max_bonus, 4 * len(result.overlap))
        result.points += bonus
        shown = ", ".join(result.overlap[:6])
        result.reasons.append(f"stack overlap ({len(result.overlap)}): {shown} (+{bonus})")

    # Foreign tech only matters when it is central to the role.
    for name in sorted(set(posting) - profile_skills):
        if name not in CORE_TECH:
            continue
        in_title = _COMPILED[name].search(title_lc) is not None
        prominent = posting[name] >= 3
        if in_title or prominent:
            result.foreign_core.append(name)

    if result.foreign_core:
        penalty = min(max_penalty, 20 * len(result.foreign_core))
        # A mismatch named in the title is the role's whole identity.
        if any(_COMPILED[n].search(title_lc) for n in result.foreign_core):
            penalty = max_penalty
        result.points -= penalty
        result.reasons.append(
            f"stack you don't have: {', '.join(result.foreign_core)} (-{penalty})"
        )

    return result

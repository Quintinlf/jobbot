"""Learn from what came back, and say so honestly at every sample size.

The temptation with a rejection log is to fit a model on the first handful of
rows and start acting on the coefficients. With eight applications and one
reply, a logistic regression will still return numbers — confident,
well-formatted, and meaningless. Acting on those is worse than acting on
nothing, because it feels like evidence.

So this module has three modes and picks by sample size, never by request:

    stated reasons   any n     what rejection emails literally said
    descriptive      n >= 8    per-feature rates with Wilson intervals
    model            n >= 25 and >= 5 positives, cross-validated

The floors are deliberately visible in the report. When the model has not
earned the right to speak yet, the report says how many more outcomes it needs
instead of printing a coefficient table.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from jobbot import outcomes

# Fitting floors. Not tuned — chosen so that the smallest cell in a two-way
# split has a chance of holding more than one example.
MIN_OUTCOMES = 25
MIN_POSITIVES = 5
MIN_DESCRIPTIVE = 8

NUMERIC = (
    "score", "skill_overlap", "skill_foreign", "letter_words",
    "description_words", "days_known_before_applying", "posting_age_days",
    "years_required",
)
BOOLEAN = ("has_letter", "acknowledged")
CATEGORICAL = (
    "gate", "worksite", "ats", "title_level", "location_bucket", "letter_framing",
)

# Features you can actually change before pressing submit. A coefficient on
# `description_words` is interesting; a coefficient on `letter_framing` is a
# decision. Only the second kind becomes advice.
ACTIONABLE = {
    "letter_framing", "has_letter", "letter_words", "days_known_before_applying",
    "gate", "location_bucket", "worksite", "score", "skill_overlap", "title_level",
}


@dataclass
class Split:
    """One feature value and how applications carrying it actually did."""
    feature: str
    value: str
    n: int
    advanced: int
    rejected: int
    ghosted: int

    @property
    def rate(self) -> float:
        return self.advanced / self.n if self.n else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.advanced, self.n)


@dataclass
class Report:
    n_applications: int = 0
    n_outcomes: int = 0
    counts: dict = field(default_factory=dict)
    reconstructed: int = 0
    pending: int = 0
    reasons: dict = field(default_factory=dict)
    reason_examples: dict = field(default_factory=dict)
    splits: list = field(default_factory=list)
    model: dict | None = None
    blocked: str = ""
    advice: list = field(default_factory=list)

    @property
    def advanced(self) -> int:
        return self.counts.get(outcomes.INTERVIEW, 0) + self.counts.get(outcomes.OFFER, 0)

    def base_rate(self) -> float:
        return (self.advanced / self.n_outcomes) if self.n_outcomes else 0.0


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — the honest error bar on a rate from 6 samples.

    The naive interval puts 0/6 at exactly 0% with no width, which reads as
    "this never works" when it means "we have barely looked".
    """
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


# ── Descriptive layer ──────────────────────────────────────────────────────────

def _bucket(feature: str, value) -> str | None:
    """Render a feature value as a comparable label, or None to skip it."""
    if value is None:
        return None
    if feature in CATEGORICAL:
        return str(value) or "unknown"
    if feature in BOOLEAN:
        return "yes" if value else "no"
    if feature == "score":
        return "80+" if value >= 80 else "60-79" if value >= 60 else "under 60"
    if feature == "letter_words":
        if not value:
            return "no letter"
        return "300+" if value >= 300 else "200-299" if value >= 200 else "under 200"
    if feature == "days_known_before_applying":
        return "same day" if value <= 1 else "2-7 days" if value <= 7 else "8+ days"
    if feature == "posting_age_days":
        return "fresh (<7d)" if value <= 7 else "7-30d" if value <= 30 else "30d+"
    if feature == "skill_overlap":
        return "5+" if value >= 5 else "2-4" if value >= 2 else "0-1"
    if feature == "years_required":
        return "0-1 yrs" if value <= 1 else "2-3 yrs" if value <= 3 else "4+ yrs"
    return None


def splits(rows: list[dict], min_cell: int = 2) -> list[Split]:
    """Per-feature outcome rates. Cells thinner than min_cell are dropped."""
    tally: dict[tuple[str, str], dict] = {}
    for row in rows:
        feats = row["features"]
        for feature in (*NUMERIC, *BOOLEAN, *CATEGORICAL):
            label = _bucket(feature, feats.get(feature))
            if label is None:
                continue
            cell = tally.setdefault((feature, label),
                                    {"n": 0, "advanced": 0, "rejected": 0, "ghosted": 0})
            cell["n"] += 1
            if row["advanced"]:
                cell["advanced"] += 1
            elif row["outcome"] == outcomes.GHOSTED:
                cell["ghosted"] += 1
            else:
                cell["rejected"] += 1

    out = [
        Split(feature, value, c["n"], c["advanced"], c["rejected"], c["ghosted"])
        for (feature, value), c in tally.items()
        if c["n"] >= min_cell
    ]
    out.sort(key=lambda s: (s.feature, -s.n))
    return out


# ── Model layer ────────────────────────────────────────────────────────────────

def encode(rows: list[dict]):
    """Build the design matrix. Returns (X, y, names) or raises ImportError.

    Categorical levels seen fewer than twice collapse into `other`: a level
    with one example cannot teach the model anything except which row it was.
    """
    import numpy as np

    levels: dict[str, dict[str, int]] = {}
    for feature in CATEGORICAL:
        counts: dict[str, int] = {}
        for row in rows:
            value = str(row["features"].get(feature) or "unknown")
            counts[value] = counts.get(value, 0) + 1
        levels[feature] = {v: n for v, n in counts.items() if n >= 2}

    medians: dict[str, float] = {}
    for feature in NUMERIC:
        values = [
            row["features"].get(feature) for row in rows
            if row["features"].get(feature) is not None
        ]
        medians[feature] = float(np.median(values)) if values else 0.0

    names: list[str] = []
    for feature in NUMERIC:
        names.append(feature)
        if any(row["features"].get(feature) is None for row in rows):
            names.append(f"{feature}__missing")
    names.extend(BOOLEAN)
    for feature in CATEGORICAL:
        for value in sorted(levels[feature]):
            names.append(f"{feature}={value}")
        if len(levels[feature]) < len({
            str(r["features"].get(feature) or "unknown") for r in rows
        }):
            names.append(f"{feature}=other")

    matrix = []
    for row in rows:
        feats = row["features"]
        vector: list[float] = []
        for feature in NUMERIC:
            raw = feats.get(feature)
            vector.append(float(raw) if raw is not None else medians[feature])
            if f"{feature}__missing" in names:
                vector.append(1.0 if raw is None else 0.0)
        for feature in BOOLEAN:
            vector.append(1.0 if feats.get(feature) else 0.0)
        for feature in CATEGORICAL:
            value = str(feats.get(feature) or "unknown")
            known = value in levels[feature]
            for level in sorted(levels[feature]):
                vector.append(1.0 if value == level else 0.0)
            if f"{feature}=other" in names:
                vector.append(0.0 if known else 1.0)
        matrix.append(vector)

    X = np.array(matrix, dtype=float)
    y = np.array([1 if row["advanced"] else 0 for row in rows], dtype=int)
    return X, y, names


def fit(rows: list[dict]) -> dict:
    """Fit and honestly cross-validate. Caller checks the floors first."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    X, y, names = encode(rows)

    # Strong regularization on purpose. With more features than applications,
    # an unregularized fit memorises the rows and reports perfect accuracy.
    def build():
        return make_pipeline(
            StandardScaler(),
            # L2 is the default; naming it explicitly is deprecated in
            # newer scikit-learn and warns on every fit.
            LogisticRegression(
                C=0.3, class_weight="balanced",
                max_iter=2000, solver="liblinear",
            ),
        )

    folds = max(2, min(5, int(y.sum()), int((1 - y).sum())))
    auc = None
    try:
        cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=0)
        probs = cross_val_predict(build(), X, y, cv=cv, method="predict_proba")[:, 1]
        auc = float(roc_auc_score(y, probs))
    except ValueError:
        auc = None  # a fold came out single-class; report no score rather than a fake one

    model = build().fit(X, y)
    coefs = model[-1].coef_[0]
    ranked = sorted(
        ({"name": n, "weight": float(w)} for n, w in zip(names, coefs)),
        key=lambda c: abs(c["weight"]),
        reverse=True,
    )
    return {
        "n": len(rows),
        "positives": int(y.sum()),
        "features": len(names),
        "cv_auc": auc,
        "cv_folds": folds,
        "base_rate": float(y.mean()),
        "coefficients": ranked,
        "estimator": model,
        "names": names,
    }


def _root_feature(name: str) -> str:
    return name.split("=")[0].split("__")[0]


def recommendations(report: Report, limit: int = 6) -> list[str]:
    """Turn evidence into things to do differently on the next application."""
    out: list[str] = []

    # Stated reasons first. They need no sample size to be worth acting on.
    for reason, n in sorted(report.reasons.items(), key=lambda kv: -kv[1]):
        if reason == "stronger_candidates":
            continue  # says nothing; it is the polite default
        if reason == "degree" and n >= 1:
            out.append(
                f"{n} rejection(s) named the degree requirement explicitly. Those "
                f"postings were mis-gated — check `jobbot.gating` classified them "
                f"as hard_degree, and stop queueing that gate."
            )
        elif reason == "experience" and n >= 2:
            out.append(
                f"{n} rejections cited experience level. Lower "
                f"config.MAX_YEARS_EXPERIENCE, currently 3."
            )
        elif reason == "work_authorization" and n >= 1:
            out.append(
                f"{n} rejection(s) cited work authorization — those postings should "
                f"have been knocked out before you spent an application on them."
            )
        elif reason == "role_closed" and n >= 2:
            out.append(
                f"{n} rejections were for roles already filled or frozen. Applying "
                f"sooner after discovery matters more than tuning the letter."
            )
        elif reason == "location" and n >= 2:
            out.append(f"{n} rejections cited location or relocation.")

    if report.model:
        weights = {}
        for coef in report.model["coefficients"]:
            root = _root_feature(coef["name"])
            if root in ACTIONABLE and abs(coef["weight"]) > 0.15:
                weights.setdefault(coef["name"], coef["weight"])
        for name, weight in list(weights.items())[:limit]:
            direction = "helps" if weight > 0 else "hurts"
            out.append(f"model: {name} {direction} (weight {weight:+.2f})")
    else:
        # No model yet — but a split with a clean separation and a non-degenerate
        # interval is still worth naming, flagged as provisional.
        for split in report.splits:
            if split.feature not in ACTIONABLE or split.n < 4:
                continue
            low, high = split.interval
            if low > report.base_rate() + 0.05:
                out.append(
                    f"provisional: {split.feature}={split.value} advanced "
                    f"{split.advanced}/{split.n} ({split.rate:.0%}, CI {low:.0%}-{high:.0%})"
                )
    return out[:limit]


def analyze(conn, include_ghosted: bool = True) -> Report:
    """Everything known about what happened, at whatever depth is earned."""
    outcomes.ensure_schema(conn)
    rows = outcomes.dataset(conn, include_ghosted=include_ghosted)
    report = Report()
    report.n_applications = conn.execute(
        "SELECT COUNT(*) n FROM applications"
    ).fetchone()["n"]
    report.pending = len(outcomes.pending(conn))
    report.n_outcomes = len(rows)
    report.reconstructed = sum(1 for r in rows if r["reconstructed"])

    for row in rows:
        report.counts[row["outcome"]] = report.counts.get(row["outcome"], 0) + 1
        for signal in row["reason_signals"]:
            report.reasons[signal] = report.reasons.get(signal, 0) + 1
            report.reason_examples.setdefault(signal, f"{row['company']} — {row['title']}")

    if report.n_outcomes >= MIN_DESCRIPTIVE:
        report.splits = splits(rows)

    positives = sum(1 for r in rows if r["advanced"])
    if report.n_outcomes < MIN_OUTCOMES:
        report.blocked = (
            f"need {MIN_OUTCOMES - report.n_outcomes} more outcomes "
            f"({report.n_outcomes}/{MIN_OUTCOMES})"
        )
    elif positives < MIN_POSITIVES:
        report.blocked = (
            f"need {MIN_POSITIVES - positives} more applications that advanced "
            f"({positives}/{MIN_POSITIVES}) — a model with no positive examples "
            f"can only learn to predict rejection, which you already know"
        )
    else:
        try:
            report.model = fit(rows)
        except ImportError:
            report.blocked = "scikit-learn is not installed; pip install scikit-learn"

    report.advice = recommendations(report)
    return report


def advise(conn, external_id: str) -> dict:
    """Score one not-yet-sent application against what has been learned.

    Returns {} until a model exists. There is no useful halfway version of
    this: a per-job probability implies a precision that descriptive splits
    over a dozen rows do not have.
    """
    report = analyze(conn)
    if not report.model:
        return {"blocked": report.blocked or "no model yet"}

    row = conn.execute("SELECT * FROM jobs WHERE external_id=?", (external_id,)).fetchone()
    if row is None:
        return {"blocked": f"no job {external_id!r}"}

    candidate = {"features": outcomes.features(row), "advanced": False}
    candidate["features"]["acknowledged"] = False
    rows = outcomes.dataset(conn) + [candidate]
    X, _, names = encode(rows)
    estimator = report.model["estimator"]
    if X.shape[1] != len(report.model["names"]):
        # Adding the candidate changed the encoding (a new level crossed the
        # rare-level floor). Refit rather than force a mismatched vector in.
        refit = fit(rows[:-1])
        estimator, names = refit["estimator"], refit["names"]

    probability = float(estimator.predict_proba(X[-1:])[0][1])
    weights = dict(zip(names, estimator[-1].coef_[0]))
    scaler = estimator[0]
    contributions = []
    for i, name in enumerate(names):
        scaled = (X[-1][i] - scaler.mean_[i]) / (scaler.scale_[i] or 1.0)
        contributions.append((name, float(weights[name] * scaled)))
    contributions.sort(key=lambda c: c[1])

    return {
        "external_id": external_id,
        "company": row["company"],
        "title": row["title"],
        "probability": probability,
        "base_rate": report.model["base_rate"],
        "hurting": [c for c in contributions[:5] if c[1] < -0.05],
        "helping": [c for c in reversed(contributions[-5:]) if c[1] > 0.05],
        "cv_auc": report.model["cv_auc"],
    }


def to_json(report: Report) -> str:
    payload = {
        "applications": report.n_applications,
        "outcomes": report.n_outcomes,
        "pending": report.pending,
        "counts": report.counts,
        "reasons": report.reasons,
        "blocked": report.blocked,
        "advice": report.advice,
        "splits": [
            {"feature": s.feature, "value": s.value, "n": s.n,
             "advanced": s.advanced, "rate": round(s.rate, 3),
             "ci": [round(x, 3) for x in s.interval]}
            for s in report.splits
        ],
    }
    if report.model:
        payload["model"] = {
            k: v for k, v in report.model.items() if k not in ("estimator", "names")
        }
    return json.dumps(payload, indent=2)

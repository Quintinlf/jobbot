"""Central configuration for jobbot.

Everything tunable lives here so the pipeline modules stay logic-only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
PKG_DIR = Path(__file__).resolve().parent
BASE_DIR = PKG_DIR.parent

DATA_DIR = PKG_DIR / "data"
DB_PATH = DATA_DIR / "jobbot.db"
PROFILE_PATH = DATA_DIR / "profile.json"
COMPANIES_PATH = DATA_DIR / "companies.txt"
VERIFIED_BOARDS_PATH = DATA_DIR / "verified_boards.json"
RESUME_DIR = DATA_DIR / "resumes"
OUTBOX_DIR = DATA_DIR / "outbox"

# Legacy CSV exports from connection.ipynb (target_boards_ranked_*.csv)
LEGACY_CSV_GLOB = "target_boards_ranked_*.csv"


def _load_dotenv() -> None:
    """Pull KEY=value from internship_code/.env without requiring python-dotenv.

    Existing process env wins (`setdefault`), so a real secret in the shell is
    never overwritten by a stale file.
    """
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)


_load_dotenv()

# ── Braven Opportunity Board ───────────────────────────────────────────────────
# The shared Airtable interface page Braven publishes to members. Staff re-curate
# it weekly. If Braven rotates the share link, replace this with the new one —
# the "View All Jobs" button in the Job Board widget goes to the current URL.
# Braven publishes this Airtable view to its members, so the link is theirs
# to hand out and not this repo's to redistribute. Put it in .env as
# BRAVEN_BOARD_URL; `jobbot braven` says so if it is unset.
BRAVEN_BOARD_URL = os.environ.get("BRAVEN_BOARD_URL", "").strip()

# Airtable versions its interface layout schema and the data call must name the
# version it expects. Bump this if a fetch starts failing after Airtable ships a
# layout change.
BRAVEN_LAYOUT_VERSION = 26

# ── Network ────────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 20
REQUEST_DELAY = 0.35  # polite pause between board fetches
MAX_RETRIES = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ── Scoring ────────────────────────────────────────────────────────────────────
# Role families the user is targeting. Weight = points per matched term in title.
ROLE_TERMS: dict[str, int] = {
    # Software engineering
    "software engineer": 10,
    "software developer": 10,
    "swe": 8,
    "backend": 8,
    "back end": 8,
    "full stack": 8,
    "fullstack": 8,
    "frontend": 6,
    "front end": 6,
    "platform engineer": 7,
    "infrastructure engineer": 7,
    # ML
    "machine learning": 10,
    "ml engineer": 10,
    "applied scientist": 7,
    "ai engineer": 9,
    "deep learning": 7,
    "research engineer": 6,
    # Research and laboratory support. Added after the 21 Aug 2026 targeting
    # review: every USC and LA County lab/research posting was being knocked
    # out as "no target role match", because ROLE_TERMS was written when the
    # target was ML Engineer roles. Those are the sixth-house roles that are
    # actually reachable without a completed degree, so they belong in the
    # queue. Scored below the engineering titles, not above them.
    "research lab tech": 8,
    "lab technician": 8,
    "laboratory technician": 8,
    "lab assistant": 7,
    "laboratory assistant": 7,
    "research assistant": 8,
    "research technician": 8,
    "research coordinator": 6,
    "project assistant": 6,
    "data technician": 8,
    "data analyst": 8,
    "research associate": 7,
    # Data / analytics
    "data engineer": 10,
    "data scientist": 9,
    "data analyst": 9,
    "analytics engineer": 9,
    "business intelligence": 7,
    "analytics": 6,
    "data science": 8,
    # Infra / ops engineering
    "devops": 7,
    "site reliability": 7,
    "sre": 6,
    "cloud engineer": 7,
    "security engineer": 6,
    "qa engineer": 4,
    "test engineer": 4,
    # Generic catch-alls, deliberately low. Without these, real roles like
    # "Network Security Engineer" and "Cloud Operations Engineer" scored zero
    # and were dropped as "no target role match". The customer-facing
    # "* Engineer" titles are excluded via DEPARTMENT_KNOCKOUTS below.
    "engineer": 4,
    "developer": 4,
}

# Level signals in the title. Positive = good for this user.
LEVEL_TERMS: dict[str, int] = {
    "intern": 8,
    "internship": 8,
    "co-op": 7,
    "coop": 6,
    "apprentice": 9,
    "apprenticeship": 9,
    "residency": 8,
    # 0, not a bonus and not a penalty. A "New Grad" programme wants a degree
    # conferred in roughly the last twelve months; his SJSU transcript reads
    # "UNDERGRADUATE REQUIREMENTS NOT YET COMPLETED" and SMC has awarded none,
    # so the title is not evidence the role is reachable and should not buy
    # rank. It is not a penalty either: Stripe's New Grad posting says "or
    # equivalent practical experience" and gates EQUIVALENT_OK, which is a
    # real opening. The gate decides eligibility; this only decides preference.
    #
    # Internships keep their bonus above: he has said he wants them, and the
    # ones that genuinely require enrolment now classify ENROLLMENT and are
    # filtered on that instead of being guessed at from the title.
    "new grad": 0,
    "university grad": 0,
    "entry level": 8,
    "entry-level": 8,
    "junior": 8,
    "associate": 5,
    "i": 0,  # placeholder; handled by regex for "Engineer I"
    "trainee": 7,
}

# Hard knockouts in the title — never surface these.
TITLE_KNOCKOUTS: tuple[str, ...] = (
    "senior",
    "sr.",
    "sr ",
    "staff",
    "principal",
    "distinguished",
    "lead ",
    "manager",
    "director",
    "head of",
    "vp ",
    "vice president",
    "chief",
    "executive",
    "fellow",
    "architect",
)

# Non-technical departments that pollute board scrapes.
DEPARTMENT_KNOCKOUTS: tuple[str, ...] = (
    "counsel",
    "legal",
    "recruiter",
    "recruiting",
    "people partner",
    "human resources",
    "investor relations",
    "compensation analyst",
    "account executive",
    "sales development",
    "customer success",
    "marketing",
    "public relations",
    "office manager",
    "executive assistant",
    "didn't see what you are looking for",
    "general application",
    "talent community",
    # Customer-facing "* Engineer" titles. These are sales and support roles
    # that would otherwise be swept in by the generic "engineer" term.
    "solutions engineer",
    "sales engineer",
    "support engineer",
    "field engineer",
    "solutions consultant",
    "technical instructor",
    "implementation consultant",
    "field service engineer",
    "solution engineer",
    # Non-software engineering disciplines. "engineer" is worth +4 as a bare
    # term, and an LA location is worth +52, so on 2026-09-06 a Long Beach
    # "Harbor Marine Engineer", an LA City "Engineering Geologist Associate"
    # and a "Field Service Engineer I - 2nd Shift" were all sitting in a batch
    # of jobs to apply to. None of them are reachable with a software
    # background and none of them are the job he is looking for.
    "marine engineer",
    # Skilled trades at universities and public agencies. "engineer" is worth
    # +4 as a bare term and an LA location +52, which was enough to put
    # UCLA's "Service Engineer" -- a commercial refrigeration technician --
    # fourth in a batch of jobs to apply to.
    "refrigeration",
    "hvac",
    "plumb",
    "electrician",
    "custodial",
    "steam operating",
    "locksmith",
    "groundskeep",
    # Recruiting again, under a name the existing terms miss.
    "talent sourcer",
    "sourcer",
    # Not interested in game development (said 2026-09-07). Titles, not
    # companies -- a studio can post a plain backend role, and the
    # studios themselves are in profile.excluded_companies.
    "gameplay",
    "game designer",
    "game developer",
    "game engineer",
    "level designer",
    "technical artist",
    "operating engineer",   # building / facilities, not software
    "geologist",
    "civil engineer",
    "structural engineer",
    "mechanical engineer",
    "chemical engineer",
    "process engineer",
    "manufacturing engineer",
    # Retail / frontline
    "retail",
    "barista",
    "teller",
    "key holder",
    "cashier",
    "store associate",
)

# Trades vocabulary, checked against the DESCRIPTION and only for postings
# whose title carried no strong role term. UCLA's "Service Engineer" is a
# commercial refrigeration technician: the title matched nothing but the bare
# "engineer" (+4), the word "refrigeration" never appears in it, and an LA
# location (+52) carried it to fourth in a batch of jobs to apply to.
#
# Deliberately only applied when the title said nothing specific. A building
# automation startup hiring a Software Engineer will say "HVAC" in its
# description too, and that posting should survive.
TRADES_TERMS: tuple[str, ...] = (
    "refrigeration",
    "hvac",
    "journey-level",
    "journeyman",
    "boiler",
    "pipefitting",
    "sheet metal",
    "hand tools",
    "power tools",
    "forklift",
    "scaffold",
    "electrical wiring",
    "preventive maintenance of equipment",
)

# Posted pay bands, read as a seniority signal rather than as money.
#
# California requires the range for the level a requisition is written for, so
# the floor says which rung it was written for — and it says so more reliably
# than the title, which is how "Site Reliability Engineer" at $175K-$250K and
# "Software Engineer" at $140K-$190K end up looking identical to a scorer that
# only reads words. Blaxel posts both.
#
# Below SALARY_ENTRY_CEILING is a band an entry candidate is inside. Above
# SALARY_SENIOR_FLOOR the requisition is written for someone with years this
# profile does not have, and applying reads as not having read the posting.
# Between them is the ordinary case and costs nothing.
SALARY_ENTRY_CEILING = 150_000
SALARY_SENIOR_FLOOR = 170_000
SALARY_SENIOR_PENALTY = 18

# Years-of-experience ceiling. A posting asking for more than this is a stretch.
MAX_YEARS_EXPERIENCE = 3

# What a posting loses when no ROLE_TERM appears in its TITLE and the match came
# only from the description. Measured 2026-09-06: without this, Stripe's
# "Investigator" (a fraud/risk role that merely mentions data scientists) scored
# 87 and sat at the top of the queue, above every Software Engineer posting, and
# USC's wet-lab "Research Lab Technician" scored 78 the same way. Location, gate
# and stack words are worth ~79 points between them, so a posting whose title
# names no engineering role could outscore one that does. The description
# fallback is still worth keeping — genuinely odd titles ("Member of Technical
# Staff") are real — so this discounts those postings rather than dropping them.
OFF_TITLE_PENALTY = 20

# ── Location ───────────────────────────────────────────────────────────────────
# Higher = better. Mirrors the priority logic from connection.ipynb.
LOCATION_PRIORITY: dict[str, int] = {
    "los angeles": 40,
    "santa monica": 38,
    "pasadena": 36,
    "irvine": 34,
    "san diego": 30,
    "california": 28,
    "san francisco": 26,
    "bay area": 24,
    "remote": 30,
}

HOME_STATE = "CA"

# How much a job's work arrangement matters to you.
#   remote_only  — onsite and hybrid roles are knocked out entirely
#   prefer_remote— remote ranked well above onsite, onsite still shown
#   open         — you'd relocate for the right role; arrangement is tagged,
#                  lightly weighted, and never disqualifying
#   anywhere     — location and arrangement ignored completely
RELOCATION = "open"

# Points by work arrangement for each preference. Remote always wins ties; the
# question is how much an onsite role is penalized for requiring a move.
WORKSITE_POINTS: dict[str, dict[str, int]] = {
    "remote_only":  {"remote": 25, "hybrid": -999, "onsite": -999, "unclear": 0},
    "prefer_remote": {"remote": 25, "hybrid": -15, "onsite": -25, "unclear": 0},
    "open":         {"remote": 12, "hybrid": -4, "onsite": -6, "unclear": 0},
    "anywhere":     {"remote": 0, "hybrid": 0, "onsite": 0, "unclear": 0},
}

# Having to fund your own move is a real cost, so it is scored separately from
# the arrangement itself.
NO_RELOCATION_PENALTY = {"remote_only": 0, "prefer_remote": -8, "open": -6, "anywhere": 0}

# ...and a posting that explicitly pays for the move is worth more than one
# that is simply silent about it. Scoring only had the penalty, so an
# out-of-metro role offering relocation assistance ranked identically to one
# that says nothing — which is exactly the distinction that decides whether a
# San Francisco job is reachable for someone in Los Angeles who would move but
# cannot self-fund it. Deliberately smaller in magnitude than the penalty:
# an offer of help reduces the obstacle, it does not remove it. Zero for the
# remote-only preference, where relocation is beside the point.
RELOCATION_OFFERED_BONUS = {
    "remote_only": 0, "prefer_remote": 4, "open": 6, "anywhere": 0,
}

# Your own metro. A five-day-onsite job in Santa Monica costs you nothing to
# take, so worksite penalties must not apply here — otherwise the ranking
# quietly prefers a remote job in another state to a local one you could walk
# into. Home-metro roles get the same "no move required" credit as remote.
HOME_METRO: tuple[str, ...] = (
    "los angeles", "santa monica", "pasadena", "culver city", "burbank",
    "glendale", "long beach", "el segundo", "torrance", "inglewood",
    "manhattan beach", "playa vista", "west hollywood", "beverly hills",
    "irvine", "orange county", "downtown la",
)

NO_MOVE_BONUS = 12


def is_home_metro(location: str) -> bool:
    loc = (location or "").lower()
    return any(term in loc for term in HOME_METRO)

# "Remote - Spain" contains "remote" and would otherwise collect the full
# remote bonus for a job that needs EU work authorization. Checked before any
# location points are awarded.
NON_US_HINTS: tuple[str, ...] = (
    # Regions, not countries. Lightdash's "Analytics Engineering Advocate -
    # Europe" named no country at all and so passed every hint below it.
    "europe", "emea", "apac", "latam", "united arab emirates",
    "united kingdom", "london", "ireland", "dublin", "spain", "madrid",
    "barcelona", "germany", "berlin", "munich", "france", "paris",
    "netherlands", "amsterdam", "sweden", "stockholm", "denmark", "norway",
    "poland", "warsaw", "krakow", "portugal", "lisbon", "italy", "rome",
    "switzerland", "zurich", "austria", "belgium", "romania", "bucharest",
    "czech", "prague", "hungary", "budapest", "greece", "athens",
    "canada", "toronto", "vancouver", "montreal", "ottawa", "calgary",
    "edmonton", "winnipeg", "halifax", "waterloo", "kitchener",
    "ontario", "quebec", "british columbia", "alberta", "manitoba",
    "saskatchewan", "nova scotia", "newfoundland",
    "mexico", "brazil", "argentina", "chile", "colombia", "peru",
    "india", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "delhi",
    "china", "beijing", "shanghai", "japan", "tokyo", "korea", "seoul",
    "singapore", "taiwan", "hong kong", "philippines", "manila",
    "australia", "sydney", "melbourne", "new zealand",
    "israel", "tel aviv", "uae", "dubai", "south africa", "nigeria",
    "armenia", "luxembourg", "emea", "apac", "latam",
)


# "British Columbia, Canada" contains the substring ", ca". Matching the state
# code without a word boundary therefore marked every Canadian posting as
# American and let 11 of them into the queue, two in the top ten.
_US_SIGNAL = re.compile(
    r"\bus\b|\busa\b|united states|,\s*ca\b|\bremote\s*[-–,]?\s*us\b", re.IGNORECASE
)


# US places named often enough to stand alone in a location field, with no
# country and no state code beside them. "San Francisco" and "New York" were
# reading as "names nowhere in the US" — a gap that only surfaces once
# something depends on it, here a check that would otherwise have called San
# Francisco roles foreign.
_US_PLACES = re.compile(
    r"\b(?:san francisco|new york|nyc|brooklyn|los angeles|san diego|san jose|"
    r"santa monica|palo alto|menlo park|mountain view|sunnyvale|oakland|"
    r"seattle|portland|austin|dallas|houston|denver|boulder|chicago|boston|"
    r"cambridge|atlanta|miami|philadelphia|pittsburgh|detroit|minneapolis|"
    r"phoenix|salt lake city|nashville|charlotte|raleigh|durham|"
    r"arlington|bellevue|irvine|pasadena|long beach|culver city|marina del rey|"
    r"bay area|silicon valley|"
    r"california|texas|new jersey|massachusetts|colorado|illinois|georgia|"
    r"virginia|maryland|florida|arizona|oregon|utah|michigan|minnesota|"
    r"pennsylvania|north carolina|tennessee)\b",
    re.IGNORECASE,
)

# Country families, for reading nationality out of a description rather than a
# location field. Bree's "Machine Learning Engineer, Underwriting" listed its
# location as "Remote", scored 96 — the highest in the queue — and is a
# Canadian consumer-finance company: three mentions of Canada or Canadians and
# not one of the US. Counting "canada" alone missed it, because the word in
# the posting was "Canadians".
FOREIGN_COUNTRY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Canada", r"canad(?:a|ian|ians)\b"),
    ("India", r"\bindia\b|\bindian\b|bengaluru|bangalore"),
    ("the UK", r"united kingdom|\bbritish\b|\bbritain\b|\blondon\b"),
    ("Australia", r"australia|\bsydney\b|melbourne"),
    ("Germany", r"\bgerman(?:y|s)?\b|berlin|munich"),
    ("France", r"\bfrance\b|\bfrench\b|\bparis\b"),
    ("Brazil", r"brazil|brasil"),
    ("Mexico", r"\bmexico\b|mexican"),
    ("Singapore", r"singapore"),
    ("Japan", r"\bjapan\b|japanese|tokyo"),
)


def names_us_location(location: str) -> bool:
    """Whether this text concretely names somewhere in the US.

    Distinct from `not looks_non_us(...)`: a bare "Remote" is not non-US, but
    it does not name the US either. That gap is what lets a title or a
    description override a location field — see `scoring.score_posting`.
    """
    text = (location or "").lower()
    return bool(_US_SIGNAL.search(text) or _US_PLACES.search(text))


def foreign_country_in(description: str, min_mentions: int = 2) -> str | None:
    """The country a description belongs to, when it clearly is not the US.

    Deliberately requires both halves: a country named repeatedly AND the US
    named nowhere at all. A US company selling into Canada mentions Canada; it
    also mentions the US. Returns None on any ambiguity, because the cost of a
    false positive here is hiding a job that was reachable.
    """
    text = (description or "").lower()
    if not text or names_us_location(text):
        return None
    for name, pattern in FOREIGN_COUNTRY_PATTERNS:
        if len(re.findall(pattern, text)) >= min_mentions:
            return name
    return None


def looks_non_us(location: str) -> bool:
    loc = (location or "").lower()
    if not loc:
        return False
    # A multi-location posting that includes a US office is still reachable.
    if _US_SIGNAL.search(loc):
        return False
    return any(hint in loc for hint in NON_US_HINTS)

# ── Tailoring ──────────────────────────────────────────────────────────────────
ANTHROPIC_MODEL = "claude-sonnet-5"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"


def anthropic_key() -> str | None:
    """Return the Anthropic API key if configured, else None.

    Tailoring degrades gracefully when this is absent — the rest of the
    pipeline runs fine without it.
    """
    key = os.environ.get(ANTHROPIC_API_KEY_ENV, "").strip()
    return key or None


# ── Gmail inbox (read-only) ────────────────────────────────────────────────────
GMAIL_ADDRESS_ENV = "GMAIL_ADDRESS"
GMAIL_APP_PASSWORD_ENV = "GMAIL_APP_PASSWORD"
IMAP_HOST = "imap.gmail.com"
INBOX_LOOKBACK_DAYS = 14
INBOX_MAX_MESSAGES = 80

# Non-job classes are ignored (logged, never applied, never replied).
INBOX_SKIP_KINDS = ("agency_pitch", "newsletter", "unknown")


def gmail_address() -> str | None:
    value = os.environ.get(GMAIL_ADDRESS_ENV, "").strip()
    return value or None


def gmail_app_password() -> str | None:
    value = os.environ.get(GMAIL_APP_PASSWORD_ENV, "").strip().replace(" ", "")
    return value or None


def ensure_dirs() -> None:
    for d in (DATA_DIR, RESUME_DIR, OUTBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)

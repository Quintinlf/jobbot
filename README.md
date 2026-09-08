# jobbot

**Finds jobs you can actually get, fills the application in, and stops.**
You press Submit. It notices when you did.

A job-search pipeline for someone without a finished degree: it reads four
applicant tracking systems, works out which postings carry an automated
eligibility knockout, fills the forms it can fill honestly, and keeps an
auditable record of what was actually sent.

---

## The problem it is built around

Most new-grad and internship postings carry a knockout question — *"are you
currently enrolled?"*, *"will you graduate by June 2027?"*, *"do you have a
Bachelor's degree?"*. An application that fails one is rejected before a human
opens it. Applying harder into those postings produces silence no matter how
good the résumé is.

So the first job is not scoring. It is **classifying eligibility, with the
evidence kept**:

| Verdict | Meaning |
|---|---|
| `equivalent_ok` | "or equivalent practical experience" — the best targets |
| `open` | no degree language at all |
| `soft_degree` | degree *preferred* |
| `hard_degree` | degree required |
| `enrollment` | must be currently enrolled / graduating by a date |
| `advanced` | MS or PhD required |

The first three go in the queue. The rest are filtered, and every verdict
stores the sentence that produced it, so the decision can be argued with
instead of trusted.

## It never submits anything

Autofill fills fields. It does not press Submit, and there is no flag that
makes it. The reasons are practical rather than precious:

- Fields it must never guess at — salary expectations, date of birth, "how did
  you hear about us" — are on an explicit `NEVER_FILL` list.
- Affirmations about *your own authorship* ("everything I submit reflects my
  own work") are not settable in config at all, because whether that is true
  depends on how much of a draft you rewrote, which a config file cannot know.
- Every EEO field defaults to blank. A value is used only if you put one there.

**Applied is recorded from the page, not from a keypress.** After filling, each
tab is watched; when the page itself shows a confirmation, that application is
logged with the evidence string and the tab closes. A tab you close by hand
records nothing — closing it is not evidence in either direction.

## What it does

```
boards.py         Greenhouse / Lever / Ashby
boards_local.py   Workday (USC), NeoGov (LA County, LA City, …), Radancy (UCLA)
startups.py       finds small companies' boards from YC's public directory
gating.py         eligibility verdicts, with the evidence that produced them
worksite.py       remote / hybrid / onsite, and whether relocation is offered
scoring.py        role, level, experience ceiling, location, gate
store.py          SQLite: jobs, events, applications, outcomes
apply_session.py  what to offer next, and what not to offer again
autofill.py       Playwright form filling across four ATS layouts
inbox.py          read-only Gmail: ingest alerts, skip agency pitches
replies.py        classify what came back — rejection, interview, offer
learn.py          the outcome model, once there are enough outcomes
review_app.py     Streamlit queue
```

```bash
pip install -r jobbot/requirements.txt
python -m jobbot init                    # scaffold the profile
python -m jobbot discover-startups       # find small companies' boards
python -m jobbot hunt -n 25              # refresh, then fill 25 tabs
```

Copy `profile.example.json` to `jobbot/data/profile.json` and fill it in. That
directory is gitignored and holds everything personal: the profile, the
database, résumé PDFs, and the browser's logged-in session.

## Things that turned out to matter

Most of the work in this repo is not features. It is finding the places where
the pipeline was confidently wrong.

**Bookkeeping was deleting data.** `refresh` logged `local boards skipped` and
threw away everything it had just fetched, because a progress callback was
called with two arguments where every caller's takes three — and the resulting
`TypeError` was caught by the same `try` that wrapped the fetch. 674 postings a
run: USC, LA County, UCLA, LA City, Long Beach, Pasadena. Silently, every run.
Only the fetch is guarded now.

**The queue was ranking on signals blind to the job.** Location, gate and stack
keywords are worth ~79 points between them and none of them know what the role
is. A fraud-investigator posting that mentioned data scientists once scored 87
and outranked every Software Engineer posting. A posting whose title names no
target role is discounted now.

**It kept offering jobs already applied to.** The per-batch "one per company"
rule only deduped *within the batch it was building*, so a second requisition
at the same company looked new every run — 14 of 25 in one measured batch.

**A country in the title beat a vague location field.** "Backend Software
Engineer - India" and "Analytics Engineering Advocate - Europe" both listed
their location as "Remote", collected the full remote bonus, and came out first
and sixth in a batch.

**Auto-discovery introduced a failure a hand-written list never had.** Probing
generic slugs — `agency`, `mesh`, `flint`, `latent` — can resolve to a
stranger's board. A 50-person company came back with 829 open roles, which was a
staffing firm of the same name. A board too big for the company is rejected now.

**A newsletter was recorded as an interview.** The phrase that triggered it sat
past the stored truncation, and hard-wrapped email broke a pattern that assumed
one line. Whitespace is normalised before matching, and "about an application"
now has to look like one.

Scraped posting text is treated as data, never as instructions.

## Honest limitations

- **The gate classifier only reads the posting.** Knockout questions frequently
  live in the *application form* instead — one posting asked for a Bachelor's
  degree and 3+ years of React, and neither appeared in the description.
- **Slug discovery is a guess with a sanity check**, not a lookup. It finds
  roughly 20% of small companies' boards; the rest have none to find.
- Submission detection is conservative and will report "cannot tell" rather
  than guess. That is the safe direction, but it means some applications need
  logging by hand.
- The outcome model needs 25 outcomes before it says anything, and rejections
  arrive slowly.
- Playwright against live ATS pages is not covered by the test suite; the
  parsing, scoring, gating and bookkeeping are.

```bash
python -m pytest jobbot/tests -q
```

## Licence

MIT. See `LICENSE`.

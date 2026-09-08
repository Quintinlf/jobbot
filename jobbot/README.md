# jobbot

Finds jobs you can actually get, gets the application ready, and hands you a
queue. You press submit.

## Why this exists

The previous setup had three problems that no amount of volume would fix:

1. **The pieces weren't connected.** `connection.ipynb` scraped real jobs into
   `target_boards_ranked_*.csv`; `auto_apply_bot/` scored a five-row fake CSV.
   The good pipeline never saw the real jobs.
2. **The funnel was eight companies.** `nextdoor, stripe, vercel, servicetitan,
   owner, zest_ai, mlt, equipe`. Stripe's board alone has ~550 postings.
3. **Nothing checked whether you were eligible.** Most internship and new-grad
   postings carry an automated knockout — *"are you currently enrolled?"*,
   *"will you graduate by June 2027?"*. An application that fails one is
   rejected before a human opens it. Applying harder into those postings
   produces silence no matter how good the resume is.

Point 3 is the one this tool is built around.

## Setup

```bash
pip install -r jobbot/requirements.txt
python -m jobbot init
```

Then fill in `jobbot/data/profile.json` and drop your resume at
`jobbot/data/resumes/resume.pdf`. Both are gitignored.

## Cover letters: ChatGPT Web (primary, free)

Jobbot does not log into chatgpt.com. You paste a briefing file, ChatGPT writes
one letter per job, you import it back.

```bash
python -m jobbot export --limit 5
# paste jobbot/data/outbox/batch_YYYY-MM-DD.md into ChatGPT Web
# save the filled file, then:
python -m jobbot import-letters jobbot/data/outbox/batch_YYYY-MM-DD.md
```

Default: **one** letter per job, 250–400 words, full posting text, grounded in
`profile.json` (and a sibling `resume.txt` if you drop one next to the PDF).
Never invents experience — that rule is in the file ChatGPT sees.

The older three education framings are still available:

```bash
python -m jobbot export --limit 5 --framings all
```

You can also paste a letter in the review app (Materials → Paste a ChatGPT
letter). That writes `letter_variants.chatgpt` and selects it. Existing
non-empty letters are not overwritten unless you tick replace.

**API key.** Set `ANTHROPIC_API_KEY` and the review app can still generate per
job. Needs funding at console.anthropic.com. ChatGPT Web remains the default
writer.

**Neither.** You still get a targeting brief, never a fake letter.

Letters are never overwritten. `save_materials` merges new variants and refuses
to replace a non-empty letter unless `overwrite=True`.

## Email job alerts

```bash
# internship_code/.env  (gitignored)
# GMAIL_ADDRESS=you@gmail.com
# GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   # Google app password, not your login

python -m jobbot inbox
```

Read-only IMAP. Jobot / LinkedIn / Indeed alerts (and other mail that clearly
contains an apply URL) go through the **same** gate/score/store path as board
jobs. The same underlying role discovered twice (email + Greenhouse) collapses
to one candidate.

Career-service pitches — "we take care of your applications", "reply Learn
more", Westbury-style programs — are classified `agency_pitch`, logged, and
**never** queued, applied to, or replied to. Jobbot does not send email.

## Daily use

```bash
python -m jobbot inbox       # job-alert emails into the same queue
python -m jobbot verify      # once, and whenever you edit companies.txt
python -m jobbot refresh     # fetch every board, score, classify
python -m jobbot braven      # plus the Braven Opportunity Board
python -m jobbot review      # the queue
```

After tuning anything in `config.py` (role terms, location weights) or a gate
pattern, re-rank everything already stored without hitting the network:

```bash
python -m jobbot rescore
```

`verify` probes each slug in `data/companies.txt` against Greenhouse, Lever,
and Ashby to learn which one the company uses, and drops slugs that resolve to
nothing. Expect a chunk of the seed list to fail — slugs aren't always the
company name, and plenty of companies use Workday or iCIMS, which this doesn't
read. Failures are printed so you can correct them.

Adding a company is one line in `data/companies.txt`. No code.

## The Braven Opportunity Board

```bash
python -m jobbot braven
```

Braven curates roles into an Airtable base and publishes it as a shared
interface page. It is not an ATS — there is no per-posting API, and the grid is
drawn to a canvas, so there is nothing to scrape in the page either. What works
is the interface's own data call, which returns the whole board in one
response. It needs three things out of the share page HTML: the signed
`accessPolicy`, a `pageLoadId`, and a `csrfToken`. Without the last two it
returns 401 — even replaying a request the browser just made successfully.

If Braven rotates the share link, replace `BRAVEN_BOARD_URL` in `config.py`.
The "View All Jobs" button in the Job Board widget always points at the current
one. A restructured base fails loudly rather than returning empty postings.

**The board carries no job descriptions**, only a title, an employer, and a
link. That is a problem, because the description is the only thing gate
detection can read, and this board is 55% internships — where enrollment gates
are densest. So each posting is then fetched from its own page: Greenhouse,
Lever, and Ashby links go through their JSON APIs, everything else is read as
HTML. LinkedIn, Indeed, Glassdoor and ZipRecruiter are never fetched, for the
same reason nothing else here works around bot detection.

Roughly 55% of postings come back readable. The rest render in JavaScript —
`delta.avature.net` alone accounts for 21 — and those are the ones worth being
careful about:

> A posting with no text has no degree language in it, so it classifies as
> `open`, which is scored **+10**. Left alone, failing to read a posting earns
> it a bonus and floats it above postings that were actually checked.

So enrichment stamps `[posting text unavailable]` into the description when it
gives up. That marker makes `classify` report *"could not be read"* as its
evidence instead of returning a confident blank, and makes the scorer withhold
the `open` bonus. The posting still appears in the queue — it is labelled, not
hidden, and one click opens the real page. The marker is stored with the
description, so `rescore` reaches the same conclusion offline.

Braven's own fields are kept and shown: the application deadline, the working
model, and whether a **Braven staff referral** is available — the one thing
this board offers that a public ATS feed cannot.

## Eligibility gates

Every posting is classified, and the text that produced the call is kept and
shown in the queue, so you can overrule it:

| Gate | Meaning | In queue |
|---|---|---|
| `equivalent_ok` | "or equivalent practical experience" | Yes, ranked top |
| `open` | No degree language at all | Yes |
| `soft_degree` | Degree "preferred" | Yes |
| `hard_degree` | Degree required | Hidden |
| `advanced` | MS/PhD required | Hidden |
| `enrollment` | Must be currently enrolled | Hidden |

`equivalent_ok` gets a +25 point bonus and `enrollment` a -40 penalty. That
ordering is the whole point: it surfaces the postings where a portfolio can
carry the application, and buries the ones with a checkbox you can't tick.

Check what's being hidden with `python -m jobbot queue --include-gated`, or the
sidebar toggle in the review app. Don't trust the filter blindly — it's regex
over job descriptions, and job descriptions are written by humans.

## Skill overlap and work arrangement

Two more things the ranking accounts for, both added after the queue's
top-ranked job turned out to be unusable:

**Skill overlap** (`skills.py`) compares the posting's stack against your
profile. Without it the scorer ranked an iOS/Swift role above a job asking for
LightGBM, XGBoost, and tabular classification. Technologies you don't have only
count against a posting when they're load-bearing — named in the title or
repeated in the body — so one nice-to-have mention of Kubernetes won't sink a
good match.

**Work arrangement** (`worksite.py`) reads what the posting actually requires,
not what the location field says. "Seattle, WA, US; San Francisco, CA, US" can
still mean *commutable distance, one day a week, no relocation assistance*.
Each job is tagged remote / hybrid / onsite with days-in-office and relocation
terms.

Set `RELOCATION` in `config.py` to `remote_only`, `prefer_remote`, `open`, or
`anywhere`. Jobs in `HOME_METRO` skip worksite penalties entirely — a five-day
onsite role in Santa Monica costs you nothing to take.

## Autofill

```bash
python -m jobbot hunt -n 12                  # scrape, then fill 12 tabs
python -m jobbot prepare-applications -n 5   # several tabs, filled, you Submit
python -m jobbot apply                       # older: one tab at a time
python -m jobbot apply -n 25
python -m jobbot apply <job-id>
```

`hunt` is the whole sitting in one command: refresh the boards, pick postings
that are reachable (no degree gate, no more years than `MAX_YEARS_EXPERIENCE`,
not opened in an earlier run, one per company), and fill that many tabs. It is
`prepare-applications` with a scrape in front and one difference — it does not
require a cover letter. Requiring one caps a sitting at however many letters
exist: measured 2026-09-06, 11 of 308 reachable unopened postings had one.
Lettered postings still go first, and the batch says which is which. Add
`--no-refresh` to work from what is stored.

`prepare-applications` opens one tab per ready job (letter present), fills
everything autofill already supports from `profile.json`, pastes the cover
letter, and **stops**. You Submit on their site. Then in the terminal:

| Key | Effect |
|-----|--------|
| `y` | Marks it applied, **closes only that tab**, other prepared tabs stay |
| `s` | Skips it permanently, closes that tab |
| `n` | Next prepared tab, keeps this one open |
| `r` | Fills again, against the page as it stands |
| `u` | The page won't scroll down to Submit |
| `l` | Leaves it in the queue, closes that tab |
| `q` | Ends tracking. Remaining tabs close when the command exits |

Applied is recorded only when you type `y`. A failed fill is never marked
applied. `apply` still exists for the original one-tab sequential sitting.

Opens each posting in a real browser, fills the repetitive fields, attaches
your resume, pastes the selected cover letter, and stops. You check it, answer
what's left, tick the boxes, press Submit on their site.

After filling, the browser is left scrolled to the submit button rather than at
the top of a form you then have to hunt through.

`u` is for when the page will not scroll that far, which happens two ways and
looks identical from your side — the page just stops before the button:

- **A consent modal set `overflow: hidden`**, which locks scrolling everywhere.
  `u` restores scrolling and **leaves the banner exactly where it is** — that
  is still your decision, same as everywhere else here.
- **The form is in an iframe**, which scrolls independently of the page around
  it, so the wheel never reaches the inner document. Clicking inside the form
  first and then scrolling works; so does `u`, which scrolls the button into
  view in whichever frame actually holds it.

Failing both, `Tab` moves focus down the form and browsers scroll to follow it.

The third cause was jobbot's own fault and is fixed. The browser used to launch
with `viewport={"width": 1400, "height": 950}`, which applies a device-metrics
override — the page is rendered into an emulated rectangle unrelated to the
real window. Measured on this machine, that produced a **950px-tall viewport
inside an 882px window**. The page scrolls to the bottom of the viewport it
thinks it has, so the last strip of the form — the strip with the submit button
in it — sits below the visible area, and no amount of scrolling reaches it.

Nothing is locked in that state, so there is no lock to detect and nothing to
report, which is exactly how it presented: `u` did its job and said nothing was
wrong. The tell is a **scrollbar that is not against the right edge of the
window**.

It launches with `no_viewport=True` now, so the page uses the real window
(measured: 1536x674 viewport in a 1536px window, submit button reachable).
`scroll_note` also detects and explains the mismatch, in case it ever returns
by another route. None of this reproduces headlessly — with no window, there is
nothing for an emulated viewport to disagree with.

`r` exists because of cookie banners. They sit on top of the form and swallow
every click, so all the dropdowns time out. Dismissing one is a consent
decision, so this reports the banner rather than clicking it away — you dismiss
it however you like, press `r`, and everything fills. Also useful when a form
loads late.

Some companies serve a branded careers page with the ATS form in an iframe
(CircleCI's has five inputs in the page and twenty-five in a frame). The filler
finds whichever frame actually holds the form. Rewriting the link to the
canonical `job-boards.greenhouse.io` URL does not help — it redirects straight
back to the branded page.

One browser for the whole run, with a persistent profile at
`data/browser_profile/`, so cookies carry between applications and a site that
remembers you stays remembered. Jobs already applied to or skipped never come
back around.

Verified live against Greenhouse: 8 fields plus resume and cover letter.

Three things it will never do, enforced by tests:

- **Submit.** There is no submit call in `autofill.py`.
- **Tick a checkbox.** No `.check()` call either. Agreements are yours.
- **Agree, sign, or type an SSN, birth date, or salary expectation.** These are
  in `NEVER_FILL_PATTERNS` and no profile setting unlocks them.

### The EEO section

Voluntary demographic questions are answered **only** from the `eeo` map in
`profile.json`, and only where you have written an answer:

```json
"eeo": {
  "gender": "Male",
  "hispanic_ethnicity": "No",
  "veteran_status": "No",
  "race": "",
  "disability": ""
}
```

A blank value means the field is never touched — reported as "decide on the
form" and left for you, every single run. No defaulting, no "decline to
answer", no inference. Keep a key blank when the answer varies or when you want
to decide for that particular employer.

Short answers are mapped onto whatever wording the form uses, so `"No"` selects
"I am not a protected veteran" and `"He/Him"` selects "He/him/his". Options are
read only from the dropdown being filled — scoped via `aria-controls`, because
a page-wide lookup will happily hand back the country list while you are
filling an ethnicity field.

### Randomized answers

For questions where more than one answer is honestly true of you, `eeo_random`
picks between weighted options:

```json
"eeo_random": {
  "race": [["Black or African American", 60], ["Two or more races", 40]],
  "disability": [["Yes", 50], ["decline", 50]]
}
```

Every option listed must be one you would give honestly — this chooses among
truths, it does not manufacture one. Note the disability pool is
`{Yes, decline}` and not `{Yes, No}`: these forms sit above a certification
that your answers are true, so an answer of "No" from someone with a
disability would be a false statement. Weighted choice between "yes" and
"prefer not to say" keeps the decision open without ever asserting something
untrue.

### Custom questions

`custom_answers` maps label patterns onto answers for the questions every form
asks in its own words:

```json
"custom_answers": {
  "pronoun": "He/Him",
  "sponsorship": "No",
  "which u.s. state|state you reside": "California",
  "how did you first learn|how did you hear": "Other",
  "previously been employed": "No"
}
```

Keys are regexes matched against the field's label, first match wins. Nothing
in `NEVER_FILL_PATTERNS` is reachable this way, and EEO questions are handled
by their own pass so a broad pattern here cannot answer one.

Because these forms reveal fields progressively (Greenhouse renders the race
question only after the Hispanic/Latino one is answered), a final sweep runs at
the end so nothing waiting on you goes unreported.

Field matching is heuristic and forms vary. Every run prints what it filled,
what it skipped, and what it couldn't match — expect roughly five custom
questions per form still to answer by hand.

### When submission fails

Greenhouse forms carry a reCAPTCHA on submit — a second reason automated
submission was never on the table.

If a form rejects an email verification code you copied correctly, or reports
that it cannot reach the reCAPTCHA service, the captcha token has expired.
They last roughly two minutes, and the gap between filling a form, reviewing
it, requesting a code, and fetching that code from your inbox is easily longer
than that. Reload the page, press `r` to fill it again, then submit promptly.

(Measured: bundled Chromium and real Chrome both load reCAPTCHA correctly —
`grecaptcha`, the iframe, and the response field are all present in each. The
browser is not the problem here. `apply` still prefers your installed Chrome
for general compatibility.)

## Postings that have closed

A job board endpoint lists every *open* posting, so a job on file that is
absent from a fresh fetch of its own board has been taken down. Nothing acted
on that until an expired CircleCI posting sat at the top of the queue at 97
points, `apply` opened it, and the page said **"Posting expired"** — served as
HTTP 200, above a list of other roles. With no form on the page, nothing
matched, and the run reported *"resume NOT attached / cover letter not
pasted"*, which reads like a filling bug rather than what it was.

Three checks now, in order of how early they catch it:

1. **`refresh` retires delisted jobs.** Anything missing from a board that
   fetched successfully is marked gone. Boards that returned nothing are
   skipped — an empty fetch is a network failure, not proof every job closed.
   The first run of this retired **1,524** postings.
2. **`enrich` reads closed-page wording.** Most boards answer 200 and say so in
   the page rather than returning 404, so status codes alone miss the common
   case.
3. **`apply` checks before filling.** It reads the rendered page in the real
   browser, and a closed posting is reported, retired, and skipped without
   asking you anything. This is the only one of the three that sees postings
   behind a bot check.

Applications already sent are never rewritten — a posting closing after you
applied is normal, and the record is history.

The marker goes into the description rather than only into `knockout`, because
`rescore` recomputes `knockout` from the description and would otherwise
resurrect the job the next time scoring is tuned.

## What it does not do

It does not submit applications. It fetches, ranks, checks eligibility, drafts
materials, and opens the application page for you.

That's deliberate on two counts. Auto-submitting to Indeed or LinkedIn means
defeating their bot detection, which violates their terms and gets accounts
banned — your existing `application_codes/indeed_auto_apply.py` uses
`undetected_chromedriver` for exactly that, and it's a liability. And blind
submission means a bad tailoring run reaches fifty companies under your name
before you notice.

The bottleneck this removes is the 20 minutes of finding, reading, and
assessing each posting. That part is now about 30 seconds.

## Measuring whether it works

```bash
python -m jobbot stats
```

Once you've marked applications as applied and logged responses, this gives a
response rate. If it stays near zero after 30-40 applications to `open` and
`equivalent_ok` postings, the problem is the resume or the projects, not the
targeting — and that's worth knowing rather than guessing at.

## Layout

```
jobbot/
  gating.py      eligibility classification  ← the important one
  scoring.py     role/level/location fit
  boards.py      Greenhouse, Lever, Ashby fetchers + ATS discovery
  braven.py      the Braven Opportunity Board (shared Airtable interface)
  enrich.py      recovers descriptions for postings that arrive as a bare link
  pipeline.py    fetch → score → store, plus legacy CSV import
  store.py       SQLite, application lifecycle
  profile.py     your details, with validation
  inbox.py       read-only Gmail job alerts
  identity.py    cross-source URL / fingerprint matching
  tailor.py      cover-letter prompts; ChatGPT Web is the writer via export
  batch.py       ChatGPT-ready export / import
  apply_session.py  apply/prepare selection and Applied bookkeeping
  review_app.py  Streamlit queue
  __main__.py    CLI
  tests/
```

## Tests

```bash
python -m pytest jobbot/tests/ -q
```

Most of these are regressions found by auditing the classifier against 12,363
real postings rather than invented cases. The ones worth knowing about:

- Real postings write `Bachelor’s` with a curly apostrophe. `bachelor'?s`
  doesn't match it, which classified 204 of 400 sampled postings as having no
  degree language at all.
- Greenhouse returns entity-encoded markup, so HTML tags survived into the
  description text as literal `<li>`.
- "a **high degree** of autonomy" must not read as degree language.
- `Remote - Spain` contains "remote" and collected the full remote bonus.
- `applied → interview` must not stop counting as an application, or the
  response rate silently inflates.

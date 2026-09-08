"""Command line entry point.

    python -m jobbot init      scaffold profile
    python -m jobbot verify    probe company slugs, learn each one's ATS
    python -m jobbot refresh   fetch boards, score, classify, store
    python -m jobbot braven    fetch the Braven Opportunity Board
    python -m jobbot legacy    import target_boards_ranked_*.csv exports
    python -m jobbot queue     print what's reachable right now
    python -m jobbot inbox     read-only Gmail job alerts into the same queue
    python -m jobbot export    ChatGPT-ready cover-letter batch
    python -m jobbot import-letters FILE
    python -m jobbot prepare-applications  fill several tabs; you Submit
    python -m jobbot outcome   log a rejection / interview / offer
    python -m jobbot applications  what went out and what came back
    python -m jobbot learn     what the outcomes teach, once they can teach it
    python -m jobbot stats     funnel + response rate
    python -m jobbot talismans the consecrated works in consecrations/
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path

from jobbot import (
    apply_session, batch, boards, config, consecrations, evidence, inbox, learn,
    outcomes, pipeline, profile as profile_mod, replies, startups, store,
    tailor,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-7s %(message)s",
)
log = logging.getLogger("jobbot")


def _tolerate_unprintable_characters() -> None:
    """Stop the Windows console's codepage from killing a run mid-fetch.

    Job titles are copied out of word processors and arrive carrying things
    like U+202F NARROW NO-BREAK SPACE. Printing one to a cp1252 console raises
    UnicodeEncodeError, which took down a 101-posting fetch at posting 18 and
    lost every description gathered up to that point. Replacing the character
    is the right trade: the run matters, the glyph does not.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # not a reconfigurable text stream
            pass


_tolerate_unprintable_characters()


def cmd_init(args) -> int:
    config.ensure_dirs()
    path = profile_mod.scaffold()
    prof = profile_mod.load()
    print(f"Profile: {path}")
    missing = prof.missing_fields()
    if missing:
        print("\nStill needs filling in before tailoring will work:")
        for field in missing:
            print(f"  - {field}")
        print(f"\nDrop your resume PDF at: {config.RESUME_DIR / 'resume.pdf'}")
    else:
        print("Profile is complete.")
    return 0


def cmd_verify(args) -> int:
    slugs = boards.load_companies()
    if not slugs:
        print(f"No slugs in {config.COMPANIES_PATH}")
        return 1

    print(f"Probing {len(slugs)} slugs across greenhouse / lever / ashby…\n")
    ok: list[str] = []
    dead: list[str] = []

    def report(slug: str, ats: str | None, count: int) -> None:
        if ats:
            ok.append(slug)
            print(f"  ok    {slug:24} {ats:11} {count:4} postings")
        else:
            dead.append(slug)

    verified = boards.verify_all(slugs, on_result=report)
    boards.save_verified(verified)

    print(f"\n{len(ok)} boards resolved, {len(dead)} did not.")
    if dead:
        print("\nNo board found for these — the slug is probably wrong, or the")
        print("company uses an ATS this tool does not read (Workday, iCIMS, Taleo):")
        print("  " + ", ".join(dead))
        print(f"\nEdit {config.COMPANIES_PATH} and re-run to fix them.")
    print(f"\nSaved to {config.VERIFIED_BOARDS_PATH}")
    return 0


def cmd_refresh(args) -> int:
    def on_board(slug: str, ats: str, n: int) -> None:
        print(f"  {slug:24} {ats:11} {n:4} postings")

    print("Fetching boards…")
    try:
        result = pipeline.refresh(slugs=args.only, on_board=on_board)
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1

    print(f"\n{result.summary()}")
    if result.reachable == 0 and result.fetched:
        print(
            "\nNothing reachable this run. Either the boards have no matching "
            "roles open, or every match is gated. Check with:\n"
            "  python -m jobbot queue --include-gated"
        )
    return 0


def cmd_braven(args) -> int:
    """Pull the Braven Opportunity Board into the same queue as everything else."""
    if not config.BRAVEN_BOARD_URL:
        print(
            "BRAVEN_BOARD_URL is not set. That board is published by Braven "
            "to its members, so the link is not stored here — put it in "
            ".env as:\n"
            "  BRAVEN_BOARD_URL=https://airtable.com/...\n"
        )
        return 1
    print(f"Reading {config.BRAVEN_BOARD_URL}")

    state = {"total": 0, "done": 0}

    def on_fetch(n: int) -> None:
        state["total"] = n
        print(f"  {n} postings on the board")
        if args.no_enrich:
            return
        print("\nFetching each posting's own page for the description "
              "(gates can't be detected without it)…")

    def on_enrich(posting, status: str) -> None:
        state["done"] += 1
        mark = {"ok": "  ok  ", "too thin": " thin ", "unreachable": " fail ",
                "blocked": " skip ", "gone": " gone "}[status]
        print(f"  [{state['done']:3}/{state['total']}] {mark} "
              f"{posting.company[:28]:28} {posting.title[:44]}")

    try:
        result, enriched = pipeline.refresh_braven(
            enrich_descriptions=not args.no_enrich,
            on_fetch=on_fetch,
            on_enrich=on_enrich if not args.no_enrich else None,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"\n{exc}")
        return 1

    print(f"\n{result.summary()}")

    if not args.no_enrich:
        print(enriched.summary())
        if enriched.gone:
            print(f"\n{enriched.gone} listing(s) have been taken down since Braven "
                  "added them. Those are knocked out of the queue.")
        if enriched.hosts_failed:
            worst = sorted(enriched.hosts_failed.items(), key=lambda kv: -kv[1])[:6]
            print("\nNo readable description from these hosts — they render the "
                  "posting in JavaScript, so those jobs are classified on board "
                  "metadata alone and their gate is unknown, not open:")
            for host, n in worst:
                print(f"  {n:3}  {host}")
    else:
        print(
            "\nRan without enrichment, so no posting had description text and "
            "every gate reads 'open' by default. That is not the same as "
            "ungated — re-run without --no-enrich before trusting the queue."
        )

    print("\nSee what landed with: python -m jobbot queue")
    return 0


def cmd_rescore(args) -> int:
    print("Re-scoring stored postings…")
    with store.session() as conn:
        result = pipeline.rescore(conn)
    print(result.summary())
    return 0


def cmd_legacy(args) -> int:
    result = pipeline.import_legacy()
    if not result.fetched:
        print(f"No {config.LEGACY_CSV_GLOB} files found in {config.BASE_DIR}")
        return 0
    print(result.summary())
    print(
        "\nNote: those CSV exports carry no job description, so eligibility "
        "gates could not be detected for them. Live fetches are better."
    )
    return 0


def cmd_queue(args) -> int:
    with store.session() as conn:
        # Pull extra when filtering by years, since dropping some means the
        # requested count has to be topped up from further down the ranking.
        fetch = args.limit * 3 if not args.include_high_experience else args.limit
        rows = store.queue(
            conn,
            limit=fetch,
            min_score=args.min_score,
            include_gated=args.include_gated,
        )

    house = getattr(args, "house", "any")
    if house != "any":
        from jobbot import horary

        rows = [r for r in rows
                if horary.classify(r["title"], r["description"] or "").house == house]

    if not args.include_high_experience:
        from jobbot.gating import min_years_required

        rows = [
            r for r in rows
            if not ((y := min_years_required(r["description"] or "")) is not None
                    and y > args.max_years)
        ][: args.limit]
    else:
        rows = rows[: args.limit]

    if not rows:
        print("Queue is empty. Run `python -m jobbot refresh` first.")
        return 0

    for i, row in enumerate(rows, 1):
        reasons = json.loads(row["score_reasons"] or "[]")
        print(f"\n{i:3}. [{row['score']:3} pts] {row['title']}")
        print(f"     {row['company']} · {row['location'] or 'location n/a'}")
        print(f"     gate: {row['gate']}")
        print(f"     {row['url']}")
        if args.verbose and reasons:
            for r in reasons:
                print(f"       · {r}")

    print(f"\n{len(rows)} jobs. Review them properly with: python -m jobbot review")
    return 0


def cmd_stats(args) -> int:
    with store.session() as conn:
        s = store.stats(conn)

    print(f"Jobs in database : {s['total']}")
    print(f"Knocked out      : {s['knocked_out']} (wrong role or seniority)")

    if s.get("letter_ready") is not None:
        print(f"Letters ready    : {s['letter_ready']}")
        print(f"Letters pending  : {s['letter_pending']}")

    print("\nBy eligibility gate:")
    for gate, n in sorted(s["by_gate"].items(), key=lambda kv: -kv[1]):
        print(f"  {gate or 'unknown':16} {n:5}")

    print("\nBy status:")
    for status, n in sorted(s["by_status"].items(), key=lambda kv: -kv[1]):
        print(f"  {status:16} {n:5}")

    if s["applied"]:
        rr = s["response_rate"]
        ir = s["interview_rate"]
        print(f"\nApplied          : {s['applied']}")
        print(f"Responses        : {s['responses']}  ({rr:.0%})")
        print(f"Interviews       : {s['interviews']}  ({ir:.0%})")
    else:
        print("\nNothing marked applied yet, so there's no response rate to show.")

    if consecrations.load_all():
        print("\nSee what's consecrated with: python -m jobbot talismans")
    return 0


def cmd_talismans(args) -> int:
    items = consecrations.load_all()
    if not items:
        print(f"Nothing found in {consecrations.CONSECRATIONS_DIR}.")
        print("These are read-only copies from Astrology_project — see its "
              "consecrations/README.md if the directory moved or was never synced.")
        return 0

    print(f"{len(items)} consecrated work(s) in {consecrations.CONSECRATIONS_DIR}:\n")
    for i, item in enumerate(items, 1):
        print(consecrations.render(item))
        if i < len(items):
            print()

    if args.imprint_resume:
        print()
        try:
            path = consecrations.imprint_resume()
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"Could not imprint the résumé: {exc}")
            return 1
        print(f"Imprinted {len(items)} path(s) on {path} as invisible text "
              "(present in the text layer, painted nowhere).")
    return 0


def cmd_export(args) -> int:
    prof = profile_mod.load()
    missing = prof.missing_fields()
    if missing:
        print("Profile is incomplete — letters will be thin. Missing:")
        for field in missing:
            print(f"  - {field}")
        print()

    with store.session() as conn:
        # Pull extra, then drop anything already drafted — otherwise every
        # export re-sends the same top jobs you have letters for.
        candidates = store.queue(conn, limit=args.limit * 4, min_score=args.min_score)
        rows = [r for r in candidates if not r["letter_variants"]]

        if not args.include_high_experience:
            # A "5+ years" ask only costs a posting ~12 points in score, which
            # is not enough to keep it out of the top of the queue when the
            # rest of the posting fits well — Affirm's Capital Orchestration
            # role scored 93 while asking for 5. Score is a ranking, not a
            # pass/fail; this is the actual pass/fail for a candidate who
            # genuinely does not have that experience.
            from jobbot.gating import min_years_required

            kept, skipped = [], 0
            for row in rows:
                years = min_years_required(row["description"] or "")
                if years is not None and years > args.max_years:
                    skipped += 1
                    continue
                kept.append(row)
            rows = kept
            if skipped:
                print(f"Skipped {skipped} posting(s) asking for more than "
                      f"{args.max_years} years — use --include-high-experience "
                      "to export them anyway.\n")

        rows = rows[: args.limit]

        # Named jobs go in whatever they rank. Score orders the queue by fit,
        # which is the right default and the wrong one when a posting closes on
        # Friday — a job worth applying to is worth applying to before the
        # deadline, not once it out-ranks everything else.
        if args.include:
            have = {r["external_id"] for r in rows}
            pinned = []
            for external_id in args.include:
                if external_id in have:
                    continue
                row = conn.execute(
                    "SELECT * FROM jobs WHERE external_id = ?", (external_id,)
                ).fetchone()
                if row is None:
                    print(f"No such job, skipping: {external_id}")
                elif row["letter_variants"]:
                    print(f"Already drafted, skipping: {row['title']}")
                else:
                    pinned.append(row)
            rows = pinned + rows

    if not rows:
        print(
            "Nothing new to export — every queued job already has letters.\n"
            "Run `python -m jobbot refresh` for new postings, or review what "
            "you have with `python -m jobbot review`."
        )
        return 1

    framings = args.framings
    if not framings:
        framings = None
    elif "all" in framings:
        framings = list(tailor.FRAMINGS)

    path = batch.export(prof, rows, framings=framings)
    print(f"Wrote {len(rows)} jobs to {path}\n")
    print("Next: paste that entire file into ChatGPT Web (chatgpt.com).")
    print("Write one 250-400 word letter into each jobbot:letter block,")
    print("leave the HTML comments in place, save the file, then run:")
    print(f"  python -m jobbot import-letters {path}")
    return 0


def cmd_import_letters(args) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"No such file: {path}")
        return 1

    with store.session() as conn:
        imported, skipped = batch.import_letters(path, conn)

    print(f"Imported {imported} letters.")
    if skipped:
        print(f"{skipped} job blocks were still empty and were skipped.")
    return 0


def cmd_set_framing(args) -> int:
    """Pick one education framing for every job that has it drafted."""
    with store.session() as conn:
        rows = conn.execute(
            "SELECT external_id FROM jobs WHERE letter_variants IS NOT NULL"
        ).fetchall()
        changed = skipped = 0
        for row in rows:
            if store.select_letter(conn, row["external_id"], args.framing):
                changed += 1
            else:
                skipped += 1

    print(f"Set {changed} jobs to the {args.framing!r} letter.")
    if skipped:
        print(f"{skipped} had no {args.framing!r} version drafted and were left alone.")
    print("\nYou can still switch any individual job in the review app.")
    return 0


def _prompt(question: str, default: str = "") -> str:
    try:
        return input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default


def _apply_one(page, row, prof, index: int, total: int) -> str:
    """Fill one posting and ask what happened. Returns the user's choice."""
    from jobbot import autofill

    bar = "═" * 64
    print(f"\n{bar}")
    print(f"[{index}/{total}]  {row['title']}")
    print(f"          {row['company']} · {row['location'] or 'location n/a'} "
          f"· {row['score']} pts · {row['gate']}")
    print(f"          {row['url']}")
    print(bar)

    closed: list[str] = []

    def fill(navigate: bool) -> None:
        try:
            page.bring_to_front()
            if navigate:
                response = page.goto(
                    row["url"], wait_until="domcontentloaded", timeout=45000
                )
                page.wait_for_timeout(3000)  # let the embedded ATS form settle

                status = getattr(response, "status", None)
                evidence = autofill.posting_closed(page)
                if status in (404, 410) or evidence:
                    # Don't fill a page that has no form on it, and don't make
                    # the user work out why nothing attached.
                    closed.append(evidence or f"HTTP {status}")
                    return

            report = autofill.fill_form(page, prof, row["cover_letter"] or "")
            print(report.render())
        except Exception as exc:
            print(f"\nCould not load or fill this one: {str(exc)[:200]}")
            print("The browser is still open if you want to do it by hand.")

    fill(navigate=True)

    if closed:
        print(f"\nThis posting is closed — {closed[0][:150]}")
        print("Nothing was filled. Skipping it and moving on.")
        return "closed"

    print(
        "\nNothing has been submitted. Check the fields, answer what's left,\n"
        "tick the boxes, and press Submit on their site.\n"
        "\nIf it rejects an email verification code you copied correctly, or says\n"
        "it can't reach reCAPTCHA: the captcha token expired (they last about two\n"
        "minutes). Reload the page, press [r] to fill it again, then submit\n"
        "promptly and enter the code as soon as it arrives."
    )
    while True:
        choice = _prompt(
            "\n  [y] submitted it   [s] skip this one   [r] fill again\n"
            "  [u] page won't scroll to Submit   [l] leave it, decide later   [q] quit\n> ",
            default="q",
        )
        if choice == "r":
            # Re-run against the page as it stands — after you've dismissed a
            # cookie banner, or opened a form that loaded late.
            print("\nFilling again…")
            fill(navigate=False)
            continue
        if choice == "u":
            from jobbot import autofill

            released = autofill.release_scroll_lock(page)
            label, note = autofill.reveal_submit(page)
            if released:
                print("\nReleased the scroll lock. The banner is untouched — that "
                      "decision is still yours.")
            if label:
                print(f'Scrolled to the "{label}" button.')
            elif note:
                print(f"\nStill stuck: {note}")
            else:
                print("\nNo submit button found on this page. If the form is in an "
                      "embedded frame, click inside the form first and scroll there; "
                      "otherwise Tab moves focus down and the page follows it.")
            continue
        if choice in {"y", "s", "l", "q"}:
            return choice
        print("  Please answer y, s, r, u, l, or q.")


def cmd_apply(args) -> int:
    """Work through the queue, filling each form and leaving you to submit."""
    from playwright.sync_api import sync_playwright

    prof = profile_mod.load()
    if missing := prof.missing_fields():
        print("Profile incomplete — fix these first:")
        for f in missing:
            print(f"  - {f}")
        return 1

    with store.session() as conn:
        if args.job_id:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE external_id = ?", (args.job_id,)
            ).fetchall()
        else:
            rows = apply_session.ready_rows(
                conn, count=args.count, min_score=args.min_score
            )

    if not rows:
        print(
            "Nothing ready to apply to. Either the queue is empty or the jobs\n"
            "in it have no cover letter yet — run `python -m jobbot export`\n"
            "and draft a batch first."
        )
        return 1

    return _apply_rows(rows, prof)


def _apply_rows(rows, prof) -> int:
    """Work one posting at a time in a single tab, in order.

    The body of `apply`, lifted out so the sequential flow and the batch flow
    in `_prepare_rows` sit side by side rather than being two copies of a loop
    that drifted apart.
    """
    from playwright.sync_api import sync_playwright

    total = len(rows)
    print(f"{total} application(s) queued. The browser stays open between them.")

    applied = skipped = left = closed = 0
    config.ensure_dirs()
    profile_dir = config.DATA_DIR / "browser_profile"

    with sync_playwright() as p:
        # A persistent context keeps cookies between applications, so any site
        # that remembers you stays remembered across the whole session.
        # Prefer real Chrome over Playwright's bundled Chromium, for general
        # compatibility with what these sites expect. Measured, both load
        # reCAPTCHA fine (grecaptcha object, iframe, and response field all
        # present), so this is not a captcha fix — see the note printed after
        # each fill about token expiry, which is the actual failure mode.
        context = None
        for channel in ("chrome", "msedge", None):
            try:
                context = p.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=False,
                    channel=channel,
                    # no_viewport, NOT a fixed size. Passing `viewport=` applies
                    # a device-metrics override, so the page is rendered into an
                    # emulated rectangle that has nothing to do with the real
                    # window. In a headed browser that rectangle is usually the
                    # wrong size, and the mismatch is silent: the page scrolls
                    # to the bottom of a viewport taller than the window, so the
                    # last strip of the form — where the submit button lives —
                    # sits below the visible area and no amount of scrolling
                    # brings it back. The giveaway is a scrollbar that is not
                    # against the right edge of the window.
                    #
                    # This never showed up in headless testing, because with no
                    # window there is nothing for the emulated viewport to
                    # disagree with.
                    no_viewport=True,
                    args=["--start-maximized"],
                )
                if channel:
                    print(f"Using your installed {channel}.")
                else:
                    print(
                        "Falling back to bundled Chromium — reCAPTCHA may fail "
                        "to load, and forms that use it may reject submission."
                    )
                break
            except Exception:
                continue
        if context is None:
            print("Could not start a browser.")
            return 1

        # A persistent context restores the tabs from last time. Left alone,
        # context.pages[0] can be a stale tab from the previous session — so
        # the window shows a job you already applied to while the automation
        # fills a different, hidden tab. Start from exactly one fresh tab.
        page = context.new_page()
        for stale in list(context.pages):
            if stale is not page:
                try:
                    stale.close()
                except Exception:
                    pass
        page.bring_to_front()

        for i, row in enumerate(rows, 1):
            with store.session() as conn:
                store.log_event(conn, row["external_id"], "prepared",
                                "opened and filled for review")
            choice = _apply_one(page, row, prof, i, total)

            with store.session() as conn:
                action = apply_session.record_decision(conn, row["external_id"], choice)
                if action == "closed":
                    closed += 1
                elif action == "applied":
                    applied += 1
                    print("  marked applied")
                elif action == "skipped":
                    skipped += 1
                    print("  skipped")
                elif action == "left":
                    left += 1
                    print("  left in the queue")

            if choice == "q":
                print("\nStopping here.")
                break

        context.close()

    print(f"\nApplied {applied} · skipped {skipped} · left {left} · closed {closed}")
    if closed:
        print(f"{closed} posting(s) had already closed and were retired from the "
              "queue. Run `python -m jobbot refresh` to catch the rest.")
    if applied:
        print("\nSee where you stand with: python -m jobbot stats")
    return 0


def _launch_persistent_context(p):
    """One headed Chrome with the existing persistent profile. Never headless."""
    config.ensure_dirs()
    profile_dir = config.DATA_DIR / "browser_profile"
    for channel in ("chrome", "msedge", None):
        try:
            context = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                channel=channel,
                no_viewport=True,
                args=["--start-maximized"],
            )
            if channel:
                print(f"Using your installed {channel}.")
            else:
                print(
                    "Falling back to bundled Chromium — reCAPTCHA may fail "
                    "to load, and forms that use it may reject submission."
                )
            return context
        except Exception:
            continue
    return None


def _retire_stale_pages(context):
    """Close tabs left over from a previous session, but never the last one.

    A persistent context always opens holding one page, and closing the final
    page shuts the whole browser down. Doing that before opening the first real
    tab is why `prepare-applications` died on

        TargetClosedError: BrowserContext.new_page: Target page, context or
        browser has been closed

    which reads like Chrome failed to launch. It launched fine; it was told to
    close every window it had. The single-tab `apply` path never hit this
    because it opens its new page first and closes the strays afterwards.

    Returns the surviving page so the caller can reuse it as the first tab
    instead of stranding a blank one.
    """
    live = []
    for page in list(context.pages):
        try:
            if not page.is_closed():
                live.append(page)
        except Exception:
            continue
    if not live:
        return None
    for stale in live[1:]:
        try:
            stale.close()
        except Exception:
            pass
    return live[0]


def cmd_inbox(args) -> int:
    print(
        "Reading Gmail INBOX as read-only. Job alerts are ingested, replies to "
        "your applications are logged, career-service pitches are skipped. "
        "Nothing is sent.\n"
    )
    try:
        # One fetch, two passes. The same messages carry both new postings and
        # the replies to applications already out — refetching for the second
        # pass would double the IMAP traffic to learn nothing new.
        items = inbox.fetch_gmail(days=args.days, limit=args.limit)
    except RuntimeError as exc:
        print(exc)
        return 1

    with store.session() as conn:
        result = inbox.ingest_items(items, conn=conn)
        # Confirmations first: an application the ATS acknowledged but that
        # never got logged has to exist before a later rejection can attach to
        # it. Running these the other way round drops the reply as an orphan.
        registered = replies.register_applications_from_mail(items, conn)
        replied = replies.ingest_replies(items, conn)

    if registered.recorded:
        print("Applications the employer confirmed that were not on file:")
        for external_id, _, label in registered.recorded:
            print(f"  applied    {label}")
        print()
    for item in registered.ambiguous:
        print(f"  a confirmation matched {len(item['candidates'])} jobs "
              f"({item['subject'][:60]}). Which was it?")
        for cand in item["candidates"]:
            print(f"    python -m jobbot mark-applied {cand['external_id']}"
                  f"   # {cand['company']} — {cand['title']}")
        print()

    print(result.summary())
    if result.skipped_kinds:
        print("\nNon-jobs (not queued, not replied):")
        for kind, n in sorted(result.skipped_kinds.items(), key=lambda kv: -kv[1]):
            print(f"  {kind:16} {n}")

    print(f"\nReplies: {replied.summary()}")
    for external_id, kind, label in replied.recorded:
        print(f"  {kind:9} {label}")
    for item in replied.ambiguous:
        print(f"\n  {item['kind']} from {item['sender']} — {item['why']}:")
        for cand in item["candidates"]:
            print(f"    python -m jobbot outcome {cand['external_id']} {item['kind']}"
                  f"   # {cand['company']} — {cand['title']}")

    if replied.orphans:
        print("\nReplies to applications jobbot never sent. Register them and the "
              "next inbox run will attach the outcome:")
        for item in replied.orphans:
            company = item["company"] or "COMPANY"
            print(f"\n  {item['kind']} — {item['subject']}")
            print(f"    python -m jobbot log-application --company \"{company}\" "
                  f"--title \"TITLE\"")

    print("\nSee the queue with: python -m jobbot queue")
    print("What the replies teach:   python -m jobbot learn")
    return 0


def cmd_outcome(args) -> int:
    """Log what came back on one application."""
    with store.session() as conn:
        row = outcomes.find_applied(conn, args.job)
        if row is None:
            print(f"No single application matches {args.job!r}.")
            print("Use the external_id from `python -m jobbot applications`.")
            return 1
        # A hand-typed command run twice is one event, not two. Email-sourced
        # rows dedupe on the message id; manual ones have nothing to dedupe on,
        # so the same outcome at the same stage is treated as a repeat.
        already = conn.execute(
            "SELECT 1 FROM outcomes WHERE external_id=? AND outcome=?"
            " AND COALESCE(stage,'')=?",
            (row["external_id"], args.outcome, args.stage or ""),
        ).fetchone()
        if already:
            print(f"Already logged {args.outcome} for "
                  f"{row['company']} — {row['title']}. Nothing changed.")
            print("A later stage is new history: add --stage screen / interview / final.")
            return 0

        wrote = outcomes.record(
            conn, row["external_id"], args.outcome,
            source="manual", stage=args.stage or "", detail=args.note or "",
        )

    if not wrote:
        print("Already recorded — nothing changed.")
        return 0

    print(f"Logged {args.outcome}: {row['company']} — {row['title']}")
    if args.note:
        found = outcomes.reason_signals(args.note)
        if found:
            print(f"Reasons picked out of your note: {', '.join(found)}")
    print("\nWhat it adds up to: python -m jobbot learn")
    return 0


def cmd_hack(args) -> int:
    """Shortlist hackathons and keep an honest clock on them."""
    from jobbot import hackathon as hack_mod

    items = hack_mod.load()

    if args.hack_action == "add":
        if hack_mod.find(items, args.name):
            print(f"Already tracking something matching {args.name!r}.")
            return 1
        budget = args.budget_hours
        if not budget and args.prize_value and args.odds:
            budget = hack_mod.suggest_budget(args.prize_value, args.odds)
            print(f"Budget from ${args.prize_value:g} x {args.odds:.0%} odds "
                  f"at $20/h: {budget:g} hours.")
        entry = hack_mod.Hackathon(
            name=args.name, url=args.url or "", deadline=args.deadline or "",
            prize=args.prize or "", prize_value=args.prize_value or 0.0,
            budget_hours=budget or 0.0, notes=args.notes or "",
        )
        items.append(entry)
        hack_mod.save(items)
        print(hack_mod.render(entry))
        return 0

    if args.hack_action == "start":
        entry = hack_mod.find(items, args.name)
        if entry is None:
            print(f"No single hackathon matches {args.name!r}.")
            return 1
        stopped = hack_mod.stop_all(items)
        for other in stopped:
            if other != entry.name:
                print(f"Stopped the clock on {other} first.")
        hack_mod.start(entry, note=args.note or "")
        hack_mod.save(items)
        print(f"Clock started on {entry.name}. {entry.hours_spent:.1f}h logged so far.")
        if entry.budget_hours:
            print(f"Budget {entry.budget_hours:g}h — {entry.hours_left:.1f}h left.")
        return 0

    if args.hack_action == "stop":
        running = [h for h in items if h.running]
        if not running:
            print("No clock is running.")
            return 0
        for entry in running:
            hack_mod.stop(entry)
            print(f"Stopped {entry.name}: {entry.hours_spent:.1f}h total"
                  + (f" of {entry.budget_hours:g}h" if entry.budget_hours else ""))
            if entry.budget_hours and entry.hours_spent > entry.budget_hours:
                print("  You are past the budget you set. That was the signal to "
                      "ship what you have, not to keep going.")
        hack_mod.save(items)
        return 0

    if args.hack_action == "status":
        if not items:
            print("Nothing tracked yet. Add one with:")
            print('  python -m jobbot hack add --name "X" --deadline 2026-09-15 '
                  '--prize "$1,500" --prize-value 1500 --odds 0.15')
            return 0
        for entry in sorted(items, key=lambda h: (h.status == "done",
                                                  h.days_to_deadline or 1e9)):
            print(hack_mod.render(entry))
            print()
        total = sum(h.hours_spent for h in items)
        print(f"{total:.1f} hours logged across {len(items)} hackathon(s).")
        return 0

    if args.hack_action == "done":
        entry = hack_mod.find(items, args.name)
        if entry is None:
            print(f"No single hackathon matches {args.name!r}.")
            return 1
        hack_mod.stop(entry)
        entry.status = args.state
        hack_mod.save(items)
        print(f"{entry.name} -> {entry.state if hasattr(entry, 'state') else args.state}"
              f" after {entry.hours_spent:.1f}h")
        return 0

    print("Unknown action.")
    return 1


def cmd_skills_scan(args) -> int:
    """Measure what you have actually written, for proficiency questions."""
    from pathlib import Path

    roots = [Path(r).expanduser() for r in args.root] if args.root else None
    print("Counting non-blank lines by language. Nothing is uploaded.\n")
    found = evidence.scan(roots=roots, read_markers=not args.fast)
    path = evidence.save(found)

    total = found.total_lines()
    print(f"{'skill':22}{'files':>8}{'lines':>10}{'share':>8}   level")
    print("-" * 62)
    for skill in found.ranked():
        if skill.kind != "language" or not skill.lines:
            continue
        share = skill.lines / total if total else 0
        print(f"{skill.name:22}{skill.files:>8}{skill.lines:>10}{share:>7.1%}   {skill.level}")

    frameworks = [s for s in found.ranked() if s.kind == "framework"]
    if frameworks:
        print("\nFrameworks seen in your code:")
        print("  " + ", ".join(f"{f.name} ({f.files} files)" for f in frameworks))

    print(f"\nSaved to {path}")
    print("Forms asking 'rate your proficiency' now answer from this, but only "
          "to say you have NOT used something — a line count cannot prove you "
          "are good, so a positive claim stays yours to make.")
    return 0


def cmd_mark_applied(args) -> int:
    """Record an application for a job already in the queue."""
    with store.session() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE external_id=?", (args.job,)
        ).fetchone()
        if row is None:
            print(f"No job with id {args.job!r}.")
            return 1
        if row["applied_at"]:
            print(f"Already recorded {row['applied_at'][:10]}: "
                  f"{row['company']} — {row['title']}")
            return 0
        store.set_status(conn, args.job, store.Status.APPLIED)
        if args.date:
            conn.execute("UPDATE jobs SET applied_at=? WHERE external_id=?",
                         (args.date, args.job))
            conn.execute("UPDATE applications SET applied_at=? WHERE external_id=?",
                         (args.date, args.job))

    print(f"Applied: {row['company']} — {row['title']}")
    return 0


def cmd_log_application(args) -> int:
    """Register an application made outside jobbot, so its reply can land."""
    with store.session() as conn:
        try:
            ext = outcomes.log_external_application(
                conn,
                company=args.company,
                title=args.title,
                url=args.url or "",
                location=args.location or "",
                applied_at=args.date or None,
            )
        except ValueError as exc:
            print(exc)
            return 1

    print(f"Recorded: {args.company} — {args.title}")
    print(f"  {ext}")
    print("\nIts features are thin — no posting text was captured, so the model "
          "will treat what is missing as missing rather than as zero.")
    print("Attach a reply you already have with:")
    print(f"  python -m jobbot outcome {ext} rejected --note \"...\"")
    print("Or just run `python -m jobbot inbox` and it will match by itself.")
    return 0


def cmd_applications(args) -> int:
    """Applications sent and where each one stands."""
    with store.session() as conn:
        outcomes.backfill_snapshots(conn)
        if args.sweep:
            ghosted = outcomes.sweep_ghosted(conn, days=args.ghost_after)
            if ghosted:
                print(f"Marked {len(ghosted)} silent application(s) as ghosted.\n")
        rows = conn.execute(
            """
            SELECT a.external_id, a.applied_at, a.company, a.title,
                   (SELECT o.outcome FROM outcomes o
                     WHERE o.external_id = a.external_id AND o.outcome != 'ack'
                     ORDER BY o.at DESC LIMIT 1) AS outcome
            FROM applications a ORDER BY a.applied_at DESC
            """
        ).fetchall()

    if not rows:
        print("No applications recorded yet.")
        return 0

    for row in rows:
        state = row["outcome"] or "waiting"
        print(f"{row['applied_at'][:10]}  {state:9}  {row['company']} — {row['title']}")
        print(f"{'':23}{row['external_id']}")
    print(f"\n{len(rows)} applications. Log a reply with:")
    print("  python -m jobbot outcome <external_id> rejected --note \"...\"")
    return 0


def cmd_learn(args) -> int:
    with store.session() as conn:
        outcomes.backfill_snapshots(conn)
        if args.sweep:
            outcomes.sweep_ghosted(conn, days=args.ghost_after)
        if args.advise:
            result = learn.advise(conn, args.advise)
            if result.get("blocked"):
                print(f"No per-job prediction yet: {result['blocked']}")
                return 0
            print(f"{result['company']} — {result['title']}")
            print(f"  predicted chance of advancing: {result['probability']:.0%} "
                  f"(base rate {result['base_rate']:.0%}, cv AUC "
                  f"{result['cv_auc'] if result['cv_auc'] is not None else 'n/a'})")
            for name, weight in result["hurting"]:
                print(f"  hurting  {name:34} {weight:+.2f}")
            for name, weight in result["helping"]:
                print(f"  helping  {name:34} {weight:+.2f}")
            return 0

        report = learn.analyze(conn, include_ghosted=not args.no_ghosted)

    if args.json:
        print(learn.to_json(report))
        return 0

    print(f"Applications sent   : {report.n_applications}")
    print(f"Outcomes recorded   : {report.n_outcomes}  (waiting: {report.pending})")
    if report.reconstructed:
        print(f"  of which rebuilt from the current job row, not frozen at send "
              f"time: {report.reconstructed}")
    if report.counts:
        print("\nWhat came back:")
        for kind, n in sorted(report.counts.items(), key=lambda kv: -kv[1]):
            print(f"  {kind:12} {n}")

    if report.reasons:
        print("\nReasons the rejections actually stated:")
        for reason, n in sorted(report.reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {reason:20} {n:3}   e.g. {report.reason_examples.get(reason, '')}")
    elif report.n_outcomes:
        print("\nNo rejection gave a stated reason. That is normal and not a "
              "signal — form letters say nothing on purpose.")

    if report.splits and args.verbose:
        print("\nHow each choice did (advanced / applied, 95% interval):")
        for split in report.splits:
            low, high = split.interval
            print(f"  {split.feature:26} {split.value:14} "
                  f"{split.advanced}/{split.n:<3} {split.rate:5.0%}  "
                  f"[{low:.0%}-{high:.0%}]")
    elif report.splits:
        print(f"\n{len(report.splits)} feature splits available — add -v to see them.")

    if report.model:
        model = report.model
        auc = model["cv_auc"]
        print(f"\nModel: logistic regression on {model['n']} outcomes, "
              f"{model['features']} features, {model['positives']} positive.")
        print(f"  cross-validated AUC ({model['cv_folds']} folds): "
              f"{auc:.2f}" if auc is not None else "  cross-validated AUC: n/a")
        if auc is not None and auc < 0.6:
            print("  That is close to chance. Treat the weights below as a "
                  "hypothesis, not a finding.")
        print("\n  strongest weights:")
        for coef in model["coefficients"][:10]:
            print(f"    {coef['name']:34} {coef['weight']:+.2f}")
    else:
        print(f"\nNo model yet — {report.blocked}.")
        print("Nothing is being hidden: with fewer outcomes than that, a fitted "
              "model reports the noise in your first few applications as if it "
              "were a pattern.")

    if report.advice:
        print("\nWhat to change:")
        for line in report.advice:
            print(f"  · {line}")
    return 0


def cmd_prepare_applications(args) -> int:
    """Fill several application tabs, then track human Submit per tab.

    Autofill never presses Submit. Applied is recorded only when you type y.
    Closing a tab happens only after that confirmation (or an explicit skip).
    """
    from playwright.sync_api import sync_playwright

    prof = profile_mod.load()
    if missing := prof.missing_fields():
        print("Profile incomplete — fix these first:")
        for f in missing:
            print(f"  - {f}")
        return 1

    with store.session() as conn:
        rows = apply_session.ready_rows(
            conn, count=args.count, min_score=args.min_score, job_id=args.job_id,
            include_opened=args.include_opened,
            include_applied=args.include_applied,
        )
        hidden = 0
        if not args.include_opened and not args.job_id:
            shown = {r["external_id"] for r in rows}
            hidden = len([
                r for r in apply_session.ready_rows(
                    conn, count=args.count, min_score=args.min_score,
                    include_opened=True,
                )
                if r["external_id"] not in shown
                and r["external_id"] in apply_session.already_opened(conn)
            ])

    if not rows:
        print(
            "Nothing new to prepare. Queue empty or no cover letters yet.\n"
            "Run `python -m jobbot export`, paste into ChatGPT Web, then\n"
            "`python -m jobbot import-letters <file>`."
        )
        if hidden:
            print(f"\n{hidden} posting(s) were opened in an earlier run and are "
                  "hidden. If you did not actually submit those, revisit them "
                  "with:\n  python -m jobbot prepare-applications --include-opened")
        return 1

    if hidden:
        print(f"Hiding {hidden} posting(s) already opened in an earlier run — "
              "add --include-opened to revisit them.\n")

    return _prepare_rows(rows, prof)


def _watch_prepared(prepared, poll_ms: int = 2000, idle_timeout_ms: int = 1800000):
    """Poll open tabs and record Applied when the page itself confirms it.

    What this answers: "I want it to know when I press submit instead of me
    pressing a key." The detector already existed -- `submission_state` reads
    the confirmation out of the page or its iframes -- but it was only ever
    used to second-guess a keypress that had already been typed.

    Recording on the page rather than on a keypress is also the more honest
    record. What the funnel counts is whether an employer received an
    application, not whether you believed you sent one.

    Conservative on purpose:

      CONFIRMED  log it, close that tab, move on.
      ERRORS     say so once and leave the tab open -- a form that was
                 rejected is one you still have to fix.
      anything   left alone. `submission_state` returns UNKNOWN when it
      else       cannot tell, and guessing there would either invent
                 applications or throw real ones away.

    A tab closed by hand is not evidence in either direction, so it comes back
    as unresolved rather than being assumed one way.
    """
    from jobbot import autofill

    applied = 0
    reported: set[str] = set()
    unresolved: list = []
    waited = 0

    print("\nWatching for your submissions. Submit each tab in the browser --")
    print("each one is recorded and closed on its own when its page confirms.")
    print("Ctrl-C when you are done.\n")

    try:
        while prepared and waited < idle_timeout_ms:
            for item in list(prepared):
                page, row = item["page"], item["row"]
                ext = row["external_id"]
                try:
                    if page.is_closed():
                        prepared.remove(item)
                        unresolved.append(row)
                        print(f"  · closed by hand, not recorded: "
                              f"{row['company']} {row['title'][:40]}")
                        continue
                    state, evidence = autofill.submission_state(page)
                except Exception:
                    continue

                if state == autofill.CONFIRMED:
                    with store.session() as conn:
                        apply_session.record_decision(conn, ext, "y")
                        store.log_event(conn, ext, "applied_confirmed", evidence)
                    applied += 1
                    print(f"  [applied] {row['company']} · {row['title'][:44]}")
                    print(f"            {evidence[:110]}")
                    try:
                        page.close()
                    except Exception:
                        pass
                    prepared.remove(item)
                    # Seeing one land resets the clock: you are still working.
                    waited = 0
                elif state == autofill.ERRORS and ext not in reported:
                    reported.add(ext)
                    print(f"  [rejected] {row['company']} · {row['title'][:40]}")
                    print(f"            {evidence[:140]}")

            if not prepared:
                break
            try:
                prepared[0]["page"].wait_for_timeout(poll_ms)
            except Exception:
                break
            waited += poll_ms
    except KeyboardInterrupt:
        print("\nStopped watching.")

    unresolved.extend(item["row"] for item in prepared)
    return applied, unresolved


def _prepare_rows(rows, prof, watch: bool = False) -> int:
    """Open a tab per posting, fill them all, then track your Submit per tab.

    Shared by `prepare-applications` and `hunt`, which differ only in how
    they choose the rows. Autofill never presses Submit.

    `watch` decides what counts as proof you applied. Without it, you type y
    and that is the record. With it, each tab is polled and Applied is
    recorded when the PAGE says the application landed -- which is the thing
    actually being tracked. `autofill.submission_state` already knew how to
    read that; it was only ever used to second-guess the keypress.
    """
    from playwright.sync_api import sync_playwright

    total = len(rows)
    print(f"Preparing {total} application(s). Each tab is filled, then you Submit.")
    print("Nothing is submitted automatically.\n")

    applied = skipped = left = closed = failed = 0
    # Applications recorded without the page confirming it. Kept apart so the
    # funnel can say which rows rest on a detector and which on a judgement call.
    unverified: list[str] = []

    with sync_playwright() as p:
        context = _launch_persistent_context(p)
        if context is None:
            print("Could not start a browser.")
            return 1

        # Reuse the tab the context already has rather than opening one more
        # and leaving a blank behind.
        spare = _retire_stale_pages(context)

        prepared: list[dict] = []
        for i, row in enumerate(rows, 1):
            page = None
            if spare is not None:
                try:
                    page = spare if not spare.is_closed() else None
                except Exception:
                    page = None
                spare = None
            if page is None:
                page = context.new_page()
            print(f"\n[{i}/{total}] filling  {row['title']}  @ {row['company']}")
            choice = _fill_prepared(page, row, prof)
            if choice == "closed":
                with store.session() as conn:
                    apply_session.record_decision(conn, row["external_id"], "closed")
                closed += 1
                try:
                    page.close()
                except Exception:
                    pass
                continue
            if choice == "f":
                with store.session() as conn:
                    apply_session.record_decision(conn, row["external_id"], "f")
                failed += 1
                try:
                    page.close()
                except Exception:
                    pass
                continue
            # Record that this posting was actually put in front of him. When
            # a confirmation email later matches several postings at the same
            # company, this is what separates the one on screen from the nine
            # that were never opened — see replies.match_application.
            with store.session() as conn:
                store.log_event(conn, row["external_id"], "prepared",
                                "tab filled and left open for review")
            prepared.append({"page": page, "row": row})

        if not prepared:
            print("\nNo tabs left to submit.")
            context.close()
            print(
                f"\nApplied {applied} · skipped {skipped} · left {left} · "
                f"closed {closed} · failed {failed}"
            )
            return 0

        if watch:
            applied, unresolved = _watch_prepared(prepared)
            context.close()
            print(f"\nApplied {applied} · closed {closed} · failed {failed}")
            if unresolved:
                print(f"\n{len(unresolved)} tab(s) ended without the page "
                      "confirming anything. Nothing was recorded for these — "
                      "if you did submit one, say so and it gets logged:")
                for row in unresolved:
                    print(f"  {row['company']} · {row['title'][:52]}")
            if applied:
                print("\nSee where you stand with: python -m jobbot stats")
            return 0

        print(f"\n{len(prepared)} tab(s) ready. Submit each in the browser yourself.")
        print("Then come back here and confirm. Only the confirmed tab closes.\n")

        idx = 0
        while prepared:
            item = prepared[idx]
            page, row = item["page"], item["row"]
            try:
                page.bring_to_front()
            except Exception:
                print("That tab is already gone — skipping tracking for it.")
                prepared.pop(idx)
                if idx >= len(prepared):
                    idx = 0
                continue

            print("─" * 64)
            print(f"[{idx + 1}/{len(prepared)}]  {row['title']}")
            print(f"          {row['company']} · {row['url']}")
            print("─" * 64)
            print(
                "Submit on their site, then:\n"
                "  [y] I submitted it   [s] skip (close tab)   [r] fill again\n"
                "  [u] page won't scroll to Submit   [n] next tab, keep this open\n"
                "  [l] leave in queue, close tab   [q] quit (remaining tabs close when this command exits)"
            )
            choice = _prompt("\n> ", default="q")
            if choice == "n":
                idx = (idx + 1) % len(prepared)
                continue
            if choice == "r":
                _fill_prepared(page, row, prof, navigate=False)
                continue
            if choice == "u":
                from jobbot import autofill

                released = autofill.release_scroll_lock(page)
                label, note = autofill.reveal_submit(page)
                if released:
                    print("Released the scroll lock. The banner is untouched.")
                if label:
                    print(f'Scrolled to the "{label}" button.')
                elif note:
                    print(f"Still stuck: {note}")
                continue
            if choice == "q":
                print(
                    f"\nStopping. {len(prepared)} prepared tab(s) will close "
                    "when this command exits. Decisions were not guessed."
                )
                left += len(prepared)
                break
            if choice not in {"y", "s", "l"}:
                print("  Please answer y, s, r, u, n, l, or q.")
                continue

            if choice == "y":
                # Check the page rather than take the word for it. An
                # application recorded from an intention instead of an event is
                # a phantom row in the funnel, and later a training label for
                # an outcome that could not have happened.
                from jobbot import autofill

                print("  checking the page...")
                state, evidence = autofill.wait_for_submission(page, timeout_ms=15000)

                if state == autofill.CONFIRMED:
                    print(f"  confirmed — {evidence}")
                else:
                    if state == autofill.ERRORS:
                        print(f"  NOT submitted — the form is still showing: {evidence}")
                        print("  Fix those fields, press Submit again, then answer y.")
                    elif state == autofill.FORM_OPEN:
                        print(f"  NOT submitted — {evidence}.")
                        print("  Press Submit on their site first, then answer y.")
                    else:
                        print(f"  Cannot confirm from this page ({evidence}).")
                        print("  Some boards show the receipt in a way this "
                              "cannot read, so you may well be right.")

                    # The detector does not get the last word — it cannot know
                    # every ATS. But an override is recorded as an override.
                    override = _prompt(
                        "  Record it as applied anyway? [y/N] > ", default="n"
                    )
                    if override != "y":
                        print("  Not recorded — this tab stays open.")
                        continue
                    unverified.append(row["external_id"])
                    print("  recorded on your say-so, flagged unverified")

            with store.session() as conn:
                action = apply_session.record_decision(conn, row["external_id"], choice)
                if action == "applied" and row["external_id"] in unverified:
                    store.log_event(
                        conn, row["external_id"], "applied_unverified",
                        "no confirmation could be read from the page",
                    )
            if action == "applied":
                applied += 1
                print("  marked applied — closing this tab only")
            elif action == "skipped":
                skipped += 1
                print("  skipped — closing this tab")
            elif action == "left":
                left += 1
                print("  left in the queue — closing this tab")

            try:
                page.close()
            except Exception:
                pass
            prepared.pop(idx)
            if idx >= len(prepared):
                idx = 0

        context.close()

    print(
        f"\nApplied {applied} · skipped {skipped} · left {left} · "
        f"closed {closed} · failed {failed}"
    )
    if unverified:
        print(f"{len(unverified)} of those could not be confirmed from the page "
              "and rest on your say-so.")
    if applied:
        print("See where you stand with: python -m jobbot stats")
    return 0


def _fill_prepared(page, row, prof, navigate: bool = True) -> str:
    """Fill one tab. Returns '' on success, 'closed' or 'f' on failure."""
    from jobbot import autofill

    try:
        page.bring_to_front()
        if navigate:
            response = page.goto(
                row["url"], wait_until="domcontentloaded", timeout=45000
            )
            page.wait_for_timeout(3000)
            status = getattr(response, "status", None)
            evidence = autofill.posting_closed(page)
            if status in (404, 410) or evidence:
                print(f"  closed — {evidence or status}")
                return "closed"
        report = autofill.fill_form(page, prof, row["cover_letter"] or "")
        print(report.render())
        return ""
    except Exception as exc:
        print(f"  could not fill: {str(exc)[:200]}")
        return "f"


def cmd_hunt(args) -> int:
    """One command: fetch what is newly open, then fill that many tabs.

    Exists because the sequence that actually gets applications sent —
    refresh, check the queue is not stale, prepare N tabs — was three commands
    and the middle one was easy to skip. Nothing here submits anything; it is
    `prepare-applications` with a fresh scrape in front of it.

    Cover letters are not required. `prepare-applications` only offers postings
    that already have one, which is right when a letter is the point, but it
    caps a sitting at however many letters exist: measured 2026-09-06, 11 of
    308 reachable unopened postings had one. Most Greenhouse and Lever forms
    do not ask for a letter at all, and `autofill.fill_form` handles an empty
    one by leaving the field for you. So this prefers lettered postings and
    then tops the batch up with unlettered ones, saying which is which.
    """
    if not args.no_refresh:
        rc = cmd_refresh(argparse.Namespace(only=None))
        if rc != 0:
            print("\nRefresh failed — working from what is already stored.\n")
        print()

    from jobbot.replies import normalize_company

    # Prefer postings with a letter, then top up. Asking for lettered rows
    # first (rather than sorting one combined pull) keeps the letters in the
    # batch even when unlettered postings outscore them.
    with store.session() as conn:
        lettered = apply_session.ready_rows(
            conn, count=args.count, min_score=args.min_score,
            max_years=args.max_years, require_letter=True,
            include_applied=args.include_applied,
        )
        topped_up = 0
        if len(lettered) < args.count:
            have = {r["external_id"] for r in lettered}
            companies = {
                normalize_company(r["company"] or "") for r in lettered
            }
            for row in apply_session.ready_rows(
                conn, count=args.count * 4, min_score=args.min_score,
                max_years=args.max_years, require_letter=False,
                include_applied=args.include_applied,
            ):
                if len(lettered) >= args.count:
                    break
                if row["external_id"] in have:
                    continue
                company = normalize_company(row["company"] or "")
                if company and company in companies:
                    continue
                have.add(row["external_id"])
                if company:
                    companies.add(company)
                lettered.append(row)
                topped_up += 1

    if not lettered:
        print("Nothing reachable and unopened. Try `python -m jobbot refresh`.")
        return 1

    prof = profile_mod.load()
    if missing := prof.missing_fields():
        print("Profile incomplete — fix these first:")
        for f in missing:
            print(f"  - {f}")
        return 1

    with_letter = len(lettered) - topped_up
    print(f"{len(lettered)} posting(s): {with_letter} with a cover letter, "
          f"{topped_up} without.")
    if topped_up:
        print("The ones without will have an empty cover-letter box if the form "
              "asks for one — write it there or skip the posting.")
    print()

    return _prepare_rows(lettered, prof, watch=not args.confirm_by_key)


def cmd_discover_startups(args) -> int:
    """Find Greenhouse/Lever/Ashby boards for small YC companies.

    The seed list in companies.txt is 92 name-brand employers, because it was
    typed by hand and a hand only contains companies someone has heard of.
    This asks YC's own directory who its companies are and probes the small
    ones for a board, so the queue stops being exclusively the employers with
    the worst applicant-to-opening ratio.
    """
    print(f"Reading {startups.YC_API}")
    try:
        directory = startups.fetch_yc_directory(
            on_page=lambda page, total, seen: (
                print(f"  page {page}/{total or '?'} — {seen} companies")
                if page % 25 == 0 else None
            )
        )
    except Exception as exc:
        print(f"Could not read the YC directory: {str(exc)[:200]}")
        return 1

    small = startups.small_companies(
        directory, min_team=args.min_team, max_team=args.max_team
    )
    if args.limit:
        small = small[: args.limit]
    print(f"\n{len(directory)} companies, {len(small)} active with "
          f"{args.min_team}-{args.max_team} people.\n")

    known = boards.load_verified()
    cache = startups.load_probe_cache()
    before = len(known)
    checked = 0

    def report(company, slug, ats, count) -> None:
        nonlocal checked
        checked += 1
        if ats:
            print(f"  {company['name'][:26]:28} {slug:24} {ats:11} "
                  f"{count:4} postings · {company['teamSize']} people")
        if checked % 100 == 0:
            print(f"  … {checked}/{len(small)} probed, "
                  f"{len(known) - before} boards found")
            # Save as we go. A full pass is tens of thousands of requests and
            # an interrupt must not throw away what it already learned.
            boards.save_verified(known)
            startups.save_probe_cache(cache)

    try:
        for slug, ats, _count in startups.discover_boards(
            small, known=known, cache=cache, on_result=report
        ):
            known[slug] = ats
    except KeyboardInterrupt:
        print("\nStopped — saving what was found so far.")

    boards.save_verified(known)
    startups.save_probe_cache(cache)

    found = len(known) - before
    print(f"\n{found} new board(s). {len(known)} verified boards in total "
          f"(was {before}).")
    if found:
        # companies.txt is the seed `verify` re-probes from, and `verify`
        # overwrites verified_boards.json with only what it finds there. Adding
        # the new slugs keeps the next `verify` from deleting this run's work.
        path = config.COMPANIES_PATH
        existing = set(boards.load_companies())
        added = [s for s in sorted(known) if s not in existing]
        if added:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n# Small YC companies, found {date.today()} by "
                         f"discover-startups\n")
                fh.write("\n".join(added) + "\n")
            print(f"Appended {len(added)} slug(s) to {path}.")
        print("\nPick them up with: python -m jobbot hunt -n 25")
    return 0


def cmd_review(args) -> int:
    app = Path(__file__).parent / "review_app.py"
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jobbot", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="scaffold the applicant profile").set_defaults(fn=cmd_init)
    sub.add_parser("verify", help="probe slugs to find each company's ATS").set_defaults(fn=cmd_verify)

    p_refresh = sub.add_parser("refresh", help="fetch boards, score, store")
    p_refresh.add_argument("--only", nargs="*", help="limit to these slugs")
    p_refresh.set_defaults(fn=cmd_refresh)

    p_braven = sub.add_parser(
        "braven", help="fetch the Braven Opportunity Board into the queue"
    )
    p_braven.add_argument(
        "--no-enrich", action="store_true",
        help="skip fetching each posting's page (fast, but leaves gates undetected)",
    )
    p_braven.set_defaults(fn=cmd_braven)

    sub.add_parser(
        "rescore",
        help="re-score stored postings after tuning scoring (no network)",
    ).set_defaults(fn=cmd_rescore)

    sub.add_parser("legacy", help="import target_boards_ranked_*.csv").set_defaults(fn=cmd_legacy)

    p_queue = sub.add_parser("queue", help="print reachable jobs")
    p_queue.add_argument("--limit", type=int, default=25)
    p_queue.add_argument("--min-score", type=int, default=1)
    p_queue.add_argument("--include-gated", action="store_true",
                         help="also show degree/enrollment-gated postings")
    p_queue.add_argument(
        "--max-years", type=int, default=config.MAX_YEARS_EXPERIENCE,
        help=f"drop postings asking for more years than this (default {config.MAX_YEARS_EXPERIENCE})",
    )
    p_queue.add_argument(
        "--include-high-experience", action="store_true",
        help="show postings asking for more years too, instead of dropping them",
    )
    p_queue.add_argument(
        "--house", choices=["sixth", "tenth", "any"], default="any",
        help="filter by the house classification in horary.py (sixth = service "
             "and entry roles, the house the 21 Aug 2026 chart says perfects)",
    )
    p_queue.add_argument("-v", "--verbose", action="store_true", help="show score breakdown")
    p_queue.set_defaults(fn=cmd_queue)

    p_export = sub.add_parser(
        "export", help="write a ChatGPT-ready cover-letter batch"
    )
    p_export.add_argument("--limit", type=int, default=10)
    p_export.add_argument("--min-score", type=int, default=1)
    p_export.add_argument(
        "--max-years", type=int, default=config.MAX_YEARS_EXPERIENCE,
        help=f"drop postings asking for more years than this (default {config.MAX_YEARS_EXPERIENCE})",
    )
    p_export.add_argument(
        "--include-high-experience", action="store_true",
        help="export postings asking for more years too, instead of dropping them",
    )
    p_export.add_argument(
        "--include", nargs="*", default=[], metavar="JOB_ID",
        help="always include these external_ids, whatever they rank "
             "(for postings with a near deadline)",
    )
    p_export.add_argument(
        "--framings",
        nargs="*",
        default=None,
        metavar="FRAMING",
        help="letter variants to include (default: one projects_only letter). "
             "Pass 'all' or the framing names for the older three-variant file.",
    )
    p_export.set_defaults(fn=cmd_export)

    p_import = sub.add_parser(
        "import-letters", help="read drafted letters from a batch file back in"
    )
    p_import.add_argument("path")
    p_import.set_defaults(fn=cmd_import_letters)

    p_framing = sub.add_parser(
        "set-framing", help="use one education framing for every drafted job"
    )
    p_framing.add_argument("framing", choices=list(tailor.FRAMINGS))
    p_framing.set_defaults(fn=cmd_set_framing)

    p_apply = sub.add_parser(
        "apply",
        help="work through the queue with each form pre-filled (never submits)",
    )
    p_apply.add_argument("job_id", nargs="?", help="external_id; omit to use the queue")
    p_apply.add_argument("-n", "--count", type=int, default=10,
                         help="how many to work through in one sitting (default 10)")
    p_apply.add_argument("--min-score", type=int, default=1)
    p_apply.set_defaults(fn=cmd_apply)

    p_startups = sub.add_parser(
        "discover-startups",
        help="find boards for small YC companies and add them to the queue",
    )
    p_startups.add_argument(
        "--min-team", type=int, default=startups.MIN_TEAM,
        help=f"smallest team to bother probing (default {startups.MIN_TEAM})",
    )
    p_startups.add_argument(
        "--max-team", type=int, default=startups.MAX_TEAM,
        help=f"largest team to count as small (default {startups.MAX_TEAM})",
    )
    p_startups.add_argument(
        "--limit", type=int, default=0,
        help="stop after this many companies (0 = all of them)",
    )
    p_startups.set_defaults(fn=cmd_discover_startups)

    p_hunt = sub.add_parser(
        "hunt",
        help="refresh the boards, then fill that many application tabs at once",
    )
    p_hunt.add_argument("-n", "--count", type=int, default=12,
                        help="how many tabs to prepare in one sitting (default 12)")
    p_hunt.add_argument("--min-score", type=int, default=1)
    p_hunt.add_argument(
        "--max-years", type=int, default=config.MAX_YEARS_EXPERIENCE,
        help=f"skip postings asking for more years than this "
             f"(default {config.MAX_YEARS_EXPERIENCE})",
    )
    p_hunt.add_argument(
        "--include-applied", action="store_true",
        help="also offer companies you already have an application out to "
             "(hidden by default: a second application at the same company "
             "while the first is unanswered reads as pressure, not interest)",
    )
    p_hunt.add_argument(
        "--confirm-by-key", action="store_true",
        help="go back to typing y per tab instead of watching each page "
             "for its own confirmation",
    )
    p_hunt.add_argument("--no-refresh", action="store_true",
                        help="skip the scrape and work from what is stored")
    p_hunt.set_defaults(fn=cmd_hunt)

    p_prep = sub.add_parser(
        "prepare-applications",
        help="fill several application tabs; you Submit; only the confirmed tab closes",
    )
    p_prep.add_argument("job_id", nargs="?", help="external_id; omit to use the queue")
    p_prep.add_argument("-n", "--count", type=int, default=5,
                        help="how many tabs to prepare (default 5)")
    p_prep.add_argument("--min-score", type=int, default=1)
    p_prep.add_argument(
        "--include-applied", action="store_true",
        help="also offer companies you already have an application out to",
    )
    p_prep.add_argument(
        "--include-opened", action="store_true",
        help="also show postings already opened in an earlier run (hidden by "
             "default, because an unconfirmed tab keeps its 'new' status and "
             "otherwise returns to the top of the queue forever)",
    )
    p_prep.set_defaults(fn=cmd_prepare_applications)

    p_inbox = sub.add_parser(
        "inbox",
        help="read-only Gmail: ingest job alerts, skip agency pitches",
    )
    p_inbox.add_argument("--days", type=int, default=None, help="lookback in days")
    p_inbox.add_argument("--limit", type=int, default=None, help="max messages")
    p_inbox.set_defaults(fn=cmd_inbox)

    p_outcome = sub.add_parser(
        "outcome",
        help="log what came back on an application (rejected, interview, offer)",
    )
    p_outcome.add_argument("job", help="external_id, posting url, or company/title text")
    p_outcome.add_argument("outcome", choices=list(outcomes.OUTCOMES))
    p_outcome.add_argument("--note", default="",
                           help="paste the reply; stated reasons are extracted from it")
    p_outcome.add_argument("--stage", default="",
                           help="how far it got: screen, interview, final")
    p_outcome.set_defaults(fn=cmd_outcome)

    p_hack = sub.add_parser("hack", help="hackathon shortlist and an hours clock")
    hack_sub = p_hack.add_subparsers(dest="hack_action", required=True)

    h_add = hack_sub.add_parser("add", help="track a hackathon")
    h_add.add_argument("--name", required=True)
    h_add.add_argument("--url", default="")
    h_add.add_argument("--deadline", default="", help="YYYY-MM-DD")
    h_add.add_argument("--prize", default="", help="free text, e.g. '$1,500 + hardware'")
    h_add.add_argument("--prize-value", type=float, default=0.0,
                       help="what a win is worth in dollars, for the budget maths")
    h_add.add_argument("--odds", type=float, default=0.0,
                       help="your honest chance of winning, 0-1")
    h_add.add_argument("--budget-hours", type=float, default=0.0,
                       help="skip the maths and set the hours yourself")
    h_add.add_argument("--notes", default="")

    h_start = hack_sub.add_parser("start", help="clock in")
    h_start.add_argument("name")
    h_start.add_argument("--note", default="")

    hack_sub.add_parser("stop", help="clock out")
    hack_sub.add_parser("status", help="hours spent, budget, and time left")

    h_done = hack_sub.add_parser("done", help="mark submitted or finished")
    h_done.add_argument("name")
    h_done.add_argument("--state", default="submitted",
                        choices=["shortlisted", "building", "submitted", "done"])
    p_hack.set_defaults(fn=cmd_hack)

    p_skills = sub.add_parser(
        "skills-scan", help="measure language experience from your actual code"
    )
    p_skills.add_argument("--root", action="append",
                          help="directory to scan (repeatable)")
    p_skills.add_argument("--fast", action="store_true",
                          help="skip framework detection")
    p_skills.set_defaults(fn=cmd_skills_scan)

    p_mark = sub.add_parser(
        "mark-applied", help="record an application for a job already in the queue"
    )
    p_mark.add_argument("job", help="external_id from the queue")
    p_mark.add_argument("--date", default="", help="when you applied, ISO timestamp")
    p_mark.set_defaults(fn=cmd_mark_applied)

    p_log = sub.add_parser(
        "log-application",
        help="register an application you made outside jobbot",
    )
    p_log.add_argument("--company", required=True)
    p_log.add_argument("--title", required=True)
    p_log.add_argument("--url", default="")
    p_log.add_argument("--location", default="")
    p_log.add_argument("--date", default="", help="when you applied, YYYY-MM-DD")
    p_log.set_defaults(fn=cmd_log_application)

    p_apps = sub.add_parser("applications", help="applications sent and their outcomes")
    p_apps.add_argument("--sweep", action="store_true",
                        help="mark long-silent applications as ghosted")
    p_apps.add_argument("--ghost-after", type=int, default=outcomes.GHOST_AFTER_DAYS)
    p_apps.set_defaults(fn=cmd_applications)

    p_learn = sub.add_parser(
        "learn", help="what the rejections say, and the model once it is earned"
    )
    p_learn.add_argument("-v", "--verbose", action="store_true",
                         help="show every per-feature split")
    p_learn.add_argument("--json", action="store_true")
    p_learn.add_argument("--advise", metavar="EXTERNAL_ID",
                         help="score one pending job against what has been learned")
    p_learn.add_argument("--sweep", action="store_true",
                         help="count long silences as negatives before analysing")
    p_learn.add_argument("--ghost-after", type=int, default=outcomes.GHOST_AFTER_DAYS)
    p_learn.add_argument("--no-ghosted", action="store_true",
                         help="analyse only applications that actually replied")
    p_learn.set_defaults(fn=cmd_learn)

    sub.add_parser("stats", help="funnel and response rate").set_defaults(fn=cmd_stats)
    sub.add_parser("status", help="alias for stats").set_defaults(fn=cmd_stats)
    sub.add_parser("review", help="launch the Streamlit review queue").set_defaults(fn=cmd_review)
    p_talismans = sub.add_parser(
        "talismans", help="the consecrated works copied into consecrations/"
    )
    p_talismans.add_argument(
        "--imprint-resume", action="store_true",
        help="stamp each consecration's path onto resume.pdf as invisible text "
             "(PDF render mode 3 — extractable, never painted)",
    )
    p_talismans.set_defaults(fn=cmd_talismans)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

"""Review queue.

One job at a time: what it is, why it scored, whether the posting is actually
reachable, and the materials to send. You read it, then either open the
application page or skip. The submit button is on the company's site, not here.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from jobbot import config, profile as profile_mod, store, tailor
from jobbot.gating import Gate

st.set_page_config(
    page_title="jobbot review queue",
    page_icon=":material/work:",
    layout="wide",
)

# ── State ──────────────────────────────────────────────────────────────────────
st.session_state.setdefault("cursor", 0)
st.session_state.setdefault("generated", {})

GATE_STYLE = {
    Gate.EQUIVALENT_OK.value: (":material/check_circle:", "green"),
    Gate.OPEN.value: (":material/check_circle:", "green"),
    Gate.SOFT_DEGREE.value: (":material/info:", "blue"),
    Gate.HARD_DEGREE.value: (":material/warning:", "orange"),
    Gate.ADVANCED.value: (":material/block:", "red"),
    Gate.ENROLLMENT.value: (":material/block:", "red"),
}


@st.cache_data(ttl="60s")
def load_queue(min_score: int, include_gated: bool, limit: int) -> pd.DataFrame:
    with store.session() as conn:
        rows = store.queue(
            conn, limit=limit, min_score=min_score, include_gated=include_gated
        )
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl="30s")
def load_stats() -> dict:
    with store.session() as conn:
        return store.stats(conn)


def advance() -> None:
    st.session_state.cursor += 1
    load_queue.clear()


def mark(external_id: str, status: store.Status, note: str = "") -> None:
    with store.session() as conn:
        store.set_status(conn, external_id, status, note)
    load_stats.clear()
    advance()


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Queue", divider="gray")

    min_score = st.slider("Minimum score", 0, 80, 10)
    limit = st.number_input("Fetch at most", 10, 500, 100, step=10)
    include_gated = st.toggle(
        "Show gated postings",
        value=False,
        help="Postings that require enrollment or a completed degree. Hidden by "
        "default — turn on to sanity-check what the filter is removing.",
    )

    st.divider()

    stats = load_stats()
    st.metric("In database", stats["total"])
    st.metric("Applied", stats["applied"])
    if stats["response_rate"] is not None:
        st.metric("Response rate", f"{stats['response_rate']:.0%}")
    else:
        st.caption("No response rate yet — nothing marked applied.")

    # Every Greenhouse/Lever/Ashby form asks for the same handful of fields.
    # Keeping them one click away beats retyping them 26 times.
    with st.expander("Application fields", icon=":material/badge:"):
        prof = profile_mod.load()
        for label, value in [
            ("Name", prof.full_name),
            ("Email", prof.email),
            ("Phone", prof.phone),
            ("Location", f"{prof.city}, {prof.state}"),
            ("LinkedIn", prof.linkedin_url),
            ("GitHub", prof.github_url),
        ]:
            if value:
                st.caption(label)
                st.code(value, language=None)
        st.caption("Work authorized")
        st.code("Yes" if prof.work_authorized else "No", language=None)
        st.caption("Requires sponsorship")
        st.code("Yes" if prof.requires_sponsorship else "No", language=None)
        if prof.resume_path:
            st.caption("Resume")
            st.code(prof.resume_path, language=None)

    if stats["by_gate"]:
        st.divider()
        st.caption("Eligibility gates")
        gate_df = pd.DataFrame(
            sorted(stats["by_gate"].items(), key=lambda kv: -kv[1]),
            columns=["gate", "jobs"],
        )
        st.dataframe(gate_df, hide_index=True)

# ── Profile check ──────────────────────────────────────────────────────────────
profile = profile_mod.load()
missing = profile.missing_fields()

if missing:
    st.warning(
        "Your profile is incomplete, so tailored materials will be thin or "
        "unavailable. Missing: " + ", ".join(missing),
        icon=":material/person_alert:",
    )
    with st.expander("Fix it"):
        st.markdown(
            f"Edit `{config.PROFILE_PATH}` and fill in the empty fields, then "
            "reload this page."
        )

# ── Queue ──────────────────────────────────────────────────────────────────────
queue_df = load_queue(min_score, include_gated, int(limit))

if queue_df.empty:
    st.title("Nothing in the queue")
    st.markdown(
        "Run a refresh to pull new postings:\n\n"
        "```bash\npython -m jobbot refresh\n```\n\n"
        "If a refresh just ran and this is still empty, every match was either "
        "knocked out on title or gated. Turn on **Show gated postings** in the "
        "sidebar to see what was filtered and why."
    )
    st.stop()

cursor = st.session_state.cursor
if cursor >= len(queue_df):
    st.title("Queue cleared")
    st.balloons()
    st.markdown(f"You reviewed {len(queue_df)} postings.")
    if st.button("Start over", icon=":material/refresh:"):
        st.session_state.cursor = 0
        load_queue.clear()
        st.rerun()
    st.stop()

job = queue_df.iloc[cursor]
job_id = job["external_id"]

# ── Header ─────────────────────────────────────────────────────────────────────
st.progress((cursor + 1) / len(queue_df), text=f"{cursor + 1} of {len(queue_df)}")

st.title(job["title"])
st.subheader(f"{job['company']} · {job['location'] or 'location not listed'}")

with st.container(horizontal=True):
    st.metric("Fit score", int(job["score"]))

    gate = job["gate"] or Gate.OPEN.value
    icon, color = GATE_STYLE.get(gate, (":material/help:", "gray"))
    gate_label = Gate(gate).label if gate in Gate._value2member_map_ else gate
    st.metric("Eligibility", gate_label)

    if job["ats"]:
        st.metric("Source", job["ats"])

# ── Gate evidence ──────────────────────────────────────────────────────────────
evidence = json.loads(job["gate_evidence"] or "[]")
if evidence:
    verdict = Gate(gate) if gate in Gate._value2member_map_ else None
    box = st.error if verdict and not verdict.worth_applying else st.info
    box(
        "**Why this eligibility call:**\n\n"
        + "\n\n".join(f"> {e}" for e in evidence),
        icon=icon,
    )

# ── Actions ────────────────────────────────────────────────────────────────────
with st.container(horizontal=True):
    st.link_button(
        "Open application",
        job["url"],
        icon=":material/open_in_new:",
        type="primary",
        disabled=not job["url"],
    )
    if st.button("Mark applied", icon=":material/check:"):
        mark(job_id, store.Status.APPLIED)
        st.rerun()
    if st.button("Skip", icon=":material/skip_next:"):
        mark(job_id, store.Status.SKIPPED)
        st.rerun()
    if st.button("Next, decide later", icon=":material/arrow_forward:"):
        advance()
        st.rerun()

st.divider()

# ── Detail ─────────────────────────────────────────────────────────────────────
tab_desc, tab_score, tab_materials = st.tabs(
    ["Posting", "Score breakdown", "Materials"]
)

with tab_desc:
    description = job["description"] or ""
    if description:
        st.text(description[:12000])
        if len(description) > 12000:
            st.caption("Truncated. Open the posting for the rest.")
    else:
        st.info(
            "No description was captured for this posting, which means the "
            "eligibility gate could not be checked. Read the posting before "
            "spending time on it.",
            icon=":material/info:",
        )

with tab_score:
    reasons = json.loads(job["score_reasons"] or "[]")
    if reasons:
        for reason in reasons:
            st.markdown(f"- {reason}")
    else:
        st.caption("No score breakdown recorded.")

with tab_materials:
    variants = json.loads(job.get("letter_variants") or "{}")
    cached = st.session_state.generated.get(job_id)
    if cached:
        variants = {**variants, **cached}

    notes = job.get("resume_notes") or ""
    has_key = bool(config.anthropic_key())

    if not has_key and not variants:
        st.caption(
            "No ANTHROPIC_API_KEY set. Export a batch and draft it in "
            "ChatGPT Web (`python -m jobbot export`), or paste a letter below, "
            "or generate a targeting brief."
        )

    drafted = [k for k in variants if k in tailor.FRAMINGS and variants[k]]
    if variants.get("chatgpt"):
        drafted.append("chatgpt")

    with st.container(horizontal=True):
        if st.button(
            "Generate all framings" if has_key else "Generate brief",
            icon=":material/edit_note:",
            help="Existing letters are never replaced.",
        ):
            produced = {}
            with st.spinner("Writing…"):
                if has_key:
                    targets = [f for f in tailor.FRAMINGS if f not in drafted]
                    for name in targets:
                        letter, new_notes = tailor.generate(
                            profile,
                            title=job["title"],
                            company=job["company"],
                            description=job["description"] or "",
                            gate=gate,
                            framing=name,
                        )
                        produced[name] = letter
                        notes = new_notes or notes
                else:
                    # A brief is a checklist, not a letter. It gets its own slot
                    # so it can never take the place of a drafted framing.
                    produced["brief"] = tailor.offline_brief(
                        profile,
                        job["title"],
                        job["company"],
                        job["description"] or "",
                        gate,
                    )
            st.session_state.generated[job_id] = produced
            variants = {**produced, **variants}  # stored letters win
            with store.session() as conn:
                store.save_materials(conn, job_id, resume_notes=notes, variants=variants)
            st.rerun()

        if drafted:
            st.caption(f"{len(drafted)} letter(s) already written — safe to click.")

    with st.expander("Paste a ChatGPT letter"):
        pasted = st.text_area(
            "Cover letter from ChatGPT Web",
            height=220,
            key=f"paste_{job_id}",
            placeholder="Paste the 250–400 word letter here, then save.",
        )
        replace = st.checkbox(
            "Replace an existing ChatGPT letter for this job",
            value=False,
            key=f"paste_overwrite_{job_id}",
            help="Off by default — existing non-empty letters are kept.",
        )
        if st.button("Save pasted letter", key=f"save_paste_{job_id}"):
            text = (pasted or "").strip()
            if not text:
                st.warning("Nothing to save — paste a letter first.")
            else:
                with store.session() as conn:
                    store.save_materials(
                        conn,
                        job_id,
                        resume_notes=notes,
                        variants={"chatgpt": text},
                        overwrite=replace,
                    )
                    store.select_letter(conn, job_id, "chatgpt")
                st.success("Saved as the selected cover letter for this application.")
                st.rerun()

    if variants:
        st.caption(
            "These differ only in how they handle education. Pick one — the "
            "choice is saved per job."
        )
        # Real letters first so the default selection is never a brief.
        ordered = [f for f in tailor.FRAMINGS if variants.get(f)]
        ordered += [k for k in variants if k not in ordered]

        choice = st.segmented_control(
            "Education framing",
            options=ordered,
            default=ordered[0],
            key=f"framing_{job_id}",
        )
        if choice:
            if choice == "brief":
                st.info(
                    "This is a targeting checklist, not a cover letter. It is "
                    "not submitted anywhere.",
                    icon=":material/info:",
                )
            # st.code carries a built-in copy button, which is the one action
            # this tab actually exists for.
            st.code(variants[choice], language=None, wrap_lines=True)

            with st.expander("Edit before sending"):
                st.text_area(
                    "Cover letter",
                    value=variants[choice],
                    height=320,
                    label_visibility="collapsed",
                    key=f"letter_{job_id}_{choice}",
                )
            if st.button(
                "Use this one",
                icon=":material/check:",
                key=f"pick_{job_id}",
                disabled=choice == "brief",
            ):
                with store.session() as conn:
                    store.select_letter(conn, job_id, choice)
                st.success(f"Saved the “{choice}” version for this application.")

    if notes:
        st.markdown("#### Resume emphasis")
        st.markdown(notes)

    if not variants and not notes:
        st.caption("Nothing generated for this posting yet.")

"""Exercise 5 — Streamlit approval UI for the HITL PR review agent.

Run with:
    uv run streamlit run app.py

Goal: wrap the LangGraph built in exercises 1–4 in a web UI that adapts to
the confidence bucket of each PR.

Routing thresholds (common/schemas.py):
    > 72%        auto_approve     UI shows a success card; reviewer does nothing
    58 – 72%     human_approval   UI shows Approve / Reject / Edit buttons
    <  58%       escalate         UI shows a question form for the reviewer
"""

from __future__ import annotations

import asyncio
import uuid

import streamlit as st
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from common.db import db_conn, db_path
from exercises.exercise_4_audit import build_graph


load_dotenv()


# ─── Session state ─────────────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "pr_url" not in st.session_state:
    st.session_state.pr_url = ""
if "interrupt_payload" not in st.session_state:
    st.session_state.interrupt_payload = None
if "final" not in st.session_state:
    st.session_state.final = None


async def recent_sessions():
    async with db_conn() as conn:
        async with conn.execute("""
            SELECT thread_id, pr_url, MAX(timestamp) AS last_event,
                   CASE MAX(CASE WHEN action = 'fetch_pr' THEN 0
                                 WHEN risk_level = 'high' THEN 3
                                 WHEN risk_level = 'med' THEN 2 ELSE 1 END)
                       WHEN 3 THEN 'high' WHEN 2 THEN 'med'
                       WHEN 1 THEN 'low' ELSE 'unknown' END AS worst_risk
            FROM audit_events
            GROUP BY thread_id, pr_url
            ORDER BY last_event DESC
            LIMIT 25
        """) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def load_session(thread_id: str):
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        graph = build_graph(cp)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        if not snapshot.values:
            return None
        payload = None
        for task in snapshot.tasks:
            if task.interrupts:
                payload = task.interrupts[0].value
                break
        return snapshot.values, payload


# ─── Page setup ────────────────────────────────────────────────────────────
st.set_page_config(page_title="HITL PR Review", layout="wide")
st.title("HITL PR Review Agent")

if st.session_state.thread_id is None and st.query_params.get("thread"):
    saved_thread = st.query_params["thread"]
    saved = asyncio.run(load_session(saved_thread))
    if saved:
        values, payload = saved
        st.session_state.thread_id = saved_thread
        st.session_state.pr_url = values["pr_url"]
        st.session_state.interrupt_payload = payload
        st.session_state.final = values if payload is None and values.get("final_action") else None


# ─── Sidebar — recent sessions ─────────────────────────────────────────────
with st.sidebar:
    st.header("Recent sessions")
    for row in asyncio.run(recent_sessions()):
        label = f"{row['pr_url'].rsplit('/', 1)[-1]} · {row['worst_risk']} · {row['last_event'][:16]}"
        if st.button(label, key=f"session_{row['thread_id']}", help=row["pr_url"]):
            saved = asyncio.run(load_session(row["thread_id"]))
            if saved:
                values, payload = saved
                st.session_state.thread_id = row["thread_id"]
                st.session_state.pr_url = row["pr_url"]
                st.session_state.interrupt_payload = payload
                st.session_state.final = values if payload is None and values.get("final_action") else None
                st.query_params["thread"] = row["thread_id"]
                st.rerun()


# ─── Top form — start a new review ─────────────────────────────────────────
with st.form("start"):
    pr_url = st.text_input(
        "PR URL", value=st.session_state.pr_url,
        placeholder="https://github.com/VinUni-AI20k/PR-Demo/pull/1",
    )
    submitted = st.form_submit_button("Run review")


# ─── Renderers per interrupt kind ──────────────────────────────────────────
def render_approval_card(payload: dict) -> dict | None:
    """58–72% bucket: show the LLM review + 3 buttons. Return resume dict or None."""
    conf = payload["confidence"]
    title = "Confirm refined review" if payload.get("after_escalation") else "Approval requested"
    st.subheader(f"{title} — confidence {conf:.0%}")
    st.caption(payload["confidence_reasoning"])
    st.markdown(payload["summary"])

    for c in payload.get("comments", []):
        st.markdown(f"- **[{c['severity']}]** `{c['file']}:{c.get('line') or '?'}` — {c['body']}")

    with st.expander("Diff"):
        st.code(payload.get("diff_preview", ""), language="diff")

    edited_comment = st.text_area(
        "Comment to post (change this before choosing Edit)",
        value=payload.get("suggested_comment", ""),
        height=220, key=f"edited_comment_{st.session_state.thread_id}_{payload.get('after_escalation', False)}",
    )
    col1, col2, col3 = st.columns(3)
    if col1.button("Approve", type="primary"):
        return {"choice": "approve", "feedback": ""}
    if col2.button("Reject"):
        return {"choice": "reject", "feedback": ""}
    if col3.button("Edit"):
        if not edited_comment.strip():
            st.error("Enter a replacement comment before choosing Edit.")
        else:
            return {"choice": "edit", "feedback": edited_comment}
    return None


def render_escalation_card(payload: dict) -> dict | None:
    """< 58% bucket: show risk factors + question form. Return {question: answer} or None."""
    conf = payload["confidence"]
    st.subheader(f"Strong escalation — confidence {conf:.0%}")
    st.caption(payload["confidence_reasoning"])
    if payload.get("risk_factors"):
        st.error("Risks: " + ", ".join(payload["risk_factors"]))
    st.markdown(payload["summary"])

    with st.form("escalation"):
        answers = {
            question: st.text_area(question, key=f"answer_{st.session_state.thread_id}_{index}")
            for index, question in enumerate(payload["questions"])
        }
        submitted_answers = st.form_submit_button("Submit answers")
    if submitted_answers:
        if all(answer.strip() for answer in answers.values()):
            return answers
        st.error("Answer every question before continuing.")
    return None


# ─── Drive the graph ───────────────────────────────────────────────────────
async def run_graph(pr_url: str, thread_id: str, resume_value=None):
    """Invoke the graph once. Returns the final result or {'__interrupt__': ...}."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        graph = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}
        if resume_value is None:
            return await graph.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)
        return await graph.ainvoke(Command(resume=resume_value), cfg)


# ─── Main flow ─────────────────────────────────────────────────────────────
if submitted and pr_url:
    st.session_state.pr_url = pr_url
    st.session_state.thread_id = str(uuid.uuid4())
    st.query_params["thread"] = st.session_state.thread_id
    st.session_state.interrupt_payload = None
    st.session_state.final = None

    try:
        with st.spinner("Fetching PR + asking the LLM..."):
            result = asyncio.run(run_graph(pr_url, st.session_state.thread_id))
    except Exception as exc:
        st.error(f"Review failed: {exc}")
        st.stop()

    if "__interrupt__" in result:
        st.session_state.interrupt_payload = result["__interrupt__"][0].value
    else:
        st.session_state.final = result
    st.rerun()

# Render the current interrupt card, if any
payload = st.session_state.interrupt_payload
if payload is not None:
    kind = payload["kind"]
    answer = render_approval_card(payload) if kind == "approval_request" else render_escalation_card(payload)
    if answer is not None:
        try:
            with st.spinner("Resuming..."):
                result = asyncio.run(run_graph(
                    st.session_state.pr_url, st.session_state.thread_id, resume_value=answer,
                ))
        except Exception as exc:
            st.error(f"Resume failed: {exc}")
            st.stop()
        if "__interrupt__" in result:
            st.session_state.interrupt_payload = result["__interrupt__"][0].value
        else:
            st.session_state.interrupt_payload = None
            st.session_state.final = result
        st.rerun()

# Render final state, if reached
if st.session_state.final is not None:
    final = st.session_state.final
    action = final.get("final_action", "?")
    if action in {"auto_committed", "committed"}:
        st.success(f"✓ {action} — comment posted to {st.session_state.pr_url}")
        st.link_button("View PR on GitHub", st.session_state.pr_url)
    elif action == "rejected":
        st.warning("Rejected — no comment posted")
    elif action in {"auto_commit_failed", "commit_failed"}:
        st.error("Could not post the review comment to GitHub")
    else:
        st.info(f"final_action = {action}")
    st.caption(f"thread_id = {st.session_state.thread_id}  ·  replay: "
               f"`uv run python -m audit.replay --thread {st.session_state.thread_id}`")

"""Exercise 4 — Structured SQLite audit trail + durable checkpointer.

Zero setup — SQLite stores everything in a single file (`./hitl_audit.db`).
The audit_events schema is created automatically on first connection.

Goals:

1. Use AsyncSqliteSaver so the graph can resume after a crash.
2. Define and emit an `AuditEntry` (common/schemas.py) for every meaningful step,
   so the full session can be replayed.
3. Verify with `uv run python -m audit.replay --thread <id>`.

Approach:
    - Read `node_fetch_pr` below — it is the one fully-worked example. Pay attention
      to *why* each AuditEntry field has the value it does at that step.
    - For every other node, you decide what to log. Field reference:
      `common/schemas.py:AuditEntry`. Helper: `risk_level_for(confidence)`.
    - Implement the one-line body of `audit()`. Everything else (graph wiring,
      checkpointer setup, interrupt/resume loop) is already done for you.

After escalation, the refined review returns to human_approval for final
confirmation before any comment is posted.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from rich.console import Console
from rich.panel import Panel

from common.db import db_conn, db_path, write_audit_event
from common.github import fetch_pr, post_review_comment
from common.llm import REVIEW_POLICY, get_llm
from common.schemas import (
    AUTO_APPROVE_THRESHOLD,
    ESCALATE_THRESHOLD,
    AuditEntry,
    PRAnalysis,
    ReviewState,
    risk_level_for,
)


console = Console()
AGENT_ID = "pr-review-agent@v0.1"


async def audit(state, entry: AuditEntry) -> None:
    """Write one structured AuditEntry row to the `audit_events` table.

    `thread_id` and `pr_url` are taken from `state` so callers only build
    the entry itself.
    """
    if entry.action in {"human_approval", "escalate"} and entry.decision == "pending":
        # The interrupted node starts again on resume; record its request once.
        async with db_conn() as conn:
            async with conn.execute(
                "SELECT 1 FROM audit_events WHERE thread_id = ? AND action = ? AND decision = 'pending' LIMIT 1",
                (state["thread_id"], entry.action),
            ) as cur:
                if await cur.fetchone() is not None:
                    return
    await write_audit_event(thread_id=state["thread_id"], pr_url=state["pr_url"], entry=entry)


# ─── Reference example — read this carefully ───────────────────────────────
async def node_fetch_pr(state):
    console.print("[cyan]→ fetch_pr[/cyan]")
    t0 = time.monotonic()
    with console.status("[dim]Fetching PR from GitHub...[/dim]"):
        pr = fetch_pr(state["pr_url"])
    console.print(f"  [green]✓[/green] {len(pr.files_changed)} files, head {pr.head_sha[:7]}")
    # We've only fetched the diff, not analyzed it. So:
    #   - confidence is unknown → 0.0
    #   - risk_level can't be derived from confidence yet → "med" as neutral default
    #   - decision is "pending" — nothing has been decided
    #   - reviewer_id is None — no human is involved at this stage
    #   - reason is a short human-readable summary of what happened
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="fetch_pr",
        confidence=0.0,
        risk_level="med",
        decision="pending",
        reason=f"Fetched {len(pr.files_changed)} files, head={pr.head_sha[:7]}",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {
        "pr_title": pr.title,
        "pr_diff": pr.diff,
        "pr_files": pr.files_changed,
        "pr_head_sha": pr.head_sha,
    }
# ───────────────────────────────────────────────────────────────────────────


async def node_analyze(state):
    console.print("[cyan]→ analyze[/cyan]")
    t0 = time.monotonic()
    llm = get_llm().with_structured_output(PRAnalysis)
    with console.status("[dim]LLM reviewing the diff...[/dim]"):
        a: PRAnalysis = await llm.ainvoke([
            {"role": "system", "content": REVIEW_POLICY},
            {"role": "user", "content": f"Title: {state['pr_title']}\nDiff:\n{state['pr_diff']}"},
        ])
    console.print(f"  [green]✓[/green] confidence={a.confidence:.0%}, {len(a.comments)} comment(s)")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="analyze", confidence=a.confidence,
        risk_level=risk_level_for(a.confidence), decision="pending",
        reason=a.confidence_reasoning,
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"analysis": a}


async def node_route(state):
    console.print("[cyan]→ route[/cyan]")
    t0 = time.monotonic()
    c = state["analysis"].confidence
    if c >= AUTO_APPROVE_THRESHOLD:
        decision = "auto_approve"
    elif c < ESCALATE_THRESHOLD:
        decision = "escalate"
    else:
        decision = "human_approval"
    console.print(f"  [green]✓[/green] decision=[bold]{decision}[/bold] (confidence={c:.0%})")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="route", confidence=c,
        risk_level=risk_level_for(c), decision=decision,
        reason=f"Confidence {c:.2f} routed to {decision}",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"decision": decision}


async def node_human_approval(state):
    t0 = time.monotonic()
    a = state["analysis"]

    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="human_approval", confidence=a.confidence,
        risk_level=risk_level_for(a.confidence), decision="pending",
        reason="Waiting for reviewer confirmation",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))

    resp = interrupt({
        "kind": "approval_request",
        "pr_url": state["pr_url"],
        "confidence": a.confidence,
        "confidence_reasoning": a.confidence_reasoning,
        "summary": a.summary,
        "comments": [c.model_dump() for c in a.comments],
        "diff_preview": state["pr_diff"][:2000],
        "suggested_comment": _render_comment_body(state),
        "after_escalation": bool(state.get("escalation_answers")),
    })

    choice = resp.get("choice")
    if choice not in {"approve", "reject", "edit"}:
        raise ValueError(f"Invalid reviewer choice: {choice}")
    if choice == "edit" and not (resp.get("feedback") or "").strip():
        raise ValueError("Edit requires a replacement comment")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="human_approval", confidence=a.confidence,
        risk_level=risk_level_for(a.confidence), reviewer_id=os.environ.get("GITHUB_USER"),
        decision=choice, reason=resp.get("feedback") or "Reviewer confirmed the review",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"human_choice": resp.get("choice"), "human_feedback": resp.get("feedback")}


def _render_comment_body(state) -> str:
    if state.get("human_choice") == "edit":
        return state.get("human_feedback") or ""
    a = state["analysis"]
    lines = [f"### Automated review (confidence {a.confidence:.0%})", "", a.summary, ""]
    for c in a.comments:
        lines.append(f"- **[{c.severity}]** `{c.file}:{c.line or '?'}` — {c.body}")
    if state.get("human_feedback"):
        lines.append(f"\n_Reviewer note: {state['human_feedback']}_")
    if state.get("escalation_answers"):
        lines.append("\n_Reviewer answered escalation questions:_")
        for q, ans in state["escalation_answers"].items():
            lines.append(f"> **{q}** {ans}")
    return "\n".join(lines)


def _post(state) -> str:
    try:
        post_review_comment(state["pr_url"], _render_comment_body(state))
        console.print(f"  [green]✓[/green] posted comment to {state['pr_url']}")
        return "committed"
    except Exception as e:
        console.print(f"  [red]✗[/red] post failed: {e}")
        return "commit_failed"


async def node_commit(state):
    console.print("[cyan]→ commit[/cyan]")
    t0 = time.monotonic()
    choice = state.get("human_choice")
    if choice == "approve" or (choice == "edit" and (state.get("human_feedback") or "").strip()):
        action = _post(state)
    else:
        console.print(f"  [yellow]·[/yellow] skipping comment (choice={state.get('human_choice')})")
        action = "rejected"
    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="commit", confidence=state["analysis"].confidence,
        risk_level=risk_level_for(state["analysis"].confidence),
        reviewer_id=os.environ.get("GITHUB_USER"), decision=choice or "reject",
        reason=action, execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"final_action": action}


async def node_auto_approve(state):
    console.print("[cyan]→ auto_approve[/cyan]  [dim]high confidence — posting directly[/dim]")
    t0 = time.monotonic()
    a = state["analysis"]
    action = _post(state)
    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="auto_approve", confidence=a.confidence,
        risk_level=risk_level_for(a.confidence), decision="auto", reason=action,
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"final_action": f"auto_{action}"}


async def node_escalate(state):
    t0 = time.monotonic()
    a = state["analysis"]
    questions = a.escalation_questions or ["What is the intent of this PR?"]

    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="escalate", confidence=a.confidence,
        risk_level=risk_level_for(a.confidence), decision="pending",
        reason="Waiting for answers: " + "; ".join(questions),
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))

    answers = interrupt({
        "kind": "escalation",
        "pr_url": state["pr_url"],
        "confidence": a.confidence,
        "confidence_reasoning": a.confidence_reasoning,
        "summary": a.summary,
        "risk_factors": a.risk_factors,
        "questions": questions,
    })

    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="escalate", confidence=a.confidence,
        risk_level=risk_level_for(a.confidence), reviewer_id=os.environ.get("GITHUB_USER"),
        decision="escalate", reason="; ".join(f"{q}: {answer}" for q, answer in answers.items()),
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"escalation_answers": answers}


async def node_synthesize(state):
    console.print("[cyan]→ synthesize[/cyan]")
    t0 = time.monotonic()
    qa = "\n".join(f"Q: {q}\nA: {a}" for q, a in (state.get("escalation_answers") or {}).items())
    llm = get_llm().with_structured_output(PRAnalysis)
    with console.status("[dim]LLM refining review with reviewer answers...[/dim]"):
        refined: PRAnalysis = await llm.ainvoke([
            {"role": "system", "content": "Refine the PR review using the human answers. Keep unresolved risks and report confidence honestly."},
            {"role": "user", "content": (
                f"Diff:\n{state['pr_diff']}\n\n"
                f"Initial review:\n{state['analysis'].model_dump_json()}\n\nQ&A:\n{qa}"
            )},
        ])
    console.print(f"  [green]✓[/green] refined confidence={refined.confidence:.0%}")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action="synthesize", confidence=refined.confidence,
        risk_level=risk_level_for(refined.confidence), decision="pending",
        reason=refined.confidence_reasoning,
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"analysis": refined}


def build_graph(checkpointer):
    g = StateGraph(ReviewState)
    for name, fn in [
        ("fetch_pr", node_fetch_pr), ("analyze", node_analyze), ("route", node_route),
        ("auto_approve", node_auto_approve), ("human_approval", node_human_approval),
        ("commit", node_commit), ("escalate", node_escalate), ("synthesize", node_synthesize),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "fetch_pr")
    g.add_edge("fetch_pr", "analyze")
    g.add_edge("analyze", "route")
    g.add_conditional_edges(
        "route", lambda s: s["decision"],
        {"auto_approve": "auto_approve", "human_approval": "human_approval", "escalate": "escalate"},
    )
    g.add_edge("auto_approve", END)
    g.add_edge("human_approval", "commit")
    g.add_edge("commit", END)
    g.add_edge("escalate", "synthesize")
    g.add_edge("synthesize", "human_approval")
    return g.compile(checkpointer=checkpointer)


def handle_interrupt(payload):
    kind = payload["kind"]
    if kind == "approval_request":
        console.print(Panel.fit(
            payload["summary"],
            title=f"Confirm refined review, conf={payload['confidence']:.0%}" if payload.get("after_escalation") else f"conf={payload['confidence']:.0%}",
            border_style="green",
        ))
        console.print(payload["suggested_comment"])
        choice = ""
        while choice not in {"approve", "reject", "edit"}:
            choice = console.input("approve/reject/edit? ").strip().lower()
        if choice == "edit":
            feedback = ""
            while not feedback:
                feedback = console.input("Replacement comment: ").strip()
        else:
            feedback = console.input("Feedback: ").strip() if choice == "reject" else ""
        return {"choice": choice, "feedback": feedback}
    return {q: console.input(f"Q: {q}\nA: ").strip() for q in payload["questions"]}


async def run(pr_url: str, thread_id: str | None):
    thread_id = thread_id or str(uuid.uuid4())
    console.rule("[bold]Exercise 4 — SQLite audit trail[/bold]")
    console.print(f"[dim]PR: {pr_url}[/dim]")
    console.print(f"[dim]thread_id = {thread_id}[/dim]\n")

    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}

        snapshot = await app.aget_state(cfg) if thread_id else None
        if snapshot and snapshot.values:
            pending = [item for task in snapshot.tasks for item in task.interrupts]
            if pending:
                result = await app.ainvoke(Command(resume=handle_interrupt(pending[0].value)), cfg)
            elif snapshot.values.get("final_action"):
                result = snapshot.values
            else:
                result = await app.ainvoke(None, cfg)
        else:
            result = await app.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)
        while "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            result = await app.ainvoke(Command(resume=handle_interrupt(payload)), cfg)

        console.rule("Final")
        console.print(f"final_action = {result.get('final_action')}")
        console.print(f"\n[dim]Replay:[/dim] uv run python -m audit.replay --thread {thread_id}")


def main():
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--pr", required=True)
    p.add_argument("--thread", help="Resume an existing thread")
    args = p.parse_args()
    asyncio.run(run(args.pr, args.thread))


if __name__ == "__main__":
    main()

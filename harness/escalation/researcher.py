"""Turning escalated tickets into ready-to-decide questions, and applying the answers."""

from __future__ import annotations

import json
from typing import Any

from harness.board import Board, Ticket, TicketStatus
from harness.context.builder import ticket_card
from harness.escalation.decisions import DecisionRequest, Option, TicketAction
from harness.tools.base import tail

S = TicketStatus

RESEARCHER_SYSTEM = """\
You are the RESEARCHER. A ticket in an automated, ticket-based development pipeline was
escalated to the product owner. Investigate why (read the files and the evidence), then
prepare ONE decision for the owner so they can decide in a minute without researching:
a clear question, the context they need, 2-4 realistic options with concrete pros and cons,
and your recommendation with its reason.

Every option must say what the harness does if chosen (`action`):
- retry: an implementer starts over on the ticket, with the owner's guidance
- send_to_fixer: a fixer continues from the current code, with the owner's guidance
- cancel: drop the ticket
- manual: the owner will handle it outside the pipeline
Put the concrete guidance an agent would follow into the option's description.
"""


def escalation_context(board: Board, ticket: Ticket) -> str:
    """The evidence a researcher (or the owner) needs about an escalated ticket."""
    events = board.events_for(ticket.id)
    escalated = next((e for e in reversed(events) if e.type == "ticket.escalated"), None)
    parts = [ticket_card(ticket)]
    if escalated is not None:
        parts.append(f"Escalated because: {escalated.payload.get('reason', '?')}")
    gates = next((e for e in reversed(events) if e.type == "gates.failed"), None)
    if gates is not None:
        failures = [r for r in gates.payload.get("results", []) if not r.get("ok")]
        parts.append(
            "Last failing gates:\n"
            + tail("\n".join(f"{r['name']}:\n{r.get('detail', '')}" for r in failures), 4000)
        )
    review = next(
        (
            e
            for e in reversed(events)
            if e.type == "ticket.changes_requested" and "source" in e.payload
        ),
        None,
    )
    if review is not None:
        if review.payload.get("findings"):
            parts.append(
                "Last review findings:\n" + json.dumps(review.payload["findings"], indent=2)
            )
        elif review.payload.get("detail"):
            parts.append("Last failure:\n" + tail(str(review.payload["detail"]), 4000))
    runs = [e.payload for e in events if e.type == "agent.finished"]
    if runs:
        parts.append(
            "Agent runs:\n"
            + "\n".join(
                f"- {r.get('agent')}: {r.get('outcome')} {r.get('reason', '')}".rstrip()
                for r in runs
            )
        )
    parts.append(f"Review rounds so far: {ticket.review_rounds}")
    return "\n\n".join(parts)


def fallback_request(ticket: Ticket, reason: str) -> DecisionRequest:
    """Used when the researcher can't run: a generic but still decidable request."""
    return DecisionRequest(
        question=f"{ticket.id} ({ticket.title}) is stuck. How should we proceed?",
        context=f"Escalation reason: {reason}",
        options=[
            Option(
                name="Retry with guidance",
                action="retry",
                description="A fresh implementer starts over; add guidance in your note.",
                pros=["clean start", "your note steers the agent"],
                cons=["discards the current approach"],
            ),
            Option(
                name="Send to a fixer",
                action="send_to_fixer",
                description="A fixer continues from the current code with your note.",
                pros=["keeps the work done so far"],
                cons=["may repeat the same mistake"],
            ),
            Option(
                name="Handle it manually",
                action="manual",
                pros=["full control"],
                cons=["your time"],
            ),
            Option(
                name="Cancel the ticket",
                action="cancel",
                pros=["unblocks the board"],
                cons=["the functionality is not built"],
            ),
        ],
        recommendation="Retry with guidance",
        confidence="low",
        reason="Without an analysis, a guided retry is the cheapest next step.",
    )


def chosen_action(decision: dict[str, Any]) -> tuple[TicketAction, str]:
    """The action and guidance for an answered ticket decision.

    A free-text answer (no option chosen) is treated as a retry guided by that text.
    """
    request = DecisionRequest.model_validate(decision["request"])
    answer = decision["answer"] or {}
    choice, note = answer.get("choice"), str(answer.get("note") or "")
    option = next((o for o in request.options if o.name == choice), None)
    if option is None:
        return "retry", note
    guidance = "\n".join(x for x in (option.description, note) if x)
    return option.action or "manual", guidance


def apply_ticket_decision(board: Board, decision_id: str) -> str | None:
    """Move an escalated ticket according to the owner's answer. Returns the action taken."""
    decision = board.get_decision(decision_id)
    ticket_id = decision["ticket_id"]
    if ticket_id is None or decision["answer"] is None:
        return None
    if board.get(ticket_id).status is not S.ESCALATED:
        return None
    action, guidance = chosen_action(decision)
    if action == "retry":
        board.resolve_escalation(ticket_id, S.READY, decision=decision_id, guidance=guidance)
    elif action == "send_to_fixer":
        board.resolve_escalation(
            ticket_id, S.CHANGES_REQUESTED, decision=decision_id, source="board", detail=guidance
        )
    elif action == "cancel":
        board.resolve_escalation(ticket_id, S.CANCELLED, decision=decision_id)
    else:
        board.log_event(ticket_id, "escalation.manual", decision=decision_id, note=guidance)
    return action

"""Decision requests to the Board (the human), and phase approvals.

Every question that reaches the human must come ready to decide: at least two
alternatives, each with pros and cons, plus a recommendation. The schema enforces
it, so an agent cannot escalate a bare question.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from harness.board import Board
from harness.tools.base import Tool

TicketAction = Literal["retry", "send_to_fixer", "cancel", "manual"]


class Option(BaseModel):
    name: str = Field(description="Short name of this alternative")
    description: str = ""
    pros: list[str] = Field(min_length=1)
    cons: list[str] = Field(min_length=1)
    action: TicketAction | None = Field(
        default=None,
        description="For escalated tickets, what the harness does if this option is chosen: "
        "retry (implement again with the Board's guidance), send_to_fixer (a fixer works on "
        "it with the guidance), cancel, or manual (the owner handles it)",
    )


class DecisionRequest(BaseModel):
    question: str
    context: str = Field(default="", description="What the human needs to know to decide")
    options: list[Option] = Field(min_length=2)
    recommendation: str = Field(description="The name of the option you recommend")
    confidence: Literal["low", "medium", "high"] = "medium"
    reason: str = Field(default="", description="Why you recommend it")

    @model_validator(mode="after")
    def _recommendation_is_an_option(self) -> DecisionRequest:
        names = [o.name for o in self.options]
        if len(set(names)) != len(names):
            raise ValueError("option names must be unique")
        if self.recommendation not in names:
            raise ValueError(f"recommendation must be one of the option names: {names}")
        return self


class Decision(BaseModel):
    choice: str | None = Field(default=None, description="Chosen option name, if any")
    note: str = Field(default="", description="Free text: a custom answer or a reason")

    def describe(self) -> str:
        parts = []
        if self.choice:
            parts.append(f"The Board chose: {self.choice}.")
        if self.note:
            parts.append(f"Board note: {self.note}")
        return " ".join(parts) or "The Board gave no answer."


class Approval(BaseModel):
    approved: bool
    feedback: str = ""


class Human(Protocol):
    """How the harness reaches the human (terminal, dashboard, or both via the board)."""

    async def decide(self, decision_id: str, request: DecisionRequest) -> Decision: ...

    async def approve(self, approval_id: str, gate: str, summary: str) -> Approval: ...


async def ask_board(
    board: Board,
    human: Human,
    request: DecisionRequest,
    *,
    phase: str | None = None,
    ticket_id: str | None = None,
) -> Decision:
    """Record the request, get the human's decision, and record the answer."""
    decision_id = board.create_decision(request.model_dump(), phase=phase, ticket_id=ticket_id)
    decision = await human.decide(decision_id, request)
    if board.get_decision(decision_id)["answer"] is None:  # answered elsewhere otherwise
        board.answer_decision(decision_id, decision.model_dump())
    return decision


async def request_approval(board: Board, human: Human, gate: str, summary: str) -> Approval:
    approval_id = board.create_approval(gate, summary)
    approval = await human.approve(approval_id, gate, summary)
    if board.get_approval(approval_id)["approved"] is None:
        board.resolve_approval(approval_id, approval.approved, approval.feedback)
    return approval


class BoardHuman:
    """Waits for answers written to the board by any channel: the web dashboard,
    `harness answer` / `harness approve`, or another process."""

    def __init__(
        self,
        board: Board,
        *,
        poll_s: float = 1.0,
        announce: Callable[[str], None] | None = print,
        where: str = "the dashboard (`harness dashboard`)",
    ) -> None:
        self.board = board
        self.poll_s = poll_s
        self.announce = announce or (lambda _: None)
        self.where = where

    async def decide(self, decision_id: str, request: DecisionRequest) -> Decision:
        self.announce(
            f"Decision {decision_id} needs you: {request.question}\n"
            f"  Answer on {self.where} or: harness answer {decision_id} <option number> [note]"
        )
        while True:
            answer = self.board.get_decision(decision_id)["answer"]
            if answer is not None:
                return Decision.model_validate(answer)
            await asyncio.sleep(self.poll_s)

    async def approve(self, approval_id: str, gate: str, summary: str) -> Approval:
        self.announce(
            f"Approval {approval_id} ({gate}) needs you.\n"
            f"  Review on {self.where} or: harness approve {approval_id} [--reject 'feedback']"
        )
        while True:
            row = self.board.get_approval(approval_id)
            if row["approved"] is not None:
                return Approval(approved=row["approved"], feedback=row["feedback"])
            await asyncio.sleep(self.poll_s)


def ask_user_tool(board: Board, human: Human, *, phase: str) -> Tool:
    async def ask_user(request: DecisionRequest) -> str:
        decision = await ask_board(board, human, request, phase=phase)
        return decision.describe()

    return Tool(
        "ask_user",
        "Ask the human product owner to decide something you must not guess (product "
        "behavior, scope, priorities, technology trade-offs). Give at least two real "
        "alternatives, each with pros and cons, and your recommendation, so they can decide "
        "without doing their own research. Batch related questions; don't ask about things "
        "you can reasonably decide yourself.",
        DecisionRequest,
        ask_user,
    )

"""Decision requests to the Board (the human), and phase approvals.

Every question that reaches the human must come ready to decide: at least two
alternatives, each with pros and cons, plus a recommendation. The schema enforces
it, so an agent cannot escalate a bare question.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from harness.board import Board
from harness.tools.base import Tool


class Option(BaseModel):
    name: str = Field(description="Short name of this alternative")
    description: str = ""
    pros: list[str] = Field(min_length=1)
    cons: list[str] = Field(min_length=1)


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
    """How the harness reaches the human. CLI now; the web dashboard later."""

    async def decide(self, decision_id: str, request: DecisionRequest) -> Decision: ...

    async def approve(self, gate: str, summary: str) -> Approval: ...


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
    board.answer_decision(decision_id, decision.model_dump())
    return decision


async def request_approval(board: Board, human: Human, gate: str, summary: str) -> Approval:
    approval = await human.approve(gate, summary)
    board.log_event(
        None,
        "phase.approved" if approval.approved else "phase.rejected",
        gate=gate,
        feedback=approval.feedback,
    )
    return approval


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

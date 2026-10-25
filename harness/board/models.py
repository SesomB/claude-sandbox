"""Ticket schema and lifecycle state machine."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TicketStatus(StrEnum):
    DRAFT = "draft"  # written, not yet queued
    WAITING = "waiting"  # queued, dependencies not done yet
    READY = "ready"  # claimable by an Implementer
    IMPLEMENTING = "implementing"
    AWAITING_REVIEW = "awaiting_review"  # claimable by a Reviewer
    REVIEWING = "reviewing"
    CHANGES_REQUESTED = "changes_requested"  # claimable by a Fixer
    FIXING = "fixing"
    APPROVED = "approved"  # claimable by the merge queue
    MERGING = "merging"
    DONE = "done"
    ESCALATED = "escalated"  # waiting on a Board decision
    CANCELLED = "cancelled"


S = TicketStatus

# Claiming a ticket in the key status moves it to the value status, held by one agent.
CLAIM_TRANSITIONS: dict[TicketStatus, TicketStatus] = {
    S.READY: S.IMPLEMENTING,
    S.AWAITING_REVIEW: S.REVIEWING,
    S.CHANGES_REQUESTED: S.FIXING,
    S.APPROVED: S.MERGING,
}

# When a lease expires, a held ticket goes back to where it was claimed from.
LEASE_EXPIRY_TRANSITIONS: dict[TicketStatus, TicketStatus] = {
    v: k for k, v in CLAIM_TRANSITIONS.items()
}

# What the agent holding a ticket may move it to when it finishes.
COMPLETE_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    S.IMPLEMENTING: frozenset({S.AWAITING_REVIEW}),
    S.FIXING: frozenset({S.AWAITING_REVIEW}),
    S.REVIEWING: frozenset({S.APPROVED, S.CHANGES_REQUESTED}),
    S.MERGING: frozenset({S.DONE, S.CHANGES_REQUESTED}),
}

# Where the Board may send an escalated ticket.
ESCALATION_RESOLUTIONS: frozenset[TicketStatus] = frozenset(
    {S.READY, S.AWAITING_REVIEW, S.CHANGES_REQUESTED, S.CANCELLED}
)

HELD_STATUSES = frozenset(LEASE_EXPIRY_TRANSITIONS)
TERMINAL_STATUSES = frozenset({S.DONE, S.CANCELLED})


def normalize_repo_path(path: str) -> str:
    """Canonical repo-relative POSIX path; rejects absolute paths and ``..``."""
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"not a repo-relative path: {path!r}")
    return p.as_posix()


class TicketSpec(BaseModel):
    """The authored part of a ticket (what a Decomposer writes)."""

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    title: str
    spec_refs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    contracts: list[str] = Field(default_factory=list)
    owned_files: list[str] = Field(default_factory=list)
    readable_files: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    tests_required: list[str] = Field(default_factory=list)
    size: Literal["S", "M", "L"] = "S"
    priority: int = 0

    @field_validator("owned_files", "readable_files", "tests_required")
    @classmethod
    def _normalize_paths(cls, paths: list[str]) -> list[str]:
        return sorted({normalize_repo_path(p) for p in paths})


class Ticket(TicketSpec):
    # Runtime state, owned by the Board; ignored when a ticket is added.
    status: TicketStatus = S.DRAFT
    agent: str | None = None
    lease_until: float | None = None
    review_rounds: int = 0

    @classmethod
    def from_spec(cls, spec: TicketSpec) -> Ticket:
        return cls.model_validate(spec.model_dump())

    def spec(self) -> dict[str, object]:
        """The authored part of the ticket, without runtime state."""
        return self.model_dump(exclude={"status", "agent", "lease_until", "review_rounds"})

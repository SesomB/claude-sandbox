"""The ticket linter: mechanical checks a plan must pass before it reaches the board."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from harness.board import Ticket, TicketSpec
from harness.context.builder import (
    ARCHITECTURE_PATH,
    ContextBudgetExceeded,
    extract_sections,
    implementer_pack,
)
from harness.phases.spec_lint import defined_ids
from harness.stacks import StackProfile

PROTECTED_PREFIXES = (".harness/", ".git/")
PROTECTED_FILES = ("docs/spec.md", "docs/architecture.md", "docs/conventions.md")


@dataclass(frozen=True)
class LintIssue:
    ticket_id: str | None
    message: str

    def __str__(self) -> str:
        return f"{self.ticket_id}: {self.message}" if self.ticket_id else self.message


def _ancestors(tid: str, by_id: dict[str, TicketSpec], existing: dict[str, Ticket]) -> set[str]:
    seen: set[str] = set()
    stack = [tid]
    while stack:
        cur = stack.pop()
        deps = (
            by_id[cur].depends_on
            if cur in by_id
            else (existing[cur].depends_on if cur in existing else [])
        )
        for d in deps:
            if d not in seen:
                seen.add(d)
                stack.append(d)
    return seen


def lint_plan(
    tickets: Sequence[TicketSpec],
    *,
    root: Path,
    spec: str,
    stacks: list[StackProfile],
    budget: int,
    existing: Sequence[Ticket] = (),
) -> list[LintIssue]:
    issues: list[LintIssue] = []

    def bad(tid: str | None, msg: str) -> None:
        issues.append(LintIssue(tid, msg))

    spec_ids = defined_ids(spec)
    architecture = (
        (root / ARCHITECTURE_PATH).read_text() if (root / ARCHITECTURE_PATH).is_file() else ""
    )
    existing_by_id = {t.id: t for t in existing}
    open_owned = {
        p: t.id
        for t in existing
        if t.status.value not in ("done", "cancelled")
        for p in t.owned_files
    }
    by_id: dict[str, TicketSpec] = {}
    for t in tickets:
        if t.id in by_id or t.id in existing_by_id:
            bad(t.id, "duplicate ticket id")
        by_id[t.id] = t
    owners: dict[str, str] = dict(open_owned)

    def is_shared(p: str) -> bool:
        return any(s.is_shared_file(p) for s in stacks)

    def is_test(p: str) -> bool:
        return any(s.is_test_file(p) for s in stacks)

    def is_source(p: str) -> bool:
        return any(s.is_source_file(p) for s in stacks)

    for t in tickets:
        if t.size != "S":
            bad(t.id, f"size {t.size}: split it into S tickets")
        if not t.acceptance:
            bad(t.id, "no acceptance criteria")
        if not t.spec_refs:
            bad(t.id, "no spec_refs")
        for ref in t.spec_refs:
            if ref not in spec_ids:
                bad(t.id, f"spec ref {ref} is not defined in the spec")
        if not t.owned_files:
            bad(t.id, "owns no files")
        for p in t.owned_files:
            if p.startswith(PROTECTED_PREFIXES) or p in PROTECTED_FILES:
                bad(t.id, f"{p} is a harness/phase document and cannot be owned by a ticket")
            if is_shared(p):
                bad(t.id, f"{p} is a shared file; tickets change it via edit_shared_file")
            if p in owners and owners[p] != t.id:
                bad(t.id, f"{p} is also owned by {owners[p]}")
            owners.setdefault(p, t.id)
        for p in t.tests_required:
            if p not in t.owned_files:
                bad(t.id, f"required test {p} must also be in owned_files")
            if stacks and not is_test(p):
                bad(t.id, f"required test {p} doesn't match any stack's test file patterns")
        if (
            stacks
            and any(is_source(p) and not is_test(p) for p in t.owned_files)
            and not (t.tests_required)
        ):
            bad(t.id, "owns source files but requires no tests")
        for d in t.depends_on:
            if d not in by_id and d not in existing_by_id:
                bad(t.id, f"depends on unknown ticket {d}")
        for c in t.contracts:
            if not extract_sections(architecture, [c]):
                bad(t.id, f"contract {c} is not described in {ARCHITECTURE_PATH}")

    # Cycles (reported once per ticket on a cycle).
    for t in tickets:
        if t.id in _ancestors(t.id, by_id, existing_by_id):
            bad(t.id, "is part of a dependency cycle")

    # Readable files must exist now, or be created by a ticket this one depends on.
    for t in tickets:
        if any(i.message.endswith("dependency cycle") and i.ticket_id == t.id for i in issues):
            continue
        ancestors = _ancestors(t.id, by_id, existing_by_id)
        for p in t.readable_files:
            if (root / p).exists():
                continue
            owner = owners.get(p)
            if owner is None:
                bad(t.id, f"readable file {p} doesn't exist and no ticket creates it")
            elif owner not in ancestors:
                bad(t.id, f"reads {p}, created by {owner}: add {owner} to depends_on")

    # Every acceptance criterion in the spec is covered by some ticket.
    covered = {r for t in [*tickets, *existing] for r in t.spec_refs}
    for ident in spec_ids:
        if ident.startswith("AC-") and ident not in covered:
            bad(None, f"{ident} is not covered by any ticket's spec_refs")

    # Each implementer's context pack must fit its budget.
    for t in tickets:
        try:
            implementer_pack(Ticket.from_spec(t), root, budget)
        except ContextBudgetExceeded as e:
            bad(t.id, str(e))
    return issues

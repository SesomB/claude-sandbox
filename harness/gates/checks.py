"""Mechanical gates: deterministic checks the harness trusts instead of agent claims."""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.executors.base import CommandResult, Executor
from harness.stacks import StackProfile, Step
from harness.tools.base import tail
from harness.tools.workspace import TicketWorkspace


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class GateReport:
    results: list[GateResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.ok]

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append(GateResult(name, ok, detail))
        return ok

    def summary(self) -> str:
        lines = []
        for r in self.results:
            lines.append(f"{'PASS' if r.ok else 'FAIL'}  {r.name}")
            if not r.ok and r.detail:
                lines.append(r.detail.rstrip())
        return "\n".join(lines)

    def as_payload(self) -> dict[str, object]:
        return {"ok": self.ok, "results": [r.__dict__ for r in self.results]}


def describe(res: CommandResult) -> str:
    status = "timed out" if res.timed_out else f"exit {res.exit_code}"
    out = "\n".join(x for x in (res.stdout.strip(), res.stderr.strip()) if x)
    return f"$ {res.command}  ({status})\n{tail(out)}"


async def run_step(
    stack: StackProfile,
    step: Step,
    executor: Executor,
    report: GateReport,
    *,
    files: list[str] | None = None,
    test_id: str | None = None,
) -> bool:
    """Run one stack step into ``report``. Undefined steps (or no files to check) pass."""
    template = getattr(stack, step)
    if template is None or ("{files}" in template and not files):
        return True
    res = await stack.run(step, executor, files=files, test_id=test_id)
    assert res is not None
    return report.add(f"{stack.name}:{step}", res.ok, "" if res.ok else describe(res))


async def build_and_test(
    stacks: list[StackProfile],
    executor: Executor,
    report: GateReport,
    *,
    changed: list[str] | None = None,
) -> None:
    """configure → build → typecheck/lint (on changed files) → full test suite, per stack."""
    changed = changed or []
    for stack in stacks:
        files = [
            p
            for p in changed
            if (stack.is_source_file(p) or stack.is_test_file(p)) and executor.exists(p)
        ]
        if not await run_step(stack, "configure", executor, report):
            continue
        if not await run_step(stack, "build", executor, report):
            continue
        if stack.typecheck != stack.build:
            await run_step(stack, "typecheck", executor, report, files=files)
        await run_step(stack, "lint", executor, report, files=files)
        await run_step(stack, "test", executor, report)


async def format_files(ws: TicketWorkspace, paths: list[str]) -> None:
    """Auto-format changed files mechanically, so style never costs a review round."""
    for stack in ws.stacks:
        files = [p for p in paths if stack.is_source_file(p) and (ws.root / p).exists()]
        if files and stack.format:
            await stack.run("format", ws.executor, files=files)


async def run_gates(ws: TicketWorkspace) -> GateReport:
    """All gates for a ticket's current working tree."""
    report = GateReport()
    changed = ws.changed()
    violations = ws.scope_violations(changed)
    report.add(
        "scope",
        not violations,
        f"changed files outside the ticket's scope: {', '.join(violations)}" if violations else "",
    )
    missing = [p for p in ws.ticket.tests_required if not (ws.root / p).is_file()]
    report.add("tests_required", not missing, f"missing: {', '.join(missing)}" if missing else "")
    touched = sorted(set(changed) | set(ws.branch_changes()))
    await build_and_test(ws.stacks, ws.executor, report, changed=touched)
    return report

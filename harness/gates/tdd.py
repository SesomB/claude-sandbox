"""Red → green enforcement for bug fixes.

For every bug finding, the Fixer must first submit a test that the harness itself
runs and sees FAIL on the unfixed code (red). The test is then committed and frozen.
Only after that may the code be changed, and the harness must see that test and the
whole suite PASS (green) before the fix is committed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from harness.gates.checks import GateReport, build_and_test, describe, run_step
from harness.stacks import StackProfile
from harness.tools.base import Tool, ToolError
from harness.tools.workspace import TicketWorkspace


@dataclass
class RedRecord:
    finding_id: str
    test_id: str
    test_files: list[str]
    stack: StackProfile
    red_commit: str | None
    green_commit: str | None = None

    @property
    def green(self) -> bool:
        return self.green_commit is not None


@dataclass
class TddTracker:
    ws: TicketWorkspace
    bug_findings: set[str]
    records: dict[str, RedRecord] = field(default_factory=dict)

    def missing_green(self) -> list[str]:
        """Bug findings without a verified red→green cycle."""
        return sorted(f for f in self.bug_findings if not self._is_green(f))

    def _is_green(self, finding_id: str) -> bool:
        rec = self.records.get(finding_id)
        return rec is not None and rec.green

    def _stack_for_tests(self, test_files: list[str]) -> StackProfile:
        for stack in self.ws.stacks:
            if stack.test_one and all(stack.is_test_file(p) for p in test_files):
                return stack
        raise ToolError(
            "the test files don't match any stack's test file patterns: "
            + ", ".join(f"{s.name}: {s.test_globs}" for s in self.ws.stacks)
        )

    async def _build(self, stack: StackProfile) -> GateReport:
        report = GateReport()
        if await run_step(stack, "configure", self.ws.executor, report):
            await run_step(stack, "build", self.ws.executor, report)
        return report

    async def submit_red(self, finding_id: str, test_id: str, test_files: list[str]) -> str:
        ws = self.ws
        if finding_id not in self.bug_findings:
            raise ToolError(f"{finding_id} is not a bug finding of this review")
        if finding_id in self.records:
            raise ToolError(f"a failing test for {finding_id} was already verified")
        test_files = [ws.check_write(p) for p in test_files]
        stack = self._stack_for_tests(test_files)

        source_changes = [p for p in ws.changed() if p not in test_files and not ws.is_shared(p)]
        if source_changes:
            raise ToolError(
                "submit the failing test BEFORE changing code. Revert these first: "
                + ", ".join(source_changes)
            )
        build = await self._build(stack)
        if not build.ok:
            raise ToolError("the test must build/compile against current code:\n" + build.summary())
        res = await stack.run("test_one", ws.executor, test_id=test_id)
        assert res is not None
        if res.ok:
            raise ToolError(
                f"{test_id} PASSES on the current code, so it does not reproduce {finding_id}. "
                "Write a test that fails because of the bug.\n" + describe(res)
            )
        if res.timed_out or not stack.is_test_failure(res.exit_code):
            raise ToolError(
                "the test command errored instead of reporting a failing test (crash, missing "
                "test, or collection error):\n" + describe(res)
            )
        changed = [p for p in ws.changed() if p in test_files or ws.is_shared(p)]
        sha = ws.commit(f"test({ws.ticket.id}): reproduce {finding_id}", changed)
        ws.frozen.update(test_files)
        self.records[finding_id] = RedRecord(finding_id, test_id, test_files, stack, sha)
        ws.board.log_event(
            ws.ticket.id, "tdd.red_verified", finding=finding_id, test_id=test_id, commit=sha
        )
        return (
            f"RED verified: {test_id} fails on the current code. The test is committed and "
            f"frozen. Now fix the code, then call tdd_submit_green for {finding_id}."
        )

    async def submit_green(self, finding_id: str) -> str:
        ws = self.ws
        rec = self.records.get(finding_id)
        if rec is None:
            raise ToolError(f"no verified failing test for {finding_id}; call tdd_submit_red first")
        if rec.green:
            raise ToolError(f"{finding_id} is already green")
        if not ws.changed():
            raise ToolError("no code changes since the failing test; fix the code first")
        build = await self._build(rec.stack)
        if not build.ok:
            raise ToolError("build failed:\n" + build.summary())
        res = await rec.stack.run("test_one", ws.executor, test_id=rec.test_id)
        assert res is not None
        if not res.ok:
            raise ToolError(f"{rec.test_id} still fails:\n" + describe(res))
        suite = GateReport()
        await build_and_test(ws.stacks, ws.executor, suite)
        if not suite.ok:
            raise ToolError(
                "the reproducing test passes, but the suite regressed:\n" + suite.summary()
            )
        sha = ws.commit(f"fix({ws.ticket.id}): {finding_id}")
        rec.green_commit = sha or "unchanged"
        ws.board.log_event(
            ws.ticket.id, "tdd.green_verified", finding=finding_id, test_id=rec.test_id, commit=sha
        )
        return f"GREEN verified for {finding_id}: the test and the full suite pass. Fix committed."


class SubmitRedInput(BaseModel):
    finding_id: str = Field(description="The bug finding this test reproduces, e.g. F-1")
    test_id: str = Field(description="How to run just this test (pytest node id / CTest name)")
    test_files: list[str] = Field(description="Test files you wrote or changed for it")


class SubmitGreenInput(BaseModel):
    finding_id: str


def tdd_tools(tracker: TddTracker) -> list[Tool]:
    async def red(args: SubmitRedInput) -> str:
        return await tracker.submit_red(args.finding_id, args.test_id, args.test_files)

    async def green(args: SubmitGreenInput) -> str:
        return await tracker.submit_green(args.finding_id)

    return [
        Tool(
            "tdd_submit_red",
            "Step 1 for a bug: submit a new test that reproduces it. The harness runs it on "
            "the unfixed code and accepts it only if it FAILS. Do not change code before this.",
            SubmitRedInput,
            red,
        ),
        Tool(
            "tdd_submit_green",
            "Step 2 for a bug: after fixing the code, the harness checks the reproducing "
            "test and the full suite PASS, then commits the fix.",
            SubmitGreenInput,
            green,
        ),
    ]

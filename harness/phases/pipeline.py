"""The upstream waterfall phases: spec → architecture → plan, each ending at a Board gate.

Each phase runs a writer agent and a critic (agent and/or mechanical checks) in rounds,
then asks the human to approve. Rejection feedback starts another round. Approved phase
output lands on main through the merge queue; an approved plan goes onto the board.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import yaml
from pydantic import BaseModel

from harness.agents.loop import AgentRun, RetryPolicy, run_agent
from harness.board import Ticket
from harness.context.builder import ARCHITECTURE_PATH, CONVENTIONS_PATH, SPEC_PATH
from harness.core.merge import MergeQueue
from harness.core.runlog import log_agent_run
from harness.escalation.decisions import Human, ask_user_tool, request_approval
from harness.executors.local import LocalExecutor
from harness.executors.worktree import Worktree, changed_files, commit_paths
from harness.gates.checks import GateReport, build_and_test
from harness.models import ModelClient, client_for_role
from harness.phases.roles import (
    ARCHITECT_SYSTEM,
    DECOMPOSER_SYSTEM,
    PLAN_CRITIC_SYSTEM,
    SPEC_CRITIC_SYSTEM,
    SPEC_WRITER_SYSTEM,
    ArchitectureDone,
    Critique,
    PhaseDone,
    Plan,
)
from harness.phases.spec_lint import lint_spec
from harness.phases.ticket_lint import lint_plan
from harness.phases.tools import check_stacks_tool, phase_file_tools
from harness.project import Project, set_stacks_in_config
from harness.stacks import StackProfile, load_profiles
from harness.tools.base import Tool, Toolbox

R = TypeVar("R", bound=BaseModel)
PHASES = ("spec", "architecture", "plan", "build")
PHASE_KEY = "phase"


@dataclass
class PhaseResult:
    ok: bool
    message: str


class PhaseError(Exception):
    pass


def _issues_text(critique: Critique | None) -> str:
    if critique is None or not critique.issues:
        return ""
    return "\n".join(
        f"- [{i.severity}] {i.kind} at {i.where}: {i.detail}"
        + (f" (suggestion: {i.suggestion})" if i.suggestion else "")
        for i in critique.issues
    )


class Pipeline:
    def __init__(
        self,
        project: Project,
        human: Human,
        *,
        client_factory: Callable[[str], ModelClient] | None = None,
        retry: RetryPolicy | None = None,
        max_rounds: int = 3,
        max_approvals: int = 5,
    ) -> None:
        self.project = project
        self.board = project.board
        self.human = human
        self.client_factory = client_factory or (lambda role: client_for_role(project.config, role))
        self.retry = retry
        self.max_rounds = max_rounds
        self.max_approvals = max_approvals
        self._ids = itertools.count(1)

    # -- phase state ----------------------------------------------------------------------

    @property
    def phase(self) -> str:
        return self.board.get_meta(PHASE_KEY, "spec") or "spec"

    def _set_phase(self, phase: str) -> None:
        self.board.set_meta(PHASE_KEY, phase)
        self.board.log_event(None, "phase.entered", phase=phase)

    def _require(self, phase: str) -> None:
        current = self.phase
        if PHASES.index(current) < PHASES.index(phase):
            raise PhaseError(f"the project is in the {current} phase; finish it before {phase}")
        if current == "build" and phase != "build":
            raise PhaseError("tickets are already on the board; earlier phases are closed")

    # -- helpers -------------------------------------------------------------------------------

    def _worktree(self, phase: str) -> Worktree:
        return self.project.worktrees.create(f"phase-{phase}", branch=f"phase/{phase}")

    async def _agent(
        self,
        role: str,
        *,
        key: str,
        system: str,
        prompt: str,
        tools: list[Tool],
        result_model: type[R],
        finish: str,
        on_finish: Callable[[R], Awaitable[str | None]] | None = None,
    ) -> AgentRun[R]:
        async def accept(_: R) -> str | None:
            return None

        run = await run_agent(
            self.client_factory(role),
            system=system,
            prompt=prompt,
            toolbox=Toolbox(tools),
            result_model=result_model,
            finish_description=finish,
            on_finish=on_finish or accept,
            max_turns=self.project.config.orchestrator.agent_max_turns,
            max_tokens=self.project.config.model_for(role).max_tokens,
            retry=self.retry,
        )
        log_agent_run(self.project, key, role, f"{role}-{next(self._ids)}", run)
        return run

    async def _land(
        self, phase: str, wt: Worktree, message: str, stacks: list[StackProfile]
    ) -> None:
        paths = [p for p in changed_files(wt.path) if not any(s.is_artifact(p) for s in stacks)]
        commit_paths(wt.path, paths, message)
        queue = MergeQueue(self.project.root, self.board, stacks, self.project.worktrees)
        result = await queue.merge_branch(wt.branch, message=f"Merge {phase} phase", stacks=stacks)
        if not result.ok:
            raise PhaseError(f"could not land the {phase} phase: {result.stage}: {result.detail}")
        self.project.worktrees.remove(wt.ticket_id, delete_branch=True, branch=wt.branch)

    @staticmethod
    def _read_tools(tools: list[Tool]) -> list[Tool]:
        return [t for t in tools if t.name in ("read_file", "list_files")]

    # -- phase 1: spec -------------------------------------------------------------------------

    async def spec(self, idea: str) -> PhaseResult:
        self._require("spec")
        wt = self._worktree("spec")
        ex = wt.executor()
        files = phase_file_tools(ex, can_write=lambda p: p == SPEC_PATH, writable_hint=SPEC_PATH)
        ask = ask_user_tool(self.board, self.human, phase="spec")

        async def check_spec(_: PhaseDone) -> str | None:
            if not ex.exists(SPEC_PATH):
                return f"write the spec to {SPEC_PATH} first"
            issues = lint_spec(ex.read_text(SPEC_PATH))
            if issues:
                return "The spec failed mechanical checks:\n" + "\n".join(f"- {i}" for i in issues)
            return None

        feedback = ""
        for _ in range(self.max_approvals):
            critique: Critique | None = None
            writer_summary = ""
            for _ in range(self.max_rounds):
                prompt = f"Product idea from the owner:\n\n{idea}"
                if ex.exists(SPEC_PATH):
                    prompt += f"\n\nA draft already exists at {SPEC_PATH}; revise it."
                if critique is not None:
                    prompt += f"\n\nThe spec critic found these issues:\n{_issues_text(critique)}"
                if feedback:
                    prompt += (
                        f"\n\nThe owner rejected the last version with this feedback:\n{feedback}"
                    )
                writer = await self._agent(
                    "spec_writer",
                    key="phase-spec",
                    system=SPEC_WRITER_SYSTEM,
                    prompt=prompt,
                    tools=[*files, ask],
                    result_model=PhaseDone,
                    finish="Call when docs/spec.md is complete.",
                    on_finish=check_spec,
                )
                if writer.result is None:
                    return PhaseResult(False, f"spec writer {writer.outcome}: {writer.reason}")
                writer_summary = writer.result.summary
                critic = await self._agent(
                    "spec_critic",
                    key="phase-spec",
                    system=SPEC_CRITIC_SYSTEM,
                    prompt=f"Review this spec:\n\n{ex.read_text(SPEC_PATH)}",
                    tools=self._read_tools(files),
                    result_model=Critique,
                    finish="Submit your verdict.",
                )
                if critic.result is None:
                    return PhaseResult(False, f"spec critic {critic.outcome}: {critic.reason}")
                critique = critic.result
                if critique.verdict == "approve":
                    break
            summary = f"{writer_summary}\n\nSpec: {wt.path / SPEC_PATH}"
            if critique is not None and critique.issues:
                summary += f"\n\nOpen critic notes:\n{_issues_text(critique)}"
            approval = await request_approval(self.board, self.human, "spec", summary)
            if approval.approved:
                await self._land("spec", wt, "Spec approved", [])
                self._set_phase("architecture")
                return PhaseResult(True, "spec approved and committed to main")
            feedback = approval.feedback
        return PhaseResult(False, "spec not approved")

    # -- phase 2: architecture -----------------------------------------------------------------

    async def architecture(self) -> PhaseResult:
        self._require("architecture")
        wt = self._worktree("architecture")
        ex = wt.executor()
        profiles = load_profiles(wt.path)
        files = phase_file_tools(
            ex, can_write=lambda p: p != SPEC_PATH, writable_hint="anything except the spec"
        )
        tools = [
            *files,
            check_stacks_tool(ex, profiles),
            ask_user_tool(self.board, self.human, phase="architecture"),
        ]

        async def check(done: ArchitectureDone) -> str | None:
            problems = [
                f"missing {p}" for p in (ARCHITECTURE_PATH, CONVENTIONS_PATH) if not ex.exists(p)
            ]
            unknown = [s for s in done.stacks if s not in profiles]
            if unknown:
                problems.append(f"unknown stacks {unknown}; available: {sorted(profiles)}")
            if not done.stacks:
                problems.append("choose at least one stack")
            if problems:
                return "Not done yet:\n" + "\n".join(f"- {p}" for p in problems)
            report = GateReport()
            await build_and_test([profiles[s] for s in done.stacks], ex, report)
            if not report.ok:
                return "The skeleton does not pass its gates:\n" + report.summary()
            return None

        feedback = ""
        for _ in range(self.max_approvals):
            prompt = (
                "Available stack profiles: "
                + ", ".join(f"{n} ({p.description})" for n, p in sorted(profiles.items()))
                + f"\n\nThe approved spec ({SPEC_PATH}):\n\n{ex.read_text(SPEC_PATH)}"
            )
            if ex.exists(ARCHITECTURE_PATH):
                prompt += "\n\nA draft architecture exists; revise it."
            if feedback:
                prompt += f"\n\nThe owner rejected the last version with this feedback:\n{feedback}"
            run = await self._agent(
                "architect",
                key="phase-architecture",
                system=ARCHITECT_SYSTEM,
                prompt=prompt,
                tools=tools,
                result_model=ArchitectureDone,
                finish="Call when the architecture docs and a passing skeleton are done.",
                on_finish=check,
            )
            if run.result is None:
                return PhaseResult(False, f"architect {run.outcome}: {run.reason}")
            stack_names = run.result.stacks
            approval = await request_approval(
                self.board,
                self.human,
                "architecture",
                f"{run.result.summary}\n\nStacks: {', '.join(stack_names)}\n"
                f"Architecture: {wt.path / ARCHITECTURE_PATH}",
            )
            if approval.approved:
                set_stacks_in_config(wt.path / ".harness" / "config.yaml", stack_names)
                await self._land(
                    "architecture",
                    wt,
                    "Architecture and skeleton approved",
                    [profiles[s] for s in stack_names],
                )
                self.project.reload()
                self._set_phase("plan")
                return PhaseResult(True, f"architecture approved; stacks: {stack_names}")
            feedback = approval.feedback
        return PhaseResult(False, "architecture not approved")

    # -- phase 3: plan (decomposition into tickets) ----------------------------------------------

    async def plan(self) -> PhaseResult:
        self._require("plan")
        root = self.project.root
        ex = LocalExecutor(root)
        read_tools = self._read_tools(
            phase_file_tools(ex, can_write=lambda p: False, writable_hint="nothing")
        )
        stacks = self.project.stacks()
        budget = self.project.config.context_budget("implementer", fallback=16_000)
        spec = ex.read_text(SPEC_PATH)
        docs = "\n\n".join(
            f"# {p}\n\n{ex.read_text(p)}"
            for p in (SPEC_PATH, ARCHITECTURE_PATH, CONVENTIONS_PATH)
            if ex.exists(p)
        )
        stack_info = "\n".join(
            f"- {s.name}: test files {s.test_globs}; shared files (never owned) {s.shared_files}"
            for s in stacks
        )

        async def lint(plan: Plan) -> str | None:
            issues = lint_plan(
                plan.tickets,
                root=root,
                spec=spec,
                stacks=stacks,
                budget=budget,
                existing=self.board.tickets(),
            )
            if issues:
                return "The ticket linter rejected the plan:\n" + "\n".join(
                    f"- {i}" for i in issues
                )
            return None

        feedback = ""
        plan: Plan | None = None
        for _ in range(self.max_approvals):
            critique: Critique | None = None
            for _ in range(self.max_rounds):
                prompt = f"{docs}\n\n# Stacks\n{stack_info}\n\n# Repository files\n" + "\n".join(
                    f for f in ex.list_files() if not f.startswith(".harness/")
                )
                if plan is not None:
                    prompt += "\n\n# Your previous plan\n" + _plan_yaml(plan)
                if critique is not None:
                    prompt += f"\n\n# Plan critic's issues\n{_issues_text(critique)}"
                if feedback:
                    prompt += f"\n\n# The owner's feedback on the last plan\n{feedback}"
                run = await self._agent(
                    "decomposer",
                    key="phase-plan",
                    system=DECOMPOSER_SYSTEM,
                    prompt=prompt,
                    tools=read_tools,
                    result_model=Plan,
                    finish="Submit the full list of tickets.",
                    on_finish=lint,
                )
                if run.result is None:
                    return PhaseResult(False, f"decomposer {run.outcome}: {run.reason}")
                plan = run.result
                critic = await self._agent(
                    "plan_critic",
                    key="phase-plan",
                    system=PLAN_CRITIC_SYSTEM,
                    prompt=f"{docs}\n\n# Proposed tickets\n{_plan_yaml(plan)}",
                    tools=read_tools,
                    result_model=Critique,
                    finish="Submit your verdict.",
                )
                if critic.result is None:
                    return PhaseResult(False, f"plan critic {critic.outcome}: {critic.reason}")
                critique = critic.result
                if critique.verdict == "approve":
                    break
            assert plan is not None
            summary = f"{len(plan.tickets)} tickets. {plan.summary}\n\n" + "\n".join(
                f"{t.id}: {t.title}"
                + (f"  (after {', '.join(t.depends_on)})" if t.depends_on else "")
                for t in plan.tickets
            )
            if critique is not None and critique.issues:
                summary += f"\n\nOpen critic notes:\n{_issues_text(critique)}"
            approval = await request_approval(self.board, self.human, "plan", summary)
            if approval.approved:
                self.board.add_tickets([Ticket.from_spec(t) for t in plan.tickets])
                for t in plan.tickets:
                    self.board.queue(t.id)
                self._set_phase("build")
                return PhaseResult(True, f"{len(plan.tickets)} tickets queued")
            feedback = approval.feedback
        return PhaseResult(False, "plan not approved")


def _plan_yaml(plan: Plan) -> str:
    data = [json.loads(t.model_dump_json(exclude_defaults=True)) for t in plan.tickets]
    return yaml.safe_dump(data, sort_keys=False)

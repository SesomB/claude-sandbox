"""The orchestrator: tails the Board's event log, runs hooks, and runs the role workers.

Every worker is a fresh agent with a minimal context pack. Workers claim tickets
atomically and hold them under a lease that a heartbeat keeps alive, so a crashed
worker's ticket goes back on the board for another one.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from harness.agents.loop import AgentAbort, AgentRun, RetryPolicy, run_agent
from harness.agents.roles import (
    FINISH_DESCRIPTIONS,
    FIXER_SYSTEM,
    IMPLEMENTER_SYSTEM,
    REVIEWER_SYSTEM,
    FixerResult,
    ImplementerResult,
    ReviewResult,
)
from harness.board import BoardError, NotHolder, Ticket, TicketStatus
from harness.board.models import TERMINAL_STATUSES
from harness.context.builder import (
    ContextBudgetExceeded,
    fixer_pack,
    implementer_pack,
    reviewer_pack,
)
from harness.core.hooks import ROLE_FOR_EVENT, HookRegistry, default_hooks
from harness.core.merge import MergeQueue, group_ops, merge_ref
from harness.core.runlog import log_agent_run
from harness.escalation.decisions import DecisionRequest
from harness.escalation.researcher import (
    RESEARCHER_SYSTEM,
    escalation_context,
    fallback_request,
)
from harness.executors.local import LocalExecutor
from harness.executors.worktree import git
from harness.gates.checks import GateReport, build_and_test, format_files, run_gates
from harness.gates.tdd import TddTracker, tdd_tools
from harness.learning.learner import Learner
from harness.models import ModelClient, client_for_role
from harness.phases.tools import phase_file_tools
from harness.project import Project
from harness.tools.base import Toolbox
from harness.tools.files import read_tools, write_tools
from harness.tools.workspace import BASE_BRANCH, TicketWorkspace

S = TicketStatus
ClientFactory = Callable[[str], ModelClient]
LessonsProvider = Callable[[Ticket, str], list[str]]

CURSOR_KEY = "orchestrator.cursor"
CLAIMABLE = {
    S.READY: "implementer",
    S.AWAITING_REVIEW: "reviewer",
    S.CHANGES_REQUESTED: "fixer",
    S.APPROVED: "merger",
}
assert set(ROLE_FOR_EVENT.values()) == set(CLAIMABLE.values())


@dataclass
class RunSummary:
    seconds: float = 0.0
    agent_runs: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


class Orchestrator:
    def __init__(
        self,
        project: Project,
        *,
        client_factory: ClientFactory | None = None,
        hooks: HookRegistry | None = None,
        retry: RetryPolicy | None = None,
        lessons: LessonsProvider | None = None,
        poll_interval_s: float = 0.2,
    ) -> None:
        self.project = project
        self.board = project.board
        self.cfg = project.config.orchestrator
        self.client_factory = client_factory or (lambda role: client_for_role(project.config, role))
        self.hooks = hooks or default_hooks()
        self.retry = retry
        self.learner = Learner(project, client_factory=self.client_factory, retry=retry)
        self.lessons = lessons or self.learner.lessons_for
        self.poll_interval_s = poll_interval_s
        self.stacks = project.stacks()
        self.merge_queue = MergeQueue(project.root, self.board, self.stacks, project.worktrees)
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._sem = (
            asyncio.Semaphore(self.cfg.max_parallel_agents)
            if self.cfg.max_parallel_agents
            else None
        )
        self._ids = itertools.count(1)
        self.agent_runs = 0

    # -- main loop ------------------------------------------------------------------------

    async def run(self, *, until_idle: bool = True, timeout_s: float | None = None) -> RunSummary:
        start = time.monotonic()
        if self.board.get_meta(CURSOR_KEY) is None:
            events = self.board.events(after_seq=0, limit=1_000_000)
            self.board.set_meta(CURSOR_KEY, str(events[-1].seq if events else 0))
        self.reconcile()
        try:
            while True:
                dispatched = await self.dispatch_events()
                self.board.expire_leases()
                self._reap()
                if not self._tasks and self._claimable():
                    self.reconcile()  # e.g. a hook failed to spawn a worker
                if until_idle and not dispatched and not self._tasks and not self._claimable():
                    break
                if timeout_s is not None and time.monotonic() - start > timeout_s:
                    break
                await asyncio.sleep(self.poll_interval_s)
        finally:
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.learner.sync()  # learn from whatever happened last
        counts: dict[str, int] = {}
        for t in self.board.tickets():
            counts[t.status.value] = counts.get(t.status.value, 0) + 1
        return RunSummary(time.monotonic() - start, self.agent_runs, counts)

    async def dispatch_events(self) -> int:
        cursor = int(self.board.get_meta(CURSOR_KEY, "0") or 0)
        events = self.board.events(after_seq=cursor)
        for event in events:
            for hook in self.hooks.handlers(event.type):
                try:
                    await hook(event, self)
                except Exception as e:  # a broken hook must not stop the pipeline
                    self.board.log_event(
                        event.ticket_id, "hook.error", event=event.type, error=repr(e)
                    )
            cursor = event.seq
        if events:
            self.board.set_meta(CURSOR_KEY, str(cursor))
        return len(events)

    def reconcile(self) -> None:
        """Spawn workers for every claimable ticket (covers restarts and missed events)."""
        for status, role in CLAIMABLE.items():
            for t in self.board.tickets(status):
                self.spawn(role, t.id)

    def _claimable(self) -> bool:
        return any(self.board.tickets(status) for status in CLAIMABLE)

    def _reap(self) -> None:
        for key in [k for k, t in self._tasks.items() if t.done()]:
            del self._tasks[key]

    def spawn(self, role: str, ticket_id: str) -> None:
        key = (role, ticket_id)
        if key in self._tasks and not self._tasks[key].done():
            return
        self._tasks[key] = asyncio.create_task(self._run_worker(role, ticket_id))

    async def _run_worker(self, role: str, ticket_id: str) -> None:
        worker = {
            "implementer": self.implement,
            "reviewer": self.review,
            "fixer": self.fix,
            "merger": self.merge,
            "researcher": self.research,
            "librarian": self.learn,
        }[role]
        sem = self._sem or contextlib.nullcontext()
        async with sem:
            try:
                await worker(ticket_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.board.log_event(ticket_id, "agent.crashed", role=role, error=repr(e))
                self._escalate(ticket_id, f"{role} crashed: {e!r}")

    # -- helpers ----------------------------------------------------------------------------

    def _escalate(self, ticket_id: str, reason: str, **payload: Any) -> None:
        try:
            status = self.board.get(ticket_id).status
            if status not in TERMINAL_STATUSES and status is not S.ESCALATED:
                self.board.escalate(ticket_id, reason, **payload)
        except BoardError:
            pass

    def _last_payload(self, ticket_id: str, type: str, key: str) -> dict[str, Any]:
        """Payload of the latest ``type`` event carrying ``key`` (skips lease expiries)."""
        for event in reversed(self.board.events_for(ticket_id)):
            if event.type == type and key in event.payload:
                return event.payload
        return {}

    def _since_escalation(self, ticket_id: str, type: str, **match: Any) -> int:
        """Count matching events since the ticket's last escalation (a Board reset)."""
        count = 0
        for event in self.board.events_for(ticket_id):
            if event.type == "ticket.escalated":
                count = 0
            elif event.type == type and all(event.payload.get(k) == v for k, v in match.items()):
                count += 1
        return count

    def _agent_name(self, role: str) -> str:
        return f"{role}-{next(self._ids)}"

    @contextlib.asynccontextmanager
    async def _hold(self, ticket_id: str, agent: str) -> AsyncIterator[None]:
        """Keep the lease alive while the body runs; cancel the body if the lease is lost."""
        body = asyncio.current_task()
        lease_s = self.cfg.lease_s

        async def beat() -> None:
            while True:
                await asyncio.sleep(lease_s / 3)
                try:
                    self.board.heartbeat(ticket_id, agent, lease_s=lease_s)
                except NotHolder:
                    if body is not None:
                        body.cancel()
                    return

        hb = asyncio.create_task(beat())
        try:
            yield
        finally:
            hb.cancel()

    def _workspace(self, ticket: Ticket) -> TicketWorkspace:
        wt = self.project.worktrees.create(ticket.id, base=BASE_BRANCH)
        return TicketWorkspace(ticket, wt, self.board, self.stacks)

    def _budget(self, role: str) -> int:
        return self.project.config.context_budget(role, fallback=16_000)

    def _log_run(self, ticket_id: str, role: str, agent: str, run: AgentRun[Any]) -> None:
        self.agent_runs += 1
        log_agent_run(
            self.project,
            ticket_id,
            role,
            agent,
            run,
            ticket_id=ticket_id,
            model=self.project.config.model_for(role).model,
        )

    async def _gates_on_finish(
        self, ws: TicketWorkspace, role: str, attempts: list[int]
    ) -> str | None:
        await format_files(ws, ws.changed())
        report = await run_gates(ws)
        self.board.log_event(
            ws.ticket.id,
            "gates.passed" if report.ok else "gates.failed",
            role=role,
            **report.as_payload(),
        )
        if report.ok:
            return None
        attempts[0] += 1
        if attempts[0] >= self.cfg.max_gate_attempts:
            raise AgentAbort(
                f"gates still failing after {attempts[0]} attempts:\n{report.summary()}"
            )
        return (
            "The harness ran the gates and they FAILED. Fix this, then call finish again.\n\n"
            + (report.summary())
        )

    def _client(self, role: str) -> tuple[ModelClient, int]:
        return self.client_factory(role), self.project.config.model_for(role).max_tokens

    # -- workers ------------------------------------------------------------------------------

    async def implement(self, ticket_id: str) -> None:
        agent = self._agent_name("implementer")
        ticket = self.board.claim(agent, S.READY, ticket_id=ticket_id, lease_s=self.cfg.lease_s)
        if ticket is None:
            return
        async with self._hold(ticket_id, agent):
            ws = self._workspace(ticket)
            try:
                prompt = implementer_pack(
                    ticket,
                    ws.root,
                    self._budget("implementer"),
                    self.lessons(ticket, "implementer"),
                    guidance=str(
                        self._last_payload(ticket_id, "ticket.ready", "guidance").get(
                            "guidance", ""
                        )
                    ),
                )
            except ContextBudgetExceeded as e:
                self.board.escalate(ticket_id, str(e), kind="ticket_too_large")
                return
            attempts = [0]

            async def on_finish(_: ImplementerResult) -> str | None:
                return await self._gates_on_finish(ws, "implementer", attempts)

            client, max_tokens = self._client("implementer")
            run = await run_agent(
                client,
                system=IMPLEMENTER_SYSTEM + self.learner.skills_for("implementer"),
                prompt=prompt,
                toolbox=Toolbox([*read_tools(ws), *write_tools(ws)]),
                result_model=ImplementerResult,
                finish_description=FINISH_DESCRIPTIONS["implementer"],
                on_finish=on_finish,
                max_turns=self.cfg.agent_max_turns,
                max_tokens=max_tokens,
                retry=self.retry,
            )
            self._log_run(ticket_id, "implementer", agent, run)
            if run.outcome != "finished" or run.result is None:
                self.board.escalate(ticket_id, f"implementer {run.outcome}: {run.reason}")
                return
            ws.commit(f"{ticket.id}: {ticket.title}\n\n{run.result.summary}")
            self.board.complete(ticket_id, agent, S.AWAITING_REVIEW, summary=run.result.summary)

    async def review(self, ticket_id: str) -> None:
        agent = self._agent_name("reviewer")
        ticket = self.board.claim(
            agent, S.AWAITING_REVIEW, ticket_id=ticket_id, lease_s=self.cfg.lease_s
        )
        if ticket is None:
            return
        async with self._hold(ticket_id, agent):
            ws = self._workspace(ticket)
            report = GateReport()
            await build_and_test(ws.stacks, ws.executor, report, changed=ws.branch_changes())
            if not report.ok:
                self.board.complete(
                    ticket_id, agent, S.CHANGES_REQUESTED, source="gates", detail=report.summary()
                )
                return
            last = self._last_payload(ticket_id, "ticket.awaiting_review", "summary")
            summary = str(last.get("summary", ""))
            try:
                prompt = reviewer_pack(
                    ticket,
                    ws.root,
                    self._budget("reviewer"),
                    diff=ws.diff(),
                    gate_summary=report.summary(),
                    implementer_summary=summary,
                    lessons=self.lessons(ticket, "reviewer"),
                )
            except ContextBudgetExceeded as e:
                self.board.escalate(ticket_id, str(e), kind="ticket_too_large")
                return

            async def on_finish(_: ReviewResult) -> str | None:
                return None

            client, max_tokens = self._client("reviewer")
            run = await run_agent(
                client,
                system=REVIEWER_SYSTEM + self.learner.skills_for("reviewer"),
                prompt=prompt,
                toolbox=Toolbox(read_tools(ws)),
                result_model=ReviewResult,
                finish_description=FINISH_DESCRIPTIONS["reviewer"],
                on_finish=on_finish,
                max_turns=self.cfg.agent_max_turns,
                max_tokens=max_tokens,
                retry=self.retry,
            )
            self._log_run(ticket_id, "reviewer", agent, run)
            if run.outcome != "finished" or run.result is None:
                self.board.escalate(ticket_id, f"reviewer {run.outcome}: {run.reason}")
                return
            result = run.result
            if result.verdict == "approve":
                self.board.complete(
                    ticket_id,
                    agent,
                    S.APPROVED,
                    summary=result.summary,
                    findings=[f.model_dump() for f in result.findings],
                )
            else:
                self.board.complete(
                    ticket_id,
                    agent,
                    S.CHANGES_REQUESTED,
                    source="review",
                    summary=result.summary,
                    findings=[f.model_dump() for f in result.findings],
                )

    async def fix(self, ticket_id: str) -> None:
        current = self.board.get(ticket_id)
        if current.review_rounds > self.cfg.max_review_rounds:
            self._escalate(
                ticket_id,
                f"still not approved after {current.review_rounds} review rounds",
                kind="review_rounds",
            )
            return
        merge_failures = self._since_escalation(
            ticket_id, "ticket.changes_requested", source="merge"
        )
        if merge_failures > self.cfg.max_merge_failures:
            self._escalate(ticket_id, f"merge failed {merge_failures} times", kind="merge")
            return
        agent = self._agent_name("fixer")
        ticket = self.board.claim(
            agent, S.CHANGES_REQUESTED, ticket_id=ticket_id, lease_s=self.cfg.lease_s
        )
        if ticket is None:
            return
        async with self._hold(ticket_id, agent):
            ws = self._workspace(ticket)
            # A fresh fixer starts from the committed state, not a crashed agent's leftovers.
            git(ws.root, "reset", "-q", "--hard")
            git(ws.root, "clean", "-q", "-fd")
            synced = merge_ref(
                ws.root,
                BASE_BRANCH,
                message=f"Merge {BASE_BRANCH} into {ws.worktree.branch}",
                main_side="theirs",
                ops=group_ops(self.board.shared_edits(ticket_id)),
                is_shared=ws.is_shared,
            )
            if not synced.ok:
                self.board.escalate(ticket_id, f"cannot sync with main: {synced.detail}")
                return
            payload = self._last_payload(ticket_id, "ticket.changes_requested", "source")
            findings: list[dict[str, Any]] = list(payload.get("findings") or [])
            failure = "" if payload.get("source") == "review" else str(payload.get("detail", ""))
            try:
                prompt = fixer_pack(
                    ticket,
                    ws.root,
                    self._budget("fixer"),
                    diff=ws.diff(),
                    findings=findings,
                    merge_failure=failure,
                    lessons=self.lessons(ticket, "fixer"),
                )
            except ContextBudgetExceeded as e:
                self.board.escalate(ticket_id, str(e), kind="ticket_too_large")
                return
            tracker = TddTracker(ws, {str(f["id"]) for f in findings if f.get("kind") == "bug"})
            finding_ids = [str(f["id"]) for f in findings]
            attempts = [0]

            async def on_finish(result: FixerResult) -> str | None:
                resolved = {r.finding_id: r.resolution for r in result.addressed}
                missing = [f for f in finding_ids if f not in resolved]
                if missing:
                    return f"Give a resolution for every finding; missing: {', '.join(missing)}"
                not_green = [f for f in tracker.missing_green() if resolved.get(f) == "fixed"]
                if not_green:
                    return (
                        "These bugs are marked fixed without a verified red→green cycle: "
                        f"{', '.join(not_green)}. Use tdd_submit_red, then tdd_submit_green."
                    )
                return await self._gates_on_finish(ws, "fixer", attempts)

            client, max_tokens = self._client("fixer")
            run = await run_agent(
                client,
                system=FIXER_SYSTEM + self.learner.skills_for("fixer"),
                prompt=prompt,
                toolbox=Toolbox([*read_tools(ws), *write_tools(ws), *tdd_tools(tracker)]),
                result_model=FixerResult,
                finish_description=FINISH_DESCRIPTIONS["fixer"],
                on_finish=on_finish,
                max_turns=self.cfg.agent_max_turns,
                max_tokens=max_tokens,
                retry=self.retry,
            )
            self._log_run(ticket_id, "fixer", agent, run)
            if run.outcome != "finished" or run.result is None:
                self.board.escalate(ticket_id, f"fixer {run.outcome}: {run.reason}")
                return
            ws.commit(f"{ticket.id}: address review\n\n{run.result.summary}")
            resolutions = "\n".join(
                f"- {r.finding_id}: {r.resolution}. {r.note}" for r in run.result.addressed
            )
            self.board.complete(
                ticket_id,
                agent,
                S.AWAITING_REVIEW,
                summary=f"{run.result.summary}\n\nResolutions:\n{resolutions}".strip(),
                resolutions=[r.model_dump() for r in run.result.addressed],
            )

    async def merge(self, ticket_id: str) -> None:
        agent = self._agent_name("merger")
        ticket = self.board.claim(agent, S.APPROVED, ticket_id=ticket_id, lease_s=self.cfg.lease_s)
        if ticket is None:
            return
        async with self._hold(ticket_id, agent):
            result = await self.merge_queue.merge(ticket)
            if result.ok:
                self.board.complete(ticket_id, agent, S.DONE, commit=result.commit)
                self.project.worktrees.remove(ticket_id, delete_branch=True)
            elif result.stage == "main_checkout":
                self.board.escalate(ticket_id, result.detail, kind="main_checkout")
            else:
                self.board.complete(
                    ticket_id,
                    agent,
                    S.CHANGES_REQUESTED,
                    source="merge",
                    stage=result.stage,
                    detail=result.detail,
                )

    async def research(self, ticket_id: str) -> None:
        """Prepare the Board decision for an escalated ticket (once per escalation)."""
        ticket = self.board.get(ticket_id)
        if ticket.status is not S.ESCALATED:
            return
        escalated = self.board.last_event(ticket_id, "ticket.escalated")
        pending = [d for d in self.board.decisions(open_only=True) if d["ticket_id"] == ticket_id]
        if pending or escalated is None:
            return
        reason = str(escalated.payload.get("reason", ""))
        evidence = escalation_context(self.board, ticket)
        wt_path = self.project.worktrees.path_for(ticket_id)
        root = wt_path if wt_path.exists() else self.project.root
        tools = [
            t
            for t in phase_file_tools(
                LocalExecutor(root), can_write=lambda p: False, writable_hint="nothing"
            )
            if t.name in ("read_file", "list_files")
        ]

        async def has_actions(req: DecisionRequest) -> str | None:
            if any(o.action is None for o in req.options):
                return "every option needs an action (retry, send_to_fixer, cancel, manual)"
            return None

        request: DecisionRequest
        try:
            client, max_tokens = self._client("researcher")
            run = await run_agent(
                client,
                system=RESEARCHER_SYSTEM + self.learner.skills_for("researcher"),
                prompt=evidence,
                toolbox=Toolbox(tools),
                result_model=DecisionRequest,
                finish_description="Submit the decision for the owner.",
                on_finish=has_actions,
                max_turns=self.cfg.agent_max_turns,
                max_tokens=max_tokens,
                retry=self.retry,
            )
            self._log_run(ticket_id, "researcher", self._agent_name("researcher"), run)
            request = run.result if run.result is not None else fallback_request(ticket, reason)
        except Exception as e:  # never leave an escalation without a question
            self.board.log_event(ticket_id, "agent.crashed", role="researcher", error=repr(e))
            request = fallback_request(ticket, reason)
        if self.board.get(ticket_id).status is S.ESCALATED:
            self.board.create_decision(request.model_dump(), ticket_id=ticket_id)

    async def learn(self, _: str) -> None:
        """Run the learner until it has caught up with the event log."""
        while await self.learner.sync():
            pass

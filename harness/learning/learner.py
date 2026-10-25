"""Learning: turning the owner's feedback and recurring problems into lessons.

Lessons are short rules with a scope. Relevant ones are injected into each agent's
context pack (ranked and capped, so context stays small); ones that keep proving
themselves are promoted into the role's standing instructions ("skills").

Sources, read from the board's event log:
- the owner's rejection feedback and decision notes (high priority, applied at once),
- review findings (become lessons when they recur),
- gate failures (the same lint rule failing on several tickets: a mechanical lesson).

A Librarian agent turns raw observations into general rules and curates the set
(merging duplicates, retiring stale rules). If it can't run, owner feedback is still
stored verbatim, so it is never lost. A lesson that contradicts the approved spec is
not applied; it becomes a Board decision instead.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from harness.agents.loop import AgentRunner, RetryPolicy
from harness.agents.runners import runner_for_role
from harness.board import Event, Ticket
from harness.context.builder import SPEC_PATH, estimate_tokens
from harness.core.runlog import log_agent_run
from harness.escalation.decisions import DecisionRequest, Option
from harness.models import ModelClient
from harness.project import Project
from harness.stacks.profile import match_glob
from harness.tools.base import Toolbox

CURSOR_KEY = "learning.cursor"
LESSON_DECISION_PREFIX = "lesson:"
APPLY, KEEP_SPEC = "Apply the rule", "Keep the spec"
PROMOTE_AFTER_HITS = 3
MAX_LESSONS_PER_AGENT = 10
MAX_LESSON_TOKENS = 1200
ROLES = (
    "implementer",
    "reviewer",
    "fixer",
    "researcher",
    "spec_writer",
    "spec_critic",
    "architect",
    "decomposer",
    "plan_critic",
)

_SCOPE = re.compile(r"^(global|role:[a-z_]+|path:\S+|stack:[A-Za-z0-9_.-]+)$")


def validate_scope(scope: str) -> str:
    scope = scope.strip()
    if not _SCOPE.match(scope):
        raise ValueError(
            f"invalid scope {scope!r}: use global, role:<role>, path:<glob> or stack:<name>"
        )
    if scope.startswith("role:") and scope[5:] not in ROLES:
        raise ValueError(f"unknown role in scope {scope!r}; roles: {', '.join(ROLES)}")
    return scope


@dataclass(frozen=True)
class Observation:
    kind: Literal["owner_feedback", "review_finding"]
    text: str
    ticket_id: str | None = None


# -- librarian I/O --------------------------------------------------------------------------


class NewLesson(BaseModel):
    rule: str = Field(description="One short, general, imperative rule")
    scope: str = Field(description="global | role:<role> | path:<glob> | stack:<name>")
    priority: Literal["high", "normal"] = "normal"
    observations: list[int] = Field(default_factory=list, description="Observation numbers")
    conflicts_with_spec: str | None = Field(
        default=None, description="If following this rule would contradict the spec, say how"
    )

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        return validate_scope(v)


class MergeOp(BaseModel):
    ids: list[str] = Field(min_length=2)
    rule: str
    scope: str

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        return validate_scope(v)


class RetireOp(BaseModel):
    id: str
    reason: str


class LibrarianResult(BaseModel):
    add: list[NewLesson] = Field(default_factory=list)
    reinforce: list[str] = Field(
        default_factory=list, description="Existing lesson ids an observation confirms again"
    )
    merge: list[MergeOp] = Field(default_factory=list)
    retire: list[RetireOp] = Field(default_factory=list)
    summary: str = ""


LIBRARIAN_SYSTEM = """\
You are the LIBRARIAN of an automated software team. You maintain the team's lessons:
short rules that are injected into the agents' context so mistakes are not repeated.

You get the current lessons and new observations (the product owner's feedback and
reviewers' findings). Decide:
- add: new lessons. Write each as one short, general, imperative rule an agent can follow
  ("Validate all user input at the API boundary and return 400 with a reason"), not a
  description of one incident. Choose the narrowest scope that fits: global,
  role:<role> (implementer, reviewer, fixer, spec_writer, architect, decomposer, ...),
  path:<glob> (e.g. path:src/api/**), or stack:<name>.
- The owner's feedback is authoritative: turn it into a high-priority lesson unless it is
  purely about one specific artifact with nothing general to learn.
- A reviewer finding becomes a lesson only if it shows a pattern: it matches an existing
  lesson (then `reinforce` that lesson instead of adding a new one) or several findings
  show the same mistake.
- merge: duplicates or near-duplicates into one better rule. retire: rules that are wrong,
  obsolete or superseded.
- If a rule would contradict the approved spec, set conflicts_with_spec; the owner decides.
Ignore anything that is not a reusable lesson. It is fine to change nothing.
"""


# -- the learner ------------------------------------------------------------------------------


class Learner:
    def __init__(
        self,
        project: Project,
        *,
        client_factory: Callable[[str], ModelClient] | None = None,
        retry: RetryPolicy | None = None,
        runner_factory: Callable[[str], AgentRunner] | None = None,
    ) -> None:
        self.project = project
        self.board = project.board
        self.runner_factory = runner_factory or (
            lambda role: runner_for_role(
                project.config, role, client_factory=client_factory, retry=retry
            )
        )
        self._lock = asyncio.Lock()

    # -- reading the event log ----------------------------------------------------------------

    def _collect(self) -> tuple[list[Observation], int, bool]:
        """New observations since the cursor, the new cursor, and whether gates failed."""
        cursor = int(self.board.get_meta(CURSOR_KEY, "0") or 0)
        observations: list[Observation] = []
        gates_failed = False
        last = cursor
        while True:
            events = self.board.events(after_seq=last, limit=2000)
            if not events:
                break
            for e in events:
                p = e.payload
                if e.type == "phase.rejected" and str(p.get("feedback", "")).strip():
                    observations.append(
                        Observation(
                            "owner_feedback",
                            f"The owner rejected the {p.get('gate')}: {p['feedback']}",
                        )
                    )
                elif e.type == "decision.answered":
                    if str(p.get("phase") or "").startswith(LESSON_DECISION_PREFIX):
                        continue
                    answer = p.get("answer") or {}
                    note = str(answer.get("note") or "").strip()
                    if note:
                        question = self.board.get_decision(str(p["decision"]))["request"][
                            "question"
                        ]
                        choice = answer.get("choice") or "(own answer)"
                        observations.append(
                            Observation(
                                "owner_feedback",
                                f"Owner's decision on '{question}': {choice}. Note: {note}",
                                e.ticket_id,
                            )
                        )
                elif e.type == "ticket.changes_requested" and p.get("source") == "review":
                    for f in p.get("findings") or []:
                        observations.append(
                            Observation(
                                "review_finding",
                                f"[{f.get('kind')}/{f.get('severity')}] {f.get('file')}: "
                                f"{f.get('detail')}",
                                e.ticket_id,
                            )
                        )
                elif e.type == "gates.failed":
                    gates_failed = True
                last = e.seq
        return observations, last, gates_failed

    async def sync(self) -> int:
        """Learn from new events. Returns how many observations were processed."""
        async with self._lock:
            observations, cursor, gates_failed = self._collect()
            if gates_failed:
                self._learn_from_lint()
            if observations:
                await self._librarian(observations)
            self.board.set_meta(CURSOR_KEY, str(cursor))
            self._auto_promote()
            return len(observations)

    # -- mechanical lessons from repeated lint failures -------------------------------------------

    def _learn_from_lint(self) -> None:
        profiles = {s.name: s for s in self.project.stacks()}
        tickets_by_code: dict[tuple[str, str], set[str]] = defaultdict(set)
        example: dict[tuple[str, str], str] = {}
        for e in self._all_events("gates.failed"):
            for r in e.payload.get("results", []):
                name = str(r.get("name", ""))
                if r.get("ok") or not name.endswith(":lint"):
                    continue
                stack = profiles.get(name.split(":")[0])
                if stack is None or not stack.lint_code_pattern:
                    continue
                detail = str(r.get("detail", ""))
                for line in detail.splitlines():
                    for code in re.findall(stack.lint_code_pattern, line):
                        key = (stack.name, code)
                        tickets_by_code[key].add(e.ticket_id or "-")
                        example.setdefault(key, line.strip()[:200])
        existing = {(lesson["scope"], lesson["source"]) for lesson in self.board.lessons()}
        for (stack_name, code), tickets in sorted(tickets_by_code.items()):
            if len(tickets) < 2:
                continue
            source = f"lint:{code}"
            scope = f"stack:{stack_name}"
            if (scope, source) in existing:
                continue
            self.board.add_lesson(
                f"The linter rule {code} keeps failing on this project; write code that "
                f"satisfies it from the start. Example: {example[(stack_name, code)]}",
                scope=scope,
                source=source,
                examples=sorted(tickets),
            )

    def _all_events(self, type: str) -> list[Event]:
        out: list[Event] = []
        after = 0
        while True:
            events = self.board.events(after_seq=after, limit=5000)
            if not events:
                return out
            out += [e for e in events if e.type == type]
            after = events[-1].seq

    # -- the librarian ----------------------------------------------------------------------

    async def _librarian(self, observations: list[Observation]) -> None:
        lessons = self.board.lessons(statuses=("active", "promoted", "pending"))
        spec_path = self.project.root / SPEC_PATH
        spec = spec_path.read_text()[:20_000] if spec_path.is_file() else "(no spec yet)"
        prompt = "\n\n".join(
            [
                "# Current lessons\n"
                + (
                    "\n".join(
                        f"- {x['id']} [{x['scope']}, {x['priority']}, hits {x['hits']}] {x['rule']}"
                        for x in lessons
                    )
                    or "(none)"
                ),
                "# New observations\n"
                + "\n".join(
                    f"{i}. ({o.kind}{', ' + o.ticket_id if o.ticket_id else ''}) {o.text}"
                    for i, o in enumerate(observations, 1)
                ),
                f"# Approved spec\n{spec}",
            ]
        )
        known = {x["id"] for x in lessons}

        async def check(res: LibrarianResult) -> str | None:
            ids = set(res.reinforce) | {r.id for r in res.retire}
            ids |= {i for m in res.merge for i in m.ids}
            unknown = sorted(ids - known)
            return f"unknown lesson ids: {unknown}" if unknown else None

        result: LibrarianResult | None = None
        try:
            run = await self.runner_factory("librarian").run(
                system=LIBRARIAN_SYSTEM,
                prompt=prompt,
                toolbox=Toolbox(),
                result_model=LibrarianResult,
                finish_description="Submit your changes to the lessons (possibly none).",
                on_finish=check,
                max_turns=6,
                cwd=self.project.root,
            )
            log_agent_run(
                self.project,
                "learning",
                "librarian",
                "librarian",
                run,
                model=self.project.config.model_for("librarian").model,
            )
            result = run.result
        except Exception as e:
            self.board.log_event(None, "agent.crashed", role="librarian", error=repr(e))
        if result is None:
            # Never lose the owner's feedback: keep it verbatim.
            for o in observations:
                if o.kind == "owner_feedback":
                    self.board.add_lesson(
                        o.text,
                        scope="global",
                        source="owner",
                        priority="high",
                        examples=[o.ticket_id] if o.ticket_id else [],
                    )
            return
        self._apply(result, observations)

    def _apply(self, res: LibrarianResult, observations: list[Observation]) -> None:
        for new in res.add:
            obs = [observations[i - 1] for i in new.observations if 1 <= i <= len(observations)]
            from_owner = any(o.kind == "owner_feedback" for o in obs)
            examples = sorted({o.ticket_id for o in obs if o.ticket_id})
            priority = "high" if from_owner else new.priority
            source = "owner" if from_owner else "review" if obs else "librarian"
            if new.conflicts_with_spec:
                lesson_id = self.board.add_lesson(
                    new.rule,
                    scope=new.scope,
                    source=source,
                    status="pending",
                    priority=priority,
                    examples=examples,
                )
                self._ask_about_conflict(lesson_id, new.rule, new.conflicts_with_spec)
            else:
                self.board.add_lesson(
                    new.rule,
                    scope=new.scope,
                    source=source,
                    priority=priority,
                    examples=examples,
                )
        self.board.bump_lessons(res.reinforce, "hits")
        for m in res.merge:
            merged = [self.board.get_lesson(i) for i in m.ids]
            self.board.add_lesson(
                m.rule,
                scope=m.scope,
                source="librarian",
                priority="high" if any(x["priority"] == "high" for x in merged) else "normal",
                examples=sorted({e for x in merged for e in x["examples"]}),
            )
            for x in merged:
                self.board.update_lesson(x["id"], status="retired", why=f"merged into: {m.rule}")
        for r in res.retire:
            self.board.update_lesson(r.id, status="retired", why=r.reason)

    def _ask_about_conflict(self, lesson_id: str, rule: str, conflict: str) -> None:
        request = DecisionRequest(
            question=f"A learned rule conflicts with the spec. Apply it? ({lesson_id})",
            context=f"Rule: {rule}\n\nConflict: {conflict}",
            options=[
                Option(
                    name=APPLY,
                    pros=["agents follow your latest feedback"],
                    cons=["the spec and the code will disagree until the spec is updated"],
                ),
                Option(
                    name=KEEP_SPEC,
                    pros=["the spec stays the single source of truth"],
                    cons=["the rule is dropped"],
                ),
            ],
            recommendation=KEEP_SPEC,
            confidence="low",
            reason="The approved spec wins unless you deliberately change direction.",
        )
        self.board.create_decision(request.model_dump(), phase=LESSON_DECISION_PREFIX + lesson_id)

    def apply_conflict_decision(self, decision_id: str) -> None:
        d = self.board.get_decision(decision_id)
        phase = str(d["phase"] or "")
        if not phase.startswith(LESSON_DECISION_PREFIX) or d["answer"] is None:
            return
        lesson_id = phase.removeprefix(LESSON_DECISION_PREFIX)
        applied = d["answer"].get("choice") == APPLY
        self.board.update_lesson(
            lesson_id,
            status="active" if applied else "retired",
            why=f"owner decision {decision_id}",
        )

    def _auto_promote(self) -> None:
        for x in self.board.lessons(statuses=("active",)):
            if x["hits"] >= PROMOTE_AFTER_HITS and (
                x["scope"] == "global" or x["scope"].startswith("role:")
            ):
                self.board.update_lesson(
                    x["id"], status="promoted", why=f"confirmed {x['hits']} times"
                )

    # -- using lessons --------------------------------------------------------------------------

    def _matches(self, scope: str, role: str, ticket: Ticket | None) -> bool:
        if scope == "global":
            return True
        kind, _, value = scope.partition(":")
        if kind == "role":
            return value == role
        if ticket is None:
            return False
        files = [*ticket.owned_files, *ticket.readable_files]
        if kind == "path":
            return any(match_glob(f, value) for f in files)
        if kind == "stack":
            stacks = {s.name: s for s in self.project.stacks()}
            stack = stacks.get(value)
            return stack is not None and any(
                stack.is_source_file(f) or stack.is_test_file(f) for f in files
            )
        return False

    def lessons_for(self, ticket: Ticket | None, role: str) -> list[str]:
        """The most relevant active lessons for an agent, within a small token budget."""
        candidates = [
            x
            for x in self.board.lessons(statuses=("active",))
            if self._matches(x["scope"], role, ticket)
        ]
        candidates.sort(key=lambda x: (x["priority"] != "high", -x["hits"], -x["updated_at"]))
        chosen, tokens = [], 0
        for x in candidates[:MAX_LESSONS_PER_AGENT]:
            cost = estimate_tokens(x["rule"])
            if tokens + cost > MAX_LESSON_TOKENS:
                break
            chosen.append(x)
            tokens += cost
        self.board.bump_lessons([x["id"] for x in chosen], "uses")
        return [x["rule"] for x in chosen]

    def skills_for(self, role: str) -> str:
        """Promoted lessons, appended to the role's system prompt."""
        rules = [
            x["rule"]
            for x in self.board.lessons(statuses=("promoted",))
            if x["scope"] == "global" or x["scope"] == f"role:{role}"
        ]
        if not rules:
            return ""
        return "\n\nProject skills (rules this team has learned; always follow them):\n" + (
            "\n".join(f"- {r}" for r in rules)
        )

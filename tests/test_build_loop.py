"""End-to-end: tickets go through implement → gates → review → TDD fix → review → merge."""

from __future__ import annotations

from pathlib import Path

from harness.agents.loop import RetryPolicy
from harness.board import TicketStatus
from harness.core.orchestrator import Orchestrator
from harness.models import ToolResultBlock
from tests.conftest import run_git
from tests.helpers import CONFIG, ScriptedAgents, call, finish, make_app, say, ticket

S = TicketStatus

CALC_V1 = "def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n"
TEST_V1 = (
    "from calc import add, div\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_div():\n    assert div(6, 3) == 2\n"
)
TEST_V2 = TEST_V1.replace(
    "from calc import add, div\n", "import pytest\n\nfrom calc import add, div\n"
) + ("\n\ndef test_div_by_zero():\n    with pytest.raises(ValueError):\n        div(1, 0)\n")

CALC_TICKET = dict(
    title="Calculator",
    spec_refs=["AC-1", "AC-2"],
    owned_files=["calc.py", "tests/test_calc.py"],
    tests_required=["tests/test_calc.py"],
    acceptance=["add returns the sum", "div by zero raises ValueError"],
)


def orchestrator(project, agents: ScriptedAgents) -> Orchestrator:  # type: ignore[no-untyped-def]
    async def no_sleep(_: float) -> None:
        return None

    return Orchestrator(
        project,
        client_factory=agents.factory,
        retry=RetryPolicy(sleep=no_sleep),
        poll_interval_s=0.01,
    )


def tool_results(agents: ScriptedAgents, role: str) -> list[ToolResultBlock]:
    out = []
    for r, _, client in agents.clients:
        if r != role:
            continue
        for c in client.calls:
            for m in c.messages:
                out += [b for b in m.content if isinstance(b, ToolResultBlock)]
    return out


async def test_full_cycle_with_tdd_bug_fix(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    project.board.add_tickets([ticket("T-1", **CALC_TICKET)])
    project.board.queue("T-1")

    agents = ScriptedAgents()
    agents.add(
        "implementer",
        "T-1",
        call("write_file", path="calc.py", content=CALC_V1),
        call("write_file", path="tests/test_calc.py", content=TEST_V1),
        call("run_tests"),
        finish(summary="add and div with tests"),
    )
    agents.add(
        "reviewer",
        "T-1",
        call("read_file", path="calc.py"),
        finish(
            verdict="changes_requested",
            summary="div by zero not handled",
            findings=[
                {
                    "id": "F-1",
                    "kind": "bug",
                    "severity": "high",
                    "file": "calc.py",
                    "line": 6,
                    "detail": "AC-2 requires ValueError",
                    "repro": "div(1, 0) raises ZeroDivisionError",
                }
            ],
        ),
    )
    agents.add(
        "fixer",
        "T-1",
        call("write_file", path="tests/test_calc.py", content=TEST_V2),
        call(
            "tdd_submit_red",
            finding_id="F-1",
            test_id="tests/test_calc.py::test_div_by_zero",
            test_files=["tests/test_calc.py"],
        ),
        finish(summary="premature", addressed=[{"finding_id": "F-1", "resolution": "fixed"}]),
        call("write_file", path="tests/test_calc.py", content="def test_nothing(): pass\n"),
        call(
            "edit_file",
            path="calc.py",
            old="    return a / b\n",
            new="    if b == 0:\n        raise ValueError('division by zero')\n    return a / b\n",
        ),
        call("tdd_submit_green", finding_id="F-1"),
        finish(
            summary="div raises ValueError",
            addressed=[{"finding_id": "F-1", "resolution": "fixed"}],
        ),
    )
    agents.add("reviewer", "T-1", finish(verdict="approve", summary="looks good"))

    summary = await orchestrator(project, agents).run(timeout_s=120)

    assert agents.unused() == {}
    t = project.board.get("T-1")
    assert t.status is S.DONE, project.board.events_for("T-1")
    assert t.review_rounds == 1
    # main (and the primary checkout) has the fix and the test.
    assert "raise ValueError" in (project.root / "calc.py").read_text()
    assert "test_div_by_zero" in (project.root / "tests/test_calc.py").read_text()
    log = run_git(project.root, "log", "--format=%s", "main")
    for subject in ("Merge T-1: Calculator", "fix(T-1): F-1", "test(T-1): reproduce F-1"):
        assert subject in log
    # The red commit really comes before the fix.
    assert log.index("fix(T-1): F-1") < log.index("test(T-1): reproduce F-1")
    types = [e.type for e in project.board.events_for("T-1")]
    assert types.index("tdd.red_verified") < types.index("tdd.green_verified")
    assert "gates.passed" in types and "merge.gates" in types
    assert summary.agent_runs == 4
    # Worktree and branch are cleaned up after merge.
    assert not (project.root / ".harness/worktrees/T-1").exists()
    assert "ticket/T-1" not in run_git(project.root, "branch")

    fixer_results = tool_results(agents, "fixer")
    errors = [r.content for r in fixer_results if r.is_error]
    assert any("without a verified red→green" in e for e in errors)
    assert any("frozen" in e for e in errors)  # the fixer couldn't weaken the test


async def test_gate_failure_is_fed_back_to_the_implementer(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    project.board.add_tickets([ticket("T-1", **CALC_TICKET)])
    project.board.queue("T-1")
    broken = CALC_V1.replace("return a + b", "return a - b")
    agents = ScriptedAgents()
    agents.add(
        "implementer",
        "T-1",
        call("write_file", path="calc.py", content=broken),
        call("write_file", path="tests/test_calc.py", content=TEST_V1),
        finish(summary="done?"),
        call("write_file", path="calc.py", content=CALC_V1),
        finish(summary="fixed add"),
    )
    agents.add("reviewer", "T-1", finish(verdict="approve", summary="ok"))

    await orchestrator(project, agents).run(timeout_s=120)

    assert project.board.get("T-1").status is S.DONE
    gate_events = [e.type for e in project.board.events_for("T-1") if e.type.startswith("gates.")]
    assert gate_events == ["gates.failed", "gates.passed"]
    feedback = [r.content for r in tool_results(agents, "implementer") if r.is_error]
    assert any("FAIL  python:test" in f and "assert -1 == 5" in f for f in feedback)


async def test_dependencies_and_parallel_shared_file_edits(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    project.board.add_tickets(
        [
            ticket("A", owned_files=["a.py"]),
            ticket("B", owned_files=["b.py"]),
            ticket("C", owned_files=["c.py"], depends_on=["A"], readable_files=["a.py"]),
        ]
    )
    for tid in "ABC":
        project.board.queue(tid)
    agents = ScriptedAgents()
    for tid, dep in (("A", "requests"), ("B", "numpy")):
        agents.add(
            "implementer",
            tid,
            call("write_file", path=f"{tid.lower()}.py", content=f"X = '{tid}'\n"),
            call("edit_shared_file", path="requirements.txt", op="append", text=f"{dep}\n"),
            finish(summary=tid),
        )
        agents.add("reviewer", tid, finish(verdict="approve", summary="ok"))
    agents.add(
        "implementer",
        "C",
        call("read_file", path="a.py"),
        call("write_file", path="c.py", content="from a import X\n\nY = X * 2\n"),
        finish(summary="C uses A"),
    )
    agents.add("reviewer", "C", finish(verdict="approve", summary="ok"))

    await orchestrator(project, agents).run(timeout_s=120)

    assert agents.unused() == {}
    assert {t.id: t.status for t in project.board.tickets()} == {
        "A": S.DONE,
        "B": S.DONE,
        "C": S.DONE,
    }
    # Both parallel tickets edited the same shared file; the merge queue replayed the edits.
    reqs = (project.root / "requirements.txt").read_text().split()
    assert sorted(reqs) == ["numpy", "requests"]
    # C only started after A was merged, so it could read A's file.
    events = project.board.events()
    a_done = next(e.seq for e in events if e.ticket_id == "A" and e.type == "ticket.done")
    c_impl = next(e.seq for e in events if e.ticket_id == "C" and e.type == "ticket.implementing")
    assert a_done < c_impl


async def test_agent_that_never_finishes_is_escalated(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    project.board.add_tickets([ticket("T-1", owned_files=["x.py"])])
    project.board.queue("T-1")
    agents = ScriptedAgents()
    agents.add("implementer", "T-1", *[say("thinking about it...") for _ in range(4)])

    await orchestrator(project, agents).run(timeout_s=60)

    t = project.board.get("T-1")
    assert t.status is S.ESCALATED
    reason = project.board.last_event("T-1", "ticket.escalated")
    assert reason is not None and "no_finish" in reason.payload["reason"]


async def test_oversized_ticket_is_escalated_before_any_model_call(tmp_path: Path) -> None:
    config = CONFIG + "context_budget_tokens: {implementer: 50}\n"
    project = make_app(tmp_path, config=config)
    project.board.add_tickets([ticket("T-1", **CALC_TICKET)])
    project.board.queue("T-1")
    agents = ScriptedAgents()

    await orchestrator(project, agents).run(timeout_s=60)

    assert project.board.get("T-1").status is S.ESCALATED
    ev = project.board.last_event("T-1", "ticket.escalated")
    assert ev is not None and ev.payload["kind"] == "ticket_too_large"
    assert agents.clients == []


async def test_custom_hooks_and_recovery_from_a_crashed_worker(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    project.board.add_tickets([ticket("T-1", owned_files=["x.py"])])
    project.board.queue("T-1")
    # A previous harness process claimed the ticket and died; its lease has expired.
    project.board.claim("dead-agent", S.READY, lease_s=-1)

    agents = ScriptedAgents()
    agents.add(
        "implementer",
        "T-1",
        call("write_file", path="x.py", content="X = 1\n"),
        finish(summary="x"),
    )
    agents.add("reviewer", "T-1", finish(verdict="approve", summary="ok"))
    orch = orchestrator(project, agents)
    done: list[str] = []

    @orch.hooks.on("ticket.done")
    async def record(event, _orch):  # type: ignore[no-untyped-def]
        done.append(event.ticket_id)

    await orch.run(timeout_s=60)

    assert done == ["T-1"]
    events = project.board.events_for("T-1")
    assert any(e.payload.get("reason") == "lease_expired" for e in events)
    assert project.board.get("T-1").status is S.DONE

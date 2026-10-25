"""End-to-end: idea → spec (interview + critic + owner feedback) → architecture → plan → build."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.agents.loop import RetryPolicy
from harness.board import TicketStatus
from harness.core.orchestrator import Orchestrator
from harness.escalation.decisions import Approval, Decision
from harness.phases.pipeline import PhaseError, Pipeline
from harness.project import Project, init_project
from tests.conftest import run_git
from tests.helpers import STACK, ScriptedAgents, ScriptedHuman, call, finish

CONFIG = """\
providers: {fake: {type: fake}}
models: {default: {provider: fake, model: scripted}}
"""

SPEC_V1 = """\
# Calculator

## Goals
- G-1: A tiny calculator library.

## User stories
- US-1: As a developer I can add and divide numbers.

## Acceptance criteria
- AC-1: add(2, 3) returns 5.
- AC-2: div(6, 3) returns 2; div(1, 0) raises ValueError.

## Open questions
- Q-1: negative numbers?
"""
OPEN_Q = "## Open questions\n- Q-1: negative numbers?\n"

ARCH = """\
# Architecture
Two modules so they can be built in parallel.

## Interface contracts
### calc.add
`add(a: float, b: float) -> float` in arith.py
### calc.sub
`sub(a: float, b: float) -> float` in arith.py
### calc.div
`div(a: float, b: float) -> float` in division.py; raises ValueError when b == 0
"""

ASK = {
    "question": "What should division by zero do?",
    "options": [
        {"name": "Raise ValueError", "pros": ["explicit"], "cons": ["callers must catch"]},
        {"name": "Return None", "pros": ["no exceptions"], "cons": ["silent bugs"]},
    ],
    "recommendation": "Raise ValueError",
}


def make_empty_app(tmp_path: Path) -> Project:
    app = tmp_path / "app"
    init_project(app, user_config=tmp_path / "none.yaml")
    (app / ".harness/config.yaml").write_text(CONFIG)
    (app / ".harness/stacks").mkdir()
    (app / ".harness/stacks/python.yaml").write_text(STACK)
    (app / ".gitignore").write_text("__pycache__/\n")
    run_git(app, "add", "-A")
    run_git(app, "commit", "-q", "-m", "empty app")
    return Project.open(app, user_config=tmp_path / "none.yaml")


def ticket(id: str, module: str, refs: list[str], contracts: list[str]) -> dict[str, object]:
    return {
        "id": id,
        "title": f"{module} module",
        "spec_refs": refs,
        "contracts": contracts,
        "owned_files": [f"{module}.py", f"tests/test_{module}.py"],
        "tests_required": [f"tests/test_{module}.py"],
        "acceptance": [f"{module} works"],
    }


async def no_sleep(_: float) -> None:
    return None


async def test_idea_to_merged_code(tmp_path: Path) -> None:
    project = make_empty_app(tmp_path)
    agents = ScriptedAgents()
    human = ScriptedHuman(
        decisions=[Decision(choice="Raise ValueError")],
        approvals=[
            Approval(approved=False, feedback="Also support subtraction."),  # spec, 1st
            Approval(approved=True),  # spec
            Approval(approved=True),  # architecture
            Approval(approved=True),  # plan
        ],
    )
    # -- spec: interview, lint rejection, critic round, owner feedback round
    agents.add(
        "spec_writer",
        "-",
        call("ask_user", **ASK),
        call("write_file", path="docs/spec.md", content=SPEC_V1),
        finish(summary="first draft"),  # rejected: open question Q-1
        call("edit_file", path="docs/spec.md", old=OPEN_Q, new=""),
        finish(summary="first draft, no open questions"),
    )
    agents.add(
        "spec_critic",
        "-",
        finish(
            verdict="changes_requested",
            summary="edge cases",
            issues=[
                {
                    "kind": "missing_edge_case",
                    "severity": "high",
                    "where": "AC-1",
                    "detail": "negative numbers are not specified",
                }
            ],
        ),
    )
    agents.add(
        "spec_writer",
        "-",
        call(
            "edit_file",
            path="docs/spec.md",
            old="- AC-2:",
            new="- AC-3: add(-2, -3) returns -5.\n- AC-2:",
        ),
        finish(summary="added negatives"),
    )
    agents.add("spec_critic", "-", finish(verdict="approve", summary="clear"))
    agents.add(
        "spec_writer",
        "-",
        call(
            "edit_file",
            path="docs/spec.md",
            old="- AC-2:",
            new="- AC-4: sub(5, 3) returns 2.\n- AC-2:",
        ),
        finish(summary="added subtraction"),
    )
    agents.add("spec_critic", "-", finish(verdict="approve", summary="clear"))
    # -- architecture
    agents.add(
        "architect",
        "-",
        call("write_file", path="docs/architecture.md", content=ARCH),
        call("write_file", path="docs/conventions.md", content="Use type hints.\n"),
        call("write_file", path="tests/test_smoke.py", content="def test_smoke():\n    pass\n"),
        call("write_file", path="docs/spec.md", content="hijack"),  # refused
        call("check_stacks", stacks=["python"]),
        finish(summary="two modules", stacks=["cobol"]),  # rejected
        finish(summary="two modules", stacks=["python"]),
    )
    # -- plan: first plan misses AC-4 and is rejected by the linter
    arith_partial = ticket("T-1", "arith", ["AC-1", "AC-3"], ["calc.add", "calc.sub"])
    arith = ticket("T-1", "arith", ["AC-1", "AC-3", "AC-4"], ["calc.add", "calc.sub"])
    division = ticket("T-2", "division", ["AC-2"], ["calc.div"])
    agents.add(
        "decomposer",
        "-",
        call("read_file", path="docs/architecture.md"),
        finish(tickets=[arith_partial, division], summary="two tickets"),
        finish(tickets=[arith, division], summary="two parallel tickets"),
    )
    agents.add("plan_critic", "-", finish(verdict="approve", summary="good split"))
    # -- build
    code = {
        "arith": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        "division": (
            "def div(a, b):\n    if b == 0:\n        raise ValueError('division by zero')\n"
            "    return a / b\n"
        ),
    }
    tests = {
        "arith": "from arith import add, sub\n\n\ndef test_it():\n    assert add(-2, -3) == -5\n"
        "    assert sub(5, 3) == 2\n",
        "division": "import pytest\n\nfrom division import div\n\n\ndef test_it():\n"
        "    assert div(6, 3) == 2\n    with pytest.raises(ValueError):\n        div(1, 0)\n",
    }
    for tid, module in (("T-1", "arith"), ("T-2", "division")):
        agents.add(
            "implementer",
            tid,
            call("write_file", path=f"{module}.py", content=code[module]),
            call("write_file", path=f"tests/test_{module}.py", content=tests[module]),
            finish(summary=module),
        )
        agents.add("reviewer", tid, finish(verdict="approve", summary="ok"))

    retry = RetryPolicy(sleep=no_sleep)
    pipeline = Pipeline(project, human, client_factory=agents.factory, retry=retry)
    assert (await pipeline.spec("A small calculator library")).ok
    with pytest.raises(PhaseError):
        await pipeline.plan()  # phases run in order
    assert (await pipeline.architecture()).ok
    assert (await pipeline.plan()).ok
    assert pipeline.phase == "build"
    await Orchestrator(
        project, client_factory=agents.factory, retry=retry, poll_interval_s=0.01
    ).run(timeout_s=120)

    assert agents.unused() == {}
    root = project.root
    spec = (root / "docs/spec.md").read_text()
    assert "AC-3" in spec and "AC-4" in spec and "Q-1" not in spec
    assert "### calc.div" in (root / "docs/architecture.md").read_text()
    assert "stacks: [python]" in (root / ".harness/config.yaml").read_text()
    assert project.config.stacks == ["python"]
    assert {t.id: t.status for t in project.board.tickets()} == {
        "T-1": TicketStatus.DONE,
        "T-2": TicketStatus.DONE,
    }
    assert "raise ValueError" in (root / "division.py").read_text()

    # The owner got a ready-to-decide question, and the answer was recorded.
    (decision,) = project.board.decisions()
    assert decision["answer"]["choice"] == "Raise ValueError"
    assert len(human.requests[0][1].options) == 2
    assert [g for g, _ in human.gates] == ["spec", "spec", "architecture", "plan"]
    assert "T-1: arith module" in human.gates[-1][1]

    # Mechanical checks pushed back on the agents before anything reached the owner.
    errors = [
        b.content
        for _, _, c in agents.clients
        for call_ in c.calls
        for m in call_.messages
        for b in m.content
        if getattr(b, "is_error", False)
    ]
    assert any("open question Q-1" in e for e in errors)
    assert any("you may not write docs/spec.md" in e for e in errors)
    assert any("unknown stacks ['cobol']" in e for e in errors)
    assert any("AC-4 is not covered" in e for e in errors)
    log = run_git(root, "log", "--format=%s", "main")
    assert "Merge spec phase" in log and "Merge architecture phase" in log

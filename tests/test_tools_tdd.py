"""Scope enforcement in the tools, and the red→green guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.gates.tdd import TddTracker
from harness.models import ToolUseBlock
from harness.tools.base import Toolbox, ToolError
from harness.tools.files import read_tools, write_tools
from harness.tools.workspace import TicketWorkspace
from tests.conftest import run_git
from tests.helpers import make_app, ticket

BUGGY = "def div(a, b):\n    return a / b\n"
FIXED = "def div(a, b):\n    if b == 0:\n        raise ValueError('zero')\n    return a / b\n"
RED_TEST = (
    "import pytest\n\nfrom calc import div\n\n\n"
    "def test_zero():\n    with pytest.raises(ValueError):\n        div(1, 0)\n"
)
TEST_ID = "tests/test_calc.py::test_zero"


@pytest.fixture
def ws(tmp_path: Path) -> TicketWorkspace:
    project = make_app(tmp_path, extra_files={"calc.py": BUGGY, "notes.md": "secret"})
    t = ticket(
        "T-1",
        owned_files=["calc.py", "tests/test_calc.py"],
        readable_files=["docs/spec.md"],
    )
    project.board.add_tickets([t])
    wt = project.worktrees.create("T-1")
    return TicketWorkspace(project.board.get("T-1"), wt, project.board, project.stacks())


async def use(box: Toolbox, name: str, **input: object) -> tuple[bool, str]:
    res = await box.call(ToolUseBlock(id="x", name=name, input=dict(input)))
    return not res.is_error, res.content


async def test_scope_is_enforced(ws: TicketWorkspace) -> None:
    box = Toolbox([*read_tools(ws), *write_tools(ws)])
    assert await use(box, "read_file", path="docs/spec.md") == (
        True,
        (ws.root / "docs/spec.md").read_text(),
    )
    ok, msg = await use(box, "read_file", path="notes.md")
    assert not ok and "outside this ticket's scope" in msg
    ok, msg = await use(box, "write_file", path="other.py", content="x")
    assert not ok and "not owned by T-1" in msg
    ok, msg = await use(box, "write_file", path="../escape.py", content="x")
    assert not ok
    ok, msg = await use(box, "write_file", path="requirements.txt", content="x")
    assert not ok and "edit_shared_file" in msg
    ok, msg = await use(box, "write_file", path="calc.py")
    assert not ok and "invalid input" in msg  # missing `content` never reaches the handler


async def test_edit_file_and_shared_edits(ws: TicketWorkspace) -> None:
    box = Toolbox([*read_tools(ws), *write_tools(ws)])
    ok, msg = await use(box, "edit_file", path="calc.py", old="nope", new="x")
    assert not ok and "occurs 0 times" in msg
    assert (await use(box, "edit_file", path="calc.py", old="a / b", new="a // b"))[0]
    assert "a // b" in (ws.root / "calc.py").read_text()

    assert (
        await use(box, "edit_shared_file", path="requirements.txt", op="append", text="attrs\n")
    )[0]
    assert ws.board.shared_edits("T-1") == [
        ("requirements.txt", {"op": "append", "text": "attrs\n", "old": None})
    ]
    ok, msg = await use(box, "edit_shared_file", path="calc.py", op="append", text="x")
    assert not ok and "not a shared file" in msg
    # Shared edits are in scope for commits; other stray files are not.
    (ws.root / "stray.txt").write_text("x")
    assert ws.scope_violations() == ["stray.txt"]
    with pytest.raises(ToolError, match="out-of-scope"):
        ws.commit("bad")


async def test_red_rejects_test_that_passes(ws: TicketWorkspace) -> None:
    tdd = TddTracker(ws, {"F-1"})
    ws.executor.write_text("tests/test_calc.py", "def test_zero():\n    assert True\n")
    with pytest.raises(ToolError, match="PASSES on the current code"):
        await tdd.submit_red("F-1", TEST_ID, ["tests/test_calc.py"])


async def test_red_rejects_source_changes_and_unknown_findings(ws: TicketWorkspace) -> None:
    tdd = TddTracker(ws, {"F-1"})
    ws.executor.write_text("tests/test_calc.py", RED_TEST)
    ws.executor.write_text("calc.py", FIXED)
    with pytest.raises(ToolError, match="BEFORE changing code"):
        await tdd.submit_red("F-1", TEST_ID, ["tests/test_calc.py"])
    with pytest.raises(ToolError, match="not a bug finding"):
        await tdd.submit_red("F-9", TEST_ID, ["tests/test_calc.py"])


async def test_red_rejects_errors_that_are_not_test_failures(ws: TicketWorkspace) -> None:
    tdd = TddTracker(ws, {"F-1"})
    ws.executor.write_text("tests/test_calc.py", RED_TEST)
    with pytest.raises(ToolError, match="errored instead of reporting a failing test"):
        await tdd.submit_red("F-1", "tests/test_calc.py::no_such_test", ["tests/test_calc.py"])


async def test_red_then_green(ws: TicketWorkspace) -> None:
    tdd = TddTracker(ws, {"F-1"})
    ws.executor.write_text("tests/test_calc.py", RED_TEST)
    msg = await tdd.submit_red("F-1", TEST_ID, ["tests/test_calc.py"])
    assert "RED verified" in msg
    assert tdd.missing_green() == ["F-1"]
    with pytest.raises(ToolError, match="frozen"):
        ws.check_write("tests/test_calc.py")
    with pytest.raises(ToolError, match="no code changes"):
        await tdd.submit_green("F-1")
    ws.executor.write_text("calc.py", BUGGY.replace("a / b", "a / (b or 1)"))
    with pytest.raises(ToolError, match="still fails"):
        await tdd.submit_green("F-1")
    ws.executor.write_text("calc.py", FIXED)
    assert "GREEN verified" in await tdd.submit_green("F-1")
    assert tdd.missing_green() == []
    log = run_git(ws.root, "log", "--format=%s", "-2")
    assert log.splitlines() == ["fix(T-1): F-1", "test(T-1): reproduce F-1"]

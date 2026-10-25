from __future__ import annotations

from pathlib import Path

from harness.core.merge import MergeQueue, merge_ref
from harness.executors.worktree import git
from tests.conftest import run_git
from tests.helpers import make_app, ticket


def commit_in(wt: Path, rel: str, content: str, msg: str = "work") -> None:
    (wt / rel).parent.mkdir(parents=True, exist_ok=True)
    (wt / rel).write_text(content)
    run_git(wt, "add", "-A")
    run_git(wt, "commit", "-q", "-m", msg)


async def test_merge_queue_replays_shared_edits_and_fast_forwards_main(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    board = project.board
    board.add_tickets([ticket("A", owned_files=["a.py"]), ticket("B", owned_files=["b.py"])])
    queue = MergeQueue(project.root, board, project.stacks(), project.worktrees)
    for tid, dep in (("A", "attrs"), ("B", "numpy")):
        wt = project.worktrees.create(tid).path
        (wt / "requirements.txt").write_text(f"{dep}\n")
        board.record_shared_edit(tid, "requirements.txt", {"op": "append", "text": f"{dep}\n"})
        commit_in(wt, f"{tid.lower()}.py", "X = 1\n")

    res_a = await queue.merge(board.get("A"))
    assert res_a.ok
    res_b = await queue.merge(board.get("B"))
    assert res_b.ok, res_b.detail
    assert (project.root / "requirements.txt").read_text() == "attrs\nnumpy\n"
    assert (project.root / "b.py").exists()  # primary checkout fast-forwarded
    assert git(project.root, "rev-parse", "main").strip() == res_b.commit


async def test_merge_queue_keeps_main_when_gates_fail(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    project.board.add_tickets([ticket("A", owned_files=["tests/test_a.py"])])
    before = git(project.root, "rev-parse", "main").strip()
    wt = project.worktrees.create("A").path
    commit_in(wt, "tests/test_a.py", "def test_a():\n    assert False\n")
    queue = MergeQueue(project.root, project.board, project.stacks(), project.worktrees)

    res = await queue.merge(project.board.get("A"))

    assert not res.ok and res.stage == "gates" and "FAIL  python:test" in res.detail
    assert git(project.root, "rev-parse", "main").strip() == before


def test_unreplayable_conflict_aborts_cleanly(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    wt = project.worktrees.create("A").path
    commit_in(wt, "docs/spec.md", "ticket version\n")
    commit_in(project.root, "docs/spec.md", "main version\n", "main moves")
    head = git(wt, "rev-parse", "HEAD").strip()

    res = merge_ref(
        wt, "main", message="sync", main_side="theirs", ops={}, is_shared=lambda p: False
    )

    assert not res.ok and "conflicts outside replayable shared edits" in res.detail
    assert git(wt, "rev-parse", "HEAD").strip() == head
    assert git(wt, "status", "--porcelain").strip() == ""

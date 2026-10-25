from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.executors import LocalExecutor, PathEscapeError, WorktreeManager
from tests.conftest import run_git


async def test_run_command(tmp_path: Path) -> None:
    ex = LocalExecutor(tmp_path)
    res = await ex.run("echo hi && echo err >&2 && exit 3")
    assert res.exit_code == 3 and not res.ok
    assert res.stdout == "hi\n" and res.stderr == "err\n"


async def test_run_in_subdir_with_env(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    res = await LocalExecutor(tmp_path).run("pwd; echo $FOO", cwd="sub", env={"FOO": "bar"})
    assert res.ok
    assert res.stdout.splitlines() == [str((tmp_path / "sub").resolve()), "bar"]


async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    res = await LocalExecutor(tmp_path).run("sleep 30 & sleep 30", timeout_s=0.5)
    assert res.timed_out and not res.ok
    assert res.duration_s < 10


def test_file_ops_confined_to_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    ex = LocalExecutor(root)
    ex.write_text("a/b.txt", "x")
    assert ex.read_text("a/b.txt") == "x"
    assert ex.exists("a/b.txt")
    assert ex.list_files() == ["a/b.txt"]
    with pytest.raises(PathEscapeError):
        ex.read_text("../outside.txt")
    with pytest.raises(PathEscapeError):
        ex.write_text("/etc/passwd", "no")
    (tmp_path / "secret").write_text("s")
    os.symlink(tmp_path / "secret", root / "link")
    with pytest.raises(PathEscapeError):
        ex.read_text("link")


async def test_cwd_cannot_escape(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        await LocalExecutor(tmp_path).run("true", cwd="..")


def test_worktree_lifecycle(git_repo: Path) -> None:
    mgr = WorktreeManager(git_repo)
    wt = mgr.create("T-1")
    assert wt.branch == "ticket/T-1"
    assert (wt.path / "README.md").read_text() == "hello\n"
    assert mgr.create("T-1") == wt  # idempotent

    wt.executor().write_text("new.txt", "content")
    run_git(wt.path, "add", ".")
    run_git(wt.path, "commit", "-q", "-m", "work")
    assert not (git_repo / "new.txt").exists()  # isolated from main checkout

    assert [w.ticket_id for w in mgr.list()] == ["T-1"]
    mgr.remove("T-1")
    assert not wt.path.exists()
    assert mgr.list() == []
    # The branch survives removal, so the work can be recreated...
    assert (mgr.create("T-1").path / "new.txt").exists()
    # ...until it is deleted explicitly.
    mgr.remove("T-1", delete_branch=True)
    assert not (mgr.create("T-1").path / "new.txt").exists()


def test_worktree_rejects_bad_ids(git_repo: Path) -> None:
    with pytest.raises(ValueError):
        WorktreeManager(git_repo).create("../evil")

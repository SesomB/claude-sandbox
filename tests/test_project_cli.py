from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.cli import main
from harness.project import Project, ProjectError, init_project
from tests.conftest import run_git


@pytest.fixture(autouse=True)
def isolate_user_config(monkeypatch: pytest.MonkeyPatch, no_user_config: Path) -> None:
    monkeypatch.setattr("harness.config.USER_CONFIG_PATH", no_user_config)


def test_init_new_directory(tmp_path: Path) -> None:
    app = tmp_path / "my-app"
    project = init_project(app)
    assert (app / ".git").is_dir()
    assert (app / ".harness" / "config.yaml").is_file()
    assert run_git(app, "log", "--oneline").count("\n") == 1
    assert run_git(app, "branch", "--show-current").strip() == "main"
    assert project.config.model_for("implementer").model == "sonnet"
    assert project.config.provider_for("implementer").type == "claude_code"
    # Board state is not committed.
    assert "board.db" not in run_git(app, "status", "--porcelain")
    # Worktrees work right away because there is a base commit.
    assert project.worktrees.create("T-1").path.is_dir()


def test_init_is_idempotent_on_existing_repo(git_repo: Path) -> None:
    init_project(git_repo)
    init_project(git_repo)
    log = run_git(git_repo, "log", "--oneline").splitlines()
    assert len(log) == 2 and "Initialize harness project" in log[0]


def test_open_from_subdirectory(git_repo: Path) -> None:
    init_project(git_repo)
    (git_repo / "src" / "deep").mkdir(parents=True)
    assert Project.open(git_repo / "src" / "deep").root == git_repo.resolve()
    with pytest.raises(ProjectError):
        Project.open(git_repo.parent)


def test_cli_ticket_flow(git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", str(git_repo)]) == 0
    tickets = git_repo / "tickets.yaml"
    tickets.write_text(
        """
- id: T-1
  title: Token model
  owned_files: [app/tokens.py]
  acceptance: ["tokens expire after 30 minutes"]
- id: T-2
  title: Reset endpoint
  depends_on: [T-1]
  owned_files: [app/reset.py]
"""
    )
    cwd = os.getcwd()
    os.chdir(git_repo)
    try:
        assert main(["ticket", "add", str(tickets), "--queue"]) == 0
        capsys.readouterr()
        assert main(["board"]) == 0
        out = capsys.readouterr().out
        assert "T-1        ready" in out and "T-2        waiting" in out
        assert main(["config"]) == 0
        assert "implementer" in capsys.readouterr().out
        assert main(["events"]) == 0
        assert "ticket.ready" in capsys.readouterr().out
        # Conflicting ownership is reported, not a traceback.
        tickets.write_text("- {id: T-3, title: x, owned_files: [app/tokens.py]}\n")
        assert main(["ticket", "add", str(tickets)]) == 1
        assert "already owned by T-1" in capsys.readouterr().err
    finally:
        os.chdir(cwd)


def test_cli_phase_status_and_decisions(git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", str(git_repo)]) == 0
    assert main(["-C", str(git_repo), "status"]) == 0
    assert "Current phase: spec" in capsys.readouterr().out
    project = Project.open(git_repo)
    project.board.create_decision({"question": "Which DB?"}, phase="architecture")
    assert main(["-C", str(git_repo), "decisions"]) == 0
    assert "D-1    [architecture] Which DB?  ->  open" in capsys.readouterr().out
    assert main(["-C", str(git_repo), "spec"]) == 1  # no idea given

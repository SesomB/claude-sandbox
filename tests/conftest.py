from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@localhost"]


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def no_user_config(tmp_path: Path) -> Path:
    """Path to a user config that doesn't exist, so tests ignore ~/.config."""
    return tmp_path / "no-user-config.yaml"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()

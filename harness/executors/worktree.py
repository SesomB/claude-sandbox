"""Per-ticket git worktrees: each ticket gets its own branch and directory."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.executors.local import LocalExecutor

_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` and return stdout; raise GitError on failure."""
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass(frozen=True)
class Worktree:
    ticket_id: str
    path: Path
    branch: str

    def executor(self) -> LocalExecutor:
        return LocalExecutor(self.path)


class WorktreeManager:
    def __init__(self, repo_root: Path, worktrees_dir: Path | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.worktrees_dir = (worktrees_dir or self.repo_root / ".harness" / "worktrees").resolve()

    @staticmethod
    def branch_for(ticket_id: str) -> str:
        return f"ticket/{ticket_id}"

    def path_for(self, ticket_id: str) -> Path:
        if not _TICKET_ID_RE.match(ticket_id):
            raise ValueError(f"invalid ticket id for a worktree: {ticket_id!r}")
        return self.worktrees_dir / ticket_id

    def create(self, ticket_id: str, base: str = "main") -> Worktree:
        """Create (or reuse) the ticket's worktree on branch ``ticket/<id>`` from ``base``."""
        path = self.path_for(ticket_id)
        branch = self.branch_for(ticket_id)
        if path.exists():
            return Worktree(ticket_id, path, branch)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        branch_exists = bool(git(self.repo_root, "branch", "--list", branch).strip())
        if branch_exists:
            git(self.repo_root, "worktree", "add", str(path), branch)
        else:
            git(self.repo_root, "worktree", "add", "-b", branch, str(path), base)
        return Worktree(ticket_id, path, branch)

    def remove(self, ticket_id: str, *, delete_branch: bool = False) -> None:
        path = self.path_for(ticket_id)
        if path.exists():
            git(self.repo_root, "worktree", "remove", "--force", str(path))
        git(self.repo_root, "worktree", "prune")
        if delete_branch:
            branch = self.branch_for(ticket_id)
            if git(self.repo_root, "branch", "--list", branch).strip():
                git(self.repo_root, "branch", "-D", branch)

    def list(self) -> list[Worktree]:
        out = git(self.repo_root, "worktree", "list", "--porcelain")
        result: list[Worktree] = []
        path: Path | None = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree ")).resolve()
            elif (
                line.startswith("branch refs/heads/ticket/")
                and path is not None
                and path.parent == self.worktrees_dir
            ):
                branch = line.removeprefix("branch refs/heads/")
                result.append(Worktree(branch.removeprefix("ticket/"), path, branch))
        return result

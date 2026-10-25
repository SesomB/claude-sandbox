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
    proc = git_run(repo, *args)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the completed process, without raising."""
    return subprocess.run(
        ["git", *_identity(repo), *args], cwd=repo, capture_output=True, text=True
    )


def _identity(repo: Path) -> list[str]:
    """A fallback committer identity when the user has none configured."""
    proc = subprocess.run(["git", "config", "user.email"], cwd=repo, capture_output=True, text=True)
    if proc.stdout.strip():
        return []
    return ["-c", "user.name=harness", "-c", "user.email=harness@localhost"]


def changed_files(repo: Path) -> list[str]:
    """Paths with uncommitted changes (incl. untracked and deleted), repo-relative."""
    out = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:  # rename/copy: next entry is the source path
            paths.append(entries[i])
            i += 1
    return sorted(set(paths))


def commit_paths(repo: Path, paths: list[str], message: str) -> str | None:
    """Stage exactly ``paths`` (incl. deletions) and commit; returns the sha or None."""
    if not paths:
        return None
    git(repo, "add", "-A", "--", *paths)
    if not git(repo, "diff", "--cached", "--name-only").strip():
        return None
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


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

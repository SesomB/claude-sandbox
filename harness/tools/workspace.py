"""A ticket's workspace: its worktree plus the file permissions its ticket grants."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from harness.board import Board, Ticket
from harness.board.models import normalize_repo_path
from harness.executors.local import LocalExecutor
from harness.executors.worktree import Worktree, changed_files, commit_paths, git
from harness.stacks import StackProfile
from harness.tools.base import ToolError

BASE_BRANCH = "main"


@dataclass
class TicketWorkspace:
    ticket: Ticket
    worktree: Worktree
    board: Board
    stacks: list[StackProfile]
    frozen: set[str] = field(default_factory=set)  # tests locked after a verified red

    @property
    def root(self) -> Path:
        return self.worktree.path

    @property
    def executor(self) -> LocalExecutor:
        return self.worktree.executor()

    # -- permissions ---------------------------------------------------------------------

    @staticmethod
    def norm(path: str) -> str:
        try:
            return normalize_repo_path(path)
        except ValueError as e:
            raise ToolError(str(e)) from e

    def is_shared(self, path: str) -> bool:
        return any(s.is_shared_file(path) for s in self.stacks)

    def is_artifact(self, path: str) -> bool:
        return path.startswith(".harness/") or any(s.is_artifact(path) for s in self.stacks)

    @property
    def owned(self) -> set[str]:
        return set(self.ticket.owned_files) | set(self.ticket.tests_required)

    def readable(self, path: str) -> bool:
        return path in self.owned or path in self.ticket.readable_files or self.is_shared(path)

    def check_read(self, path: str) -> str:
        path = self.norm(path)
        if not self.readable(path):
            raise ToolError(
                f"{path} is outside this ticket's scope. Readable: owned files, "
                "readable_files, and shared files. If you truly need it, finish with a "
                "note explaining why so the ticket can be re-scoped."
            )
        return path

    def check_write(self, path: str) -> str:
        path = self.norm(path)
        if path in self.frozen:
            raise ToolError(
                f"{path} holds a verified failing test and is frozen; change the code under "
                "test, not the test."
            )
        if self.is_shared(path):
            raise ToolError(f"{path} is a shared file; use the edit_shared_file tool")
        if path not in self.owned:
            raise ToolError(
                f"{path} is not owned by {self.ticket.id}. Owned: {', '.join(sorted(self.owned))}"
            )
        return path

    def stack_for(self, path: str) -> StackProfile | None:
        for s in self.stacks:
            if s.is_test_file(path) or s.is_source_file(path):
                return s
        return None

    # -- git state ----------------------------------------------------------------------

    def changed(self) -> list[str]:
        """Uncommitted changes, ignoring build artifacts and caches."""
        return [p for p in changed_files(self.root) if not self.is_artifact(p)]

    def shared_paths_edited(self) -> set[str]:
        return {path for path, _ in self.board.shared_edits(self.ticket.id)}

    def allowed_changes(self) -> set[str]:
        return self.owned | self.shared_paths_edited()

    def scope_violations(self, paths: list[str] | None = None) -> list[str]:
        paths = self.changed() if paths is None else paths
        allowed = self.allowed_changes()
        return [p for p in paths if p not in allowed]

    def branch_changes(self) -> list[str]:
        """Files changed on the ticket branch relative to main (committed only)."""
        out = git(self.root, "diff", "--name-only", f"{BASE_BRANCH}...HEAD")
        return [p for p in out.splitlines() if p]

    def diff(self) -> str:
        return git(self.root, "diff", f"{BASE_BRANCH}...HEAD")

    def commit(self, message: str, paths: list[str] | None = None) -> str | None:
        """Commit the given (or all allowed) changes. Out-of-scope changes are refused."""
        paths = self.changed() if paths is None else paths
        bad = self.scope_violations(paths)
        if bad:
            raise ToolError(f"refusing to commit out-of-scope changes: {', '.join(bad)}")
        return commit_paths(self.root, paths, message)

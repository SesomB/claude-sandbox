"""Merging: syncing ticket branches with main and the serialized merge queue into main.

Open tickets never own the same files, so the only conflicts possible are in shared
files. Those are resolved mechanically: take main's version and replay the ticket's
recorded shared-file edits on top of it.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from harness.board import Board, Ticket
from harness.executors.local import LocalExecutor
from harness.executors.worktree import GitError, WorktreeManager, git, git_run
from harness.gates.checks import GateReport, build_and_test
from harness.stacks import StackProfile
from harness.tools.shared import SharedEditError, replay
from harness.tools.workspace import BASE_BRANCH


@dataclass(frozen=True)
class MergeResult:
    ok: bool
    stage: str = ""  # "conflict" | "gates" | "main_checkout"
    detail: str = ""
    commit: str | None = None


def group_ops(edits: list[tuple[str, dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path, op in edits:
        out[path].append(op)
    return dict(out)


def merge_ref(
    repo: Path,
    ref: str,
    *,
    message: str,
    main_side: Literal["ours", "theirs"],
    ops: dict[str, list[dict[str, Any]]],
    is_shared: Callable[[str], bool],
) -> MergeResult:
    """Merge ``ref`` into the checkout at ``repo``, auto-resolving shared-file conflicts.

    ``main_side`` says which side of the merge is main's version of the files.
    On failure the merge is aborted and the checkout is unchanged.
    """
    proc = git_run(repo, "merge", "--no-ff", "--no-edit", "-m", message, ref)
    if proc.returncode == 0:
        return MergeResult(True, commit=git(repo, "rev-parse", "HEAD").strip())
    conflicted = [p for p in git(repo, "diff", "--name-only", "--diff-filter=U").splitlines() if p]
    if not conflicted:
        git_run(repo, "merge", "--abort")
        return MergeResult(False, "conflict", (proc.stderr or proc.stdout).strip())
    unresolvable = [p for p in conflicted if not is_shared(p) or p not in ops]
    if unresolvable:
        git_run(repo, "merge", "--abort")
        return MergeResult(
            False, "conflict", f"conflicts outside replayable shared edits: {unresolvable}"
        )
    try:
        for path in conflicted:
            git(repo, "checkout", f"--{main_side}", "--", path)
            content = (repo / path).read_text()
            (repo / path).write_text(replay(content, ops[path]))
            git(repo, "add", "--", path)
        git(repo, "commit", "-q", "--no-edit", "-m", message)
    except (GitError, SharedEditError, OSError) as e:
        git_run(repo, "merge", "--abort")
        return MergeResult(False, "conflict", f"could not replay shared edits: {e}")
    return MergeResult(True, commit=git(repo, "rev-parse", "HEAD").strip())


class MergeQueue:
    """Merges approved tickets into main one at a time, gating the exact merged commit."""

    def __init__(
        self, root: Path, board: Board, stacks: list[StackProfile], worktrees: WorktreeManager
    ) -> None:
        self.root = root
        self.board = board
        self.stacks = stacks
        self.worktrees = worktrees
        self.merge_dir = worktrees.worktrees_dir / "_merge"
        self._lock = asyncio.Lock()

    def _prepare(self) -> str:
        if not self.merge_dir.exists():
            self.merge_dir.parent.mkdir(parents=True, exist_ok=True)
            git(self.root, "worktree", "add", "--detach", str(self.merge_dir), BASE_BRANCH)
        git(self.merge_dir, "checkout", "-q", "--detach", "-f", BASE_BRANCH)
        git(self.merge_dir, "reset", "-q", "--hard", BASE_BRANCH)
        git(self.merge_dir, "clean", "-q", "-ffd")
        return git(self.merge_dir, "rev-parse", "HEAD").strip()

    def _advance_main(self, old: str, new: str) -> MergeResult:
        head = git_run(self.root, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
        if head == BASE_BRANCH:
            # The primary checkout is on main: fast-forward it so its files update too.
            proc = git_run(self.root, "merge", "-q", "--ff-only", new)
            if proc.returncode != 0:
                return MergeResult(
                    False,
                    "main_checkout",
                    "could not fast-forward the main checkout (local changes in the way?): "
                    + (proc.stderr or proc.stdout).strip(),
                )
        else:
            git(self.root, "update-ref", f"refs/heads/{BASE_BRANCH}", new, old)
        return MergeResult(True, commit=new)

    async def merge(self, ticket: Ticket) -> MergeResult:
        return await self.merge_branch(
            WorktreeManager.branch_for(ticket.id),
            message=f"Merge {ticket.id}: {ticket.title}",
            ops=group_ops(self.board.shared_edits(ticket.id)),
            ticket_id=ticket.id,
        )

    async def merge_branch(
        self,
        branch: str,
        *,
        message: str,
        ops: dict[str, list[dict[str, Any]]] | None = None,
        stacks: list[StackProfile] | None = None,
        ticket_id: str | None = None,
    ) -> MergeResult:
        """Merge ``branch`` into main, gating the exact merged commit before main moves."""
        stacks = self.stacks if stacks is None else stacks
        async with self._lock:
            old = self._prepare()
            res = merge_ref(
                self.merge_dir,
                branch,
                message=message,
                main_side="ours",
                ops=ops or {},
                is_shared=lambda p: any(s.is_shared_file(p) for s in stacks),
            )
            if not res.ok:
                return res
            changed = git(self.merge_dir, "diff", "--name-only", old, "HEAD").splitlines()
            report = GateReport()
            await build_and_test(stacks, LocalExecutor(self.merge_dir), report, changed=changed)
            self.board.log_event(ticket_id, "merge.gates", branch=branch, **report.as_payload())
            if not report.ok:
                git(self.merge_dir, "reset", "-q", "--hard", old)
                return MergeResult(False, "gates", report.summary())
            assert res.commit is not None
            return self._advance_main(old, res.commit)

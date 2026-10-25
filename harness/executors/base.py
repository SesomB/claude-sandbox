"""The Executor protocol: every command and file access an agent makes goes through one.

An executor is rooted at a directory (normally a ticket's worktree) and refuses
to touch paths outside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class PathEscapeError(PermissionError):
    """A path resolved outside the executor's root."""


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class Executor(Protocol):
    @property
    def root(self) -> Path: ...

    async def run(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout_s: float = 600.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...

    def read_text(self, path: str) -> str: ...

    def write_text(self, path: str, content: str) -> None: ...

    def exists(self, path: str) -> bool: ...

    def list_files(self, pattern: str = "**/*") -> list[str]: ...

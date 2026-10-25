"""Runs commands and file operations directly on this machine, confined to a root dir."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from pathlib import Path

from harness.executors.base import CommandResult, PathEscapeError

MAX_OUTPUT_CHARS = 200_000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return (
        f"{text[:half]}\n... [{len(text) - MAX_OUTPUT_CHARS} chars truncated] ...\n{text[-half:]}"
    )


class LocalExecutor:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str) -> Path:
        """Absolute path for ``path`` (relative to root); refuses escapes incl. via symlinks."""
        p = (self._root / path).resolve()
        if p != self._root and self._root not in p.parents:
            raise PathEscapeError(f"{path!r} is outside {self._root}")
        return p

    async def run(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout_s: float = 600.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        workdir = self.resolve(cwd)
        full_env = {**os.environ, **(env or {})}
        start = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workdir,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group, so a timeout kills children too
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            out, err = await proc.communicate()
        return CommandResult(
            command=command,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=_truncate(out.decode(errors="replace")),
            stderr=_truncate(err.decode(errors="replace")),
            duration_s=time.monotonic() - start,
            timed_out=timed_out,
        )

    def read_text(self, path: str) -> str:
        return self.resolve(path).read_text()

    def write_text(self, path: str, content: str) -> None:
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def exists(self, path: str) -> bool:
        return self.resolve(path).exists()

    def list_files(self, pattern: str = "**/*") -> list[str]:
        return sorted(
            p.relative_to(self._root).as_posix()
            for p in self._root.glob(pattern)
            if p.is_file() and ".git" not in p.relative_to(self._root).parts
        )

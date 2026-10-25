"""File tools for phase agents (spec, architecture, decomposition), with a path policy."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field

from harness.board.models import normalize_repo_path
from harness.executors.local import LocalExecutor
from harness.gates.checks import GateReport, build_and_test
from harness.stacks import StackProfile
from harness.tools.base import Tool, ToolError
from harness.tools.files import EditFileInput, NoInput, ReadFileInput, WriteFileInput, replace_once

PathCheck = Callable[[str], bool]


def _never_internal(path: str) -> bool:
    return not path.startswith((".harness/", ".git/")) and path not in (".harness", ".git")


def phase_file_tools(
    executor: LocalExecutor, *, can_write: PathCheck, writable_hint: str
) -> list[Tool]:
    """Read any project file; write only where ``can_write`` allows."""

    def norm(path: str) -> str:
        try:
            p = normalize_repo_path(path)
        except ValueError as e:
            raise ToolError(str(e)) from e
        if not _never_internal(p):
            raise ToolError(f"{p} is internal to the harness")
        return p

    def writable(path: str) -> str:
        p = norm(path)
        if not can_write(p):
            raise ToolError(f"you may not write {p}; you may write: {writable_hint}")
        return p

    async def read_file(args: ReadFileInput) -> str:
        p = norm(args.path)
        if not executor.exists(p):
            raise ToolError(f"{p} does not exist")
        return executor.read_text(p)

    async def list_files(_: NoInput) -> str:
        files = [f for f in executor.list_files() if _never_internal(f)]
        return "\n".join(files) or "(no files yet)"

    async def write_file(args: WriteFileInput) -> str:
        p = writable(args.path)
        executor.write_text(p, args.content)
        return f"wrote {p} ({len(args.content)} chars)"

    async def edit_file(args: EditFileInput) -> str:
        p = writable(args.path)
        if not executor.exists(p):
            raise ToolError(f"{p} does not exist; use write_file")
        executor.write_text(p, replace_once(executor.read_text(p), args.old, args.new, p))
        return f"edited {p}"

    return [
        Tool("read_file", "Read a project file.", ReadFileInput, read_file),
        Tool("list_files", "List the project's files.", NoInput, list_files),
        Tool(
            "write_file",
            f"Create or overwrite a file ({writable_hint}).",
            WriteFileInput,
            write_file,
        ),
        Tool("edit_file", "Replace one exact, unique snippet in a file.", EditFileInput, edit_file),
    ]


class CheckStacksInput(BaseModel):
    stacks: list[str] = Field(description="Stack profile names, e.g. ['python']")


def check_stacks_tool(executor: LocalExecutor, profiles: dict[str, StackProfile]) -> Tool:
    async def check_stacks(args: CheckStacksInput) -> str:
        unknown = [s for s in args.stacks if s not in profiles]
        if unknown:
            raise ToolError(f"unknown stacks {unknown}; available: {sorted(profiles)}")
        report = GateReport()
        await build_and_test([profiles[s] for s in args.stacks], executor, report)
        return ("ALL PASSED\n" if report.ok else "FAILED\n") + report.summary()

    return Tool(
        "check_stacks",
        "Configure, build and run the tests of the skeleton with the given stacks.",
        CheckStacksInput,
        check_stacks,
    )

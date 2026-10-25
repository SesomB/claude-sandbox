"""The scoped tools agents use inside a ticket workspace."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from harness.gates.checks import GateReport, build_and_test, describe, run_step
from harness.tools.base import Tool, ToolError
from harness.tools.shared import SharedEditError, SharedOp, apply_op
from harness.tools.workspace import TicketWorkspace


class ReadFileInput(BaseModel):
    path: str = Field(description="Repo-relative path")


class WriteFileInput(BaseModel):
    path: str = Field(description="Repo-relative path of a file this ticket owns")
    content: str = Field(description="The complete new file content")


class EditFileInput(BaseModel):
    path: str
    old: str = Field(description="Exact existing text; must occur exactly once")
    new: str = Field(description="Replacement text")


class NoInput(BaseModel):
    pass


class RunTestsInput(BaseModel):
    test_id: str | None = Field(
        default=None,
        description="One test (e.g. 'tests/test_x.py::test_y' or a CTest name); omit for all",
    )


class RunCheckInput(BaseModel):
    step: Literal["build", "lint", "typecheck"]


class SharedEditInput(BaseModel):
    path: str = Field(description="A shared file, e.g. CMakeLists.txt or pyproject.toml")
    op: Literal["append", "replace"]
    text: str = Field(description="Text to append, or the replacement text")
    old: str | None = Field(default=None, description="For replace: exact text, occurring once")


def read_tools(ws: TicketWorkspace) -> list[Tool]:
    async def read_file(args: ReadFileInput) -> str:
        path = ws.check_read(args.path)
        if not ws.executor.exists(path):
            raise ToolError(f"{path} does not exist yet")
        return ws.executor.read_text(path)

    async def list_files(_: NoInput) -> str:
        scope = sorted(ws.owned | set(ws.ticket.readable_files))
        lines = [f"{p}{'' if ws.executor.exists(p) else '  (does not exist yet)'}" for p in scope]
        shared = [p for p in ws.executor.list_files() if ws.is_shared(p)]
        if shared:
            lines.append("shared (edit via edit_shared_file): " + ", ".join(shared))
        return "\n".join(lines)

    async def run_tests(args: RunTestsInput) -> str:
        report = GateReport()
        if args.test_id is None:
            await build_and_test(ws.stacks, ws.executor, report)
        else:
            for stack in ws.stacks:
                if not stack.test_one:
                    continue
                if await run_step(stack, "configure", ws.executor, report) and await run_step(
                    stack, "build", ws.executor, report
                ):
                    res = await stack.run("test_one", ws.executor, test_id=args.test_id)
                    assert res is not None
                    report.add(f"{stack.name}:test_one", res.ok, describe(res))
        if not report.results:
            return "no test command is configured for this project's stacks"
        return ("ALL PASSED\n" if report.ok else "FAILED\n") + report.summary()

    return [
        Tool("read_file", "Read a file in this ticket's scope.", ReadFileInput, read_file),
        Tool(
            "list_files",
            "List the files in this ticket's scope and the shared files.",
            NoInput,
            list_files,
        ),
        Tool("run_tests", "Build and run all tests, or one test.", RunTestsInput, run_tests),
    ]


def write_tools(ws: TicketWorkspace) -> list[Tool]:
    async def write_file(args: WriteFileInput) -> str:
        path = ws.check_write(args.path)
        ws.executor.write_text(path, args.content)
        return f"wrote {path} ({len(args.content)} chars)"

    async def edit_file(args: EditFileInput) -> str:
        path = ws.check_write(args.path)
        if not ws.executor.exists(path):
            raise ToolError(f"{path} does not exist; use write_file to create it")
        text = ws.executor.read_text(path)
        count = text.count(args.old)
        if count != 1:
            raise ToolError(
                f"`old` occurs {count} times in {path}; it must occur exactly once"
                + (" (re-read the file)" if count == 0 else " (add surrounding context)")
            )
        ws.executor.write_text(path, text.replace(args.old, args.new, 1))
        return f"edited {path}"

    async def run_check(args: RunCheckInput) -> str:
        report = GateReport()
        changed = ws.changed()
        for stack in ws.stacks:
            files = [p for p in changed if stack.is_source_file(p) or stack.is_test_file(p)]
            if await run_step(stack, "configure", ws.executor, report):
                await run_step(stack, args.step, ws.executor, report, files=files)
        if not report.results:
            return f"nothing to {args.step}"
        return ("OK\n" if report.ok else "FAILED\n") + report.summary()

    async def edit_shared_file(args: SharedEditInput) -> str:
        path = ws.norm(args.path)
        if not ws.is_shared(path):
            raise ToolError(f"{path} is not a shared file; edit it with write_file/edit_file")
        op = SharedOp(op=args.op, text=args.text, old=args.old)
        current = ws.executor.read_text(path) if ws.executor.exists(path) else None
        try:
            new = apply_op(current, op)
        except SharedEditError as e:
            raise ToolError(str(e)) from e
        ws.executor.write_text(path, new)
        ws.board.record_shared_edit(ws.ticket.id, path, op.model_dump())
        return f"applied {args.op} to shared file {path}"

    return [
        Tool(
            "write_file",
            "Create or overwrite a file this ticket owns.",
            WriteFileInput,
            write_file,
        ),
        Tool(
            "edit_file",
            "Replace one exact, unique snippet in a file this ticket owns.",
            EditFileInput,
            edit_file,
        ),
        Tool(
            "run_check",
            "Run the build, linter, or type checker on your changes.",
            RunCheckInput,
            run_check,
        ),
        Tool(
            "edit_shared_file",
            "Append to or replace text in a shared file (build files, dependency "
            "manifests). These edits are replayed at merge time, so keep them minimal.",
            SharedEditInput,
            edit_shared_file,
        ),
    ]

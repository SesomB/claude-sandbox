"""Agent runners: who drives the agent loop for a role.

- `ApiRunner` (in loop.py): the harness's own tool loop over a model API.
- `ClaudeCodeRunner` / `OpenCodeRunner`: a coding-agent CLI drives the loop, using your
  existing login (e.g. a Claude subscription). Its built-in tools are disabled; it acts
  only through the harness's tools, served to it by an `McpBridge`.

Provider config (`providers.<name>`):
  type: claude_code | opencode
  command: path or name of the CLI          (default: claude / opencode)
  extra_args: [..]                          appended to every invocation
  env: {..}                                 extra environment for the CLI
  timeout_s: 3600                           per invocation
  nudges: 2                                 resumes asking it to call `finish`
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from harness.agents.loop import (
    FINISH,
    AgentAbort,
    AgentRun,
    AgentRunner,
    ApiRunner,
    OnFinish,
    RetryPolicy,
)
from harness.agents.mcp_bridge import McpBridge
from harness.config import HarnessConfig, ProviderConfig, RoleModel
from harness.models import ModelClient, client_for_role
from harness.models.base import Message, ToolResultBlock, ToolSpec, ToolUseBlock, Usage
from harness.tools.base import Toolbox, tail

R = TypeVar("R", bound=BaseModel)

SERVER = "harness"
NUDGE = (
    "You stopped without calling the `finish` tool, so the harness has not recorded your "
    "work. Continue the task with the harness tools; when you are done, call `finish`."
)
FINISH_ACCEPTED = "Accepted. Your work is complete: end your turn now without calling more tools."
GRACE_S = 20.0
UNREADABLE = "unreadable output from"


@dataclass
class CliOutput:
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None
    text: str = ""
    error: str = ""  # set when the CLI reported a failure
    retryable: bool = True


class _RunState:
    """Tool calls arriving from the CLI, handled by the harness's own toolbox."""

    def __init__(
        self, toolbox: Toolbox, result_model: type[R], on_finish: OnFinish[R], max_calls: int
    ) -> None:
        self.toolbox = toolbox
        self.result_model: type[Any] = result_model
        self.on_finish: OnFinish[Any] = on_finish
        self.max_calls = max_calls
        self.calls = 0
        self.messages: list[Message] = []
        self.outcome: str | None = None
        self.result: Any = None
        self.reason = ""
        self.stopped = asyncio.Event()
        self._lock = asyncio.Lock()

    def _stop(self, outcome: str, reason: str = "") -> None:
        self.outcome, self.reason = outcome, reason
        self.stopped.set()

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        async with self._lock:  # one tool at a time, like the API loop
            if self.stopped.is_set():
                return "The run is over; stop now.", True
            self.calls += 1
            use = ToolUseBlock(id=f"call_{self.calls}", name=name, input=args)
            if self.calls > self.max_calls:
                self._stop("max_turns", f"more than {self.max_calls} tool calls")
                text, is_error = "Tool-call limit reached; the harness stopped this run.", True
            elif name == FINISH:
                text, is_error = await self._finish(args)
            else:
                res = await self.toolbox.call(use)
                text, is_error = res.content, res.is_error
            self.messages.append(Message(role="assistant", content=[use]))
            self.messages.append(
                Message(
                    role="user",
                    content=[ToolResultBlock(tool_use_id=use.id, content=text, is_error=is_error)],
                )
            )
            return text, is_error

    async def _finish(self, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            parsed = self.result_model.model_validate(args)
        except ValidationError as e:
            return f"invalid finish input: {e}", True
        try:
            rejection = await self.on_finish(parsed)
        except AgentAbort as e:
            self._stop("aborted", str(e))
            return f"Stopped by the harness: {e}", True
        if rejection is not None:
            return rejection, True
        self.result = parsed
        self._stop("finished")
        return FINISH_ACCEPTED, False


class CliRunner:
    """Common machinery: bridge, subprocess, nudges, retries. Subclasses build the argv."""

    default_command = ""
    tool_prefix = ""

    def __init__(
        self, provider: ProviderConfig, role_model: RoleModel, *, retry: RetryPolicy | None = None
    ) -> None:
        extra = provider.model_extra or {}
        self.role_model = role_model
        self.command = str(extra.get("command") or self.default_command)
        self.extra_args = [str(a) for a in extra.get("extra_args") or []]
        self.env = {str(k): str(v) for k, v in (extra.get("env") or {}).items()}
        self.timeout_s = float(extra.get("timeout_s", 3600))
        self.nudges = int(extra.get("nudges", 2))
        self.retry = retry or RetryPolicy(attempts=3)

    # -- subclass hooks -------------------------------------------------------------------

    def command_line(
        self, *, system: str, prompt: str, url: str, cwd: Path, session_id: str | None
    ) -> tuple[list[str], dict[str, str], bytes | None]:
        raise NotImplementedError

    def parse(self, stdout: str) -> CliOutput:
        raise NotImplementedError

    # -- running ----------------------------------------------------------------------------

    def _system(self, system: str) -> str:
        return (
            f"{system}\n\nYour tools are served by the `{SERVER}` MCP server (they appear "
            f"with the prefix `{self.tool_prefix}`, e.g. `{self.tool_prefix}{FINISH}`). They "
            "are the only way to read or change files and to run checks. Your work counts only "
            f"when `{FINISH}` accepts it."
        )

    async def run(
        self,
        *,
        system: str,
        prompt: str,
        toolbox: Toolbox,
        result_model: type[R],
        finish_description: str,
        on_finish: OnFinish[R],
        max_turns: int = 80,
        cwd: Path | None = None,
    ) -> AgentRun[R]:
        cwd = cwd or Path.cwd()
        schema = result_model.model_json_schema()
        schema.pop("title", None)
        specs = [
            *toolbox.specs(),
            ToolSpec(name=FINISH, description=finish_description, input_schema=schema),
        ]
        state = _RunState(toolbox, result_model, on_finish, max_calls=max_turns)
        usage, cost, session_id, error = Usage(), None, None, ""
        async with McpBridge(specs, state.call, name=SERVER) as bridge:
            attempt = failures = 0
            message = prompt
            while not state.stopped.is_set():
                out = await self._invoke(
                    system=self._system(system),
                    prompt=message,
                    url=bridge.url,
                    cwd=cwd,
                    session_id=session_id,
                    state=state,
                )
                usage = usage + out.usage
                if out.cost_usd is not None:
                    cost = (cost or 0.0) + out.cost_usd
                session_id = out.session_id or session_id
                if state.stopped.is_set():
                    break
                if out.error and session_id is None and out.retryable:
                    # Failed before a session existed (auth, rate limit, network): retry.
                    failures += 1
                    error = out.error
                    if failures >= self.retry.attempts:
                        break
                    delay = min(self.retry.base_delay_s * 2**failures, self.retry.max_delay_s)
                    await self.retry.sleep(delay + random.uniform(0, delay / 4))
                    continue
                error = out.error
                if not out.retryable:
                    break
                attempt += 1
                if attempt > self.nudges or session_id is None:
                    break
                message = NUDGE
        run: AgentRun[R] = AgentRun(
            outcome="no_finish",
            usage=usage,
            cost_usd=cost,
            messages=state.messages,
            turns=state.calls,
        )
        if state.outcome is not None:
            run.outcome = state.outcome  # type: ignore[assignment]
            run.reason = state.reason
            run.result = state.result
        elif error:
            run.outcome, run.reason = "model_error", error
        else:
            run.reason = "the agent stopped without calling finish"
        return run

    async def _invoke(
        self,
        *,
        system: str,
        prompt: str,
        url: str,
        cwd: Path,
        session_id: str | None,
        state: _RunState,
    ) -> CliOutput:
        argv, env, stdin = self.command_line(
            system=system, prompt=prompt, url=url, cwd=cwd, session_id=session_id
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env={**os.environ, **self.env, **env},
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            return CliOutput(
                error=f"`{argv[0]}` was not found; install it or set `command`", retryable=False
            )
        comm = asyncio.create_task(proc.communicate(stdin))
        stop = asyncio.create_task(state.stopped.wait())
        done, _ = await asyncio.wait(
            {comm, stop}, timeout=self.timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        timed_out = not done
        if comm not in done:
            if stop in done:  # finished or aborted: let the CLI wrap up briefly
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(comm), GRACE_S)
            if not comm.done():
                await _terminate(proc)
        stop.cancel()
        out_b, err_b = await comm
        out = self.parse(out_b.decode(errors="replace"))
        if timed_out and not state.stopped.is_set():
            out.error = out.error or f"`{argv[0]}` timed out after {self.timeout_s:.0f}s"
        elif (
            proc.returncode not in (0, None)
            and not state.stopped.is_set()
            and (not out.error or out.error.startswith(UNREADABLE))
        ):
            # The CLI's own message (e.g. "not logged in") beats "unreadable output".
            out.error = f"`{argv[0]}` exited with {proc.returncode}: " + tail(
                err_b.decode(errors="replace") or out_b.decode(errors="replace"), 1500
            )
        return out


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), 5)
            return
        except TimeoutError:
            continue


class ClaudeCodeRunner(CliRunner):
    """`claude -p` with only the harness's MCP tools (built-in tools disabled)."""

    default_command = "claude"
    tool_prefix = f"mcp__{SERVER}__"

    def command_line(
        self, *, system: str, prompt: str, url: str, cwd: Path, session_id: str | None
    ) -> tuple[list[str], dict[str, str], bytes | None]:
        mcp = {"mcpServers": {SERVER: {"type": "http", "url": url}}}
        argv = [
            self.command,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.role_model.model,
            "--system-prompt",
            system,
            "--tools",
            "",
            "--mcp-config",
            json.dumps(mcp),
            "--strict-mcp-config",
            "--allowedTools",
            f"mcp__{SERVER}",
            "--permission-mode",
            "dontAsk",
        ]
        if self.role_model.effort:
            argv += ["--effort", self.role_model.effort]
        if session_id:
            argv += ["--resume", session_id]
        return [*argv, *self.extra_args], {}, prompt.encode()

    def parse(self, stdout: str) -> CliOutput:
        data: dict[str, Any] | None = None
        for line in reversed(stdout.strip().splitlines()):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                data = obj
                break
        if data is None:
            return CliOutput(error=f"{UNREADABLE} claude: {tail(stdout, 500)}")
        u = data.get("usage") or {}
        out = CliOutput(
            session_id=data.get("session_id"),
            usage=Usage(
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_read_input_tokens=int(u.get("cache_read_input_tokens") or 0),
                cache_creation_input_tokens=int(u.get("cache_creation_input_tokens") or 0),
            ),
            cost_usd=data.get("total_cost_usd"),
            text=str(data.get("result") or ""),
        )
        if data.get("is_error"):
            out.error = f"claude: {data.get('subtype', 'error')}: {tail(out.text, 1500)}"
        return out


# OpenCode's built-in tools, all switched off so only the harness's MCP tools remain.
OPENCODE_BUILTINS = (
    "bash",
    "edit",
    "write",
    "read",
    "grep",
    "glob",
    "list",
    "patch",
    "webfetch",
    "websearch",
    "codesearch",
    "task",
    "todowrite",
    "todoread",
    "lsp",
    "skill",
    "question",
)


class OpenCodeRunner(CliRunner):
    """`opencode run` with a generated agent whose only tools are the harness's."""

    default_command = "opencode"
    tool_prefix = f"{SERVER}_"

    def command_line(
        self, *, system: str, prompt: str, url: str, cwd: Path, session_id: str | None
    ) -> tuple[list[str], dict[str, str], bytes | None]:
        tools: dict[str, bool] = {t: False for t in OPENCODE_BUILTINS}
        tools[f"{SERVER}_*"] = True
        config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {SERVER: {"type": "remote", "url": url, "enabled": True}},
            "tools": tools,
            "agent": {
                SERVER: {
                    "description": "Harness agent: works only through the harness's tools",
                    "mode": "primary",
                    "prompt": system,
                    "tools": tools,
                }
            },
        }
        argv = [
            self.command,
            "run",
            "--format",
            "json",
            "--model",
            self.role_model.model,
            "--agent",
            SERVER,
            "--dir",
            str(cwd),
            "--auto",
        ]
        if self.role_model.effort:
            argv += ["--variant", self.role_model.effort]
        if session_id:
            argv += ["--session", session_id]
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(config)}
        return [*argv, *self.extra_args, prompt], env, None

    def parse(self, stdout: str) -> CliOutput:
        out = CliOutput()
        texts: list[str] = []
        cost = 0.0
        saw_cost = False
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            raw_part = ev.get("part")
            part: dict[str, Any] = raw_part if isinstance(raw_part, dict) else {}
            out.session_id = ev.get("sessionID") or part.get("sessionID") or out.session_id
            kind = str(ev.get("type") or part.get("type") or "")
            if kind in ("step_finish", "step-finish"):
                tokens = part.get("tokens") or {}
                cache = tokens.get("cache") or {}
                out.usage = out.usage + Usage(
                    input_tokens=int(tokens.get("input") or 0),
                    output_tokens=int(tokens.get("output") or 0)
                    + int(tokens.get("reasoning") or 0),
                    cache_read_input_tokens=int(cache.get("read") or 0),
                    cache_creation_input_tokens=int(cache.get("write") or 0),
                )
                if part.get("cost") is not None:
                    cost += float(part["cost"])
                    saw_cost = True
            elif kind == "text" and part.get("text"):
                texts.append(str(part["text"]))
            elif kind == "error":
                err = ev.get("error") or part.get("error") or ev
                out.error = f"opencode: {tail(json.dumps(err), 1500)}"
        out.text = "\n".join(texts)
        out.cost_usd = cost if saw_cost else None
        return out


CLI_RUNNERS: dict[str, type[CliRunner]] = {
    "claude_code": ClaudeCodeRunner,
    "opencode": OpenCodeRunner,
}


def runner_for_role(
    config: HarnessConfig,
    role: str,
    *,
    client_factory: Callable[[str], ModelClient] | None = None,
    retry: RetryPolicy | None = None,
) -> AgentRunner:
    """The runner configured for ``role``: a CLI agent or the built-in API loop."""
    role_model = config.model_for(role)
    if client_factory is not None:  # tests and embedders supply clients directly
        return ApiRunner(client_factory(role), max_tokens=role_model.max_tokens, retry=retry)
    provider = config.providers[role_model.provider]
    cli = CLI_RUNNERS.get(provider.type)
    if cli is not None:
        return cli(provider, role_model, retry=retry)
    return ApiRunner(client_for_role(config, role), max_tokens=role_model.max_tokens, retry=retry)

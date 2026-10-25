"""Claude Code / OpenCode as agent backends, against a fake CLI that speaks real MCP."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from harness.agents.loop import AgentAbort, ApiRunner, RetryPolicy
from harness.agents.mcp_bridge import McpBridge
from harness.agents.runners import (
    NUDGE,
    ClaudeCodeRunner,
    OpenCodeRunner,
    runner_for_role,
)
from harness.board import TicketStatus
from harness.config import HarnessConfig, ProviderConfig, RoleModel
from harness.core.orchestrator import Orchestrator
from harness.core.usage import usage_report
from harness.models import FakeModelClient, ToolSpec
from harness.tools.base import Tool, Toolbox
from tests.helpers import make_app, ticket

FAKE = Path(__file__).parent / "fake_cli.py"


class Result(BaseModel):
    answer: int


class EchoIn(BaseModel):
    text: str


def echo_box(log: list[str]) -> Toolbox:
    async def echo(args: EchoIn) -> str:
        log.append(args.text)
        return f"echo: {args.text}"

    return Toolbox([Tool("echo", "Echo text", EchoIn, echo)])


async def accept(_: Result) -> str | None:
    return None


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def fake_cli(tmp_path: Path) -> Callable[[list[list[dict[str, Any]]]], tuple[str, Path]]:
    def make(invocations: list[list[dict[str, Any]]]) -> tuple[str, Path]:
        script = tmp_path / f"script-{len(list(tmp_path.glob('script-*.json')))}.json"
        script.write_text(json.dumps({"invocations": invocations}))
        wrapper = tmp_path / f"{script.stem}-cli"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        return str(wrapper), script

    return make


def logged(script: Path) -> list[dict[str, Any]]:
    return [
        json.loads(x) for x in (script.parent / (script.name + ".log")).read_text().splitlines()
    ]


def results(script: Path) -> list[list[dict[str, Any]]]:
    return [
        json.loads(x) for x in (script.parent / (script.name + ".results")).read_text().splitlines()
    ]


def claude(command: str, script: Path, **extra: Any) -> ClaudeCodeRunner:
    return ClaudeCodeRunner(
        ProviderConfig(
            type="claude_code", command=command, env={"FAKE_CLI_SCRIPT": str(script)}, **extra
        ),
        RoleModel(provider="cc", model="sonnet", effort="high"),
        retry=RetryPolicy(attempts=3, sleep=no_sleep),
    )


async def run(
    runner: Any,
    tmp_path: Path,
    log: list[str] | None = None,
    on_finish: Any = accept,
    max_turns: int = 80,
) -> Any:
    return await runner.run(
        system="You are a test agent.",
        prompt="Echo hi, then finish with 42.",
        toolbox=echo_box(log if log is not None else []),
        result_model=Result,
        finish_description="Submit the answer.",
        on_finish=on_finish,
        max_turns=max_turns,
        cwd=tmp_path,
    )


# -- the MCP bridge ----------------------------------------------------------------------------


async def test_bridge_speaks_mcp() -> None:
    async def handler(name: str, args: dict[str, Any]) -> tuple[str, bool]:
        return (f"{name}:{args}", name == "bad")

    spec = ToolSpec(name="echo", description="d", input_schema={"type": "object"})
    async with McpBridge([spec], handler) as bridge:
        init = await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2099-01-01"},
            }
        )
        assert init is not None and init["result"]["protocolVersion"] == "2099-01-01"
        assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert (
            await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        )
        call = await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "bad", "arguments": {"x": 1}},
            }
        )
        assert call is not None and call["result"]["isError"] is True
        unknown = await bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        assert unknown is not None and unknown["error"]["code"] == -32601

        async with httpx.AsyncClient() as http:
            r = await http.post(
                bridge.url, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
            )
            assert r.json()["result"]["tools"][0]["name"] == "echo"
            assert "mcp-session-id" in r.headers
            r = await http.post(bridge.url, json={"jsonrpc": "2.0", "method": "notifications/x"})
            assert r.status_code == 202
            assert (await http.get(bridge.url)).status_code == 405
            wrong = bridge.url.rsplit("/", 1)[0] + "/not-the-token"
            assert (await http.post(wrong, json={"id": 5, "method": "ping"})).status_code == 404
            assert bridge.url.startswith("http://127.0.0.1:")


# -- Claude Code --------------------------------------------------------------------------------


async def test_claude_code_run(tmp_path: Path, fake_cli: Any) -> None:
    command, script = fake_cli(
        [
            [
                {"call": "echo", "args": {"text": "hi"}},
                {"call": "finish", "args": {"answer": "not a number"}},
                {"call": "finish", "args": {"answer": 42}},
            ]
        ]
    )
    log: list[str] = []
    r = await run(claude(command, script), tmp_path, log)

    assert r.outcome == "finished" and r.result == Result(answer=42) and log == ["hi"]
    assert r.cost_usd == 0.01 and r.usage.input_tokens == 100
    assert r.usage.cache_read_input_tokens == 50 and r.turns == 3
    (call_results,) = results(script)
    assert [c["isError"] for c in call_results] == [False, True, False]
    assert "invalid finish input" in call_results[1]["content"][0]["text"]
    (inv,) = logged(script)
    argv = inv["argv"]
    assert argv[argv.index("--tools") + 1] == ""  # every built-in tool disabled
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == "mcp__harness"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "high"
    assert "mcp__harness__finish" in argv[argv.index("--system-prompt") + 1]
    assert inv["stdin"] == "Echo hi, then finish with 42."  # long prompts go via stdin
    # The transcript records each tool call and result for the dashboard.
    assert [b.type for m in r.messages for b in m.content][:2] == ["tool_use", "tool_result"]


async def test_rejected_finish_then_accepted(tmp_path: Path, fake_cli: Any) -> None:
    seen: list[int] = []

    async def picky(res: Result) -> str | None:
        seen.append(res.answer)
        return None if res.answer == 42 else "gates failed: try again"

    command, script = fake_cli(
        [
            [
                {"call": "finish", "args": {"answer": 1}},
                {"call": "finish", "args": {"answer": 42}},
            ]
        ]
    )
    r = await run(claude(command, script), tmp_path, on_finish=picky)
    assert r.outcome == "finished" and seen == [1, 42]
    assert results(script)[0][0]["content"][0]["text"] == "gates failed: try again"


async def test_nudge_resumes_the_session(tmp_path: Path, fake_cli: Any) -> None:
    command, script = fake_cli(
        [
            [{"call": "echo", "args": {"text": "working"}}],  # stops without finish
            [{"call": "finish", "args": {"answer": 42}}],
        ]
    )
    r = await run(claude(command, script), tmp_path)
    assert r.outcome == "finished"
    first, second = logged(script)
    assert "--resume" not in first["argv"]
    assert second["argv"][second["argv"].index("--resume") + 1] == "sess-1"
    assert second["stdin"] == NUDGE
    assert r.cost_usd == pytest.approx(0.02)  # both invocations counted


async def test_no_finish_after_nudges(tmp_path: Path, fake_cli: Any) -> None:
    command, script = fake_cli([[], []])
    r = await run(claude(command, script, nudges=1), tmp_path)
    assert r.outcome == "no_finish" and len(logged(script)) == 2


async def test_abort_and_tool_limit(tmp_path: Path, fake_cli: Any) -> None:
    async def abort(_: Result) -> str | None:
        raise AgentAbort("gates keep failing")

    command, script = fake_cli([[{"call": "finish", "args": {"answer": 1}}]])
    r = await run(claude(command, script), tmp_path, on_finish=abort)
    assert r.outcome == "aborted" and r.reason == "gates keep failing"

    command, script = fake_cli([[{"call": "echo", "args": {"text": str(i)}} for i in range(4)]])
    r = await run(claude(command, script), tmp_path, max_turns=2)
    assert r.outcome == "max_turns"
    assert [c["isError"] for c in results(script)[0]] == [False, False, True, True]


async def test_cli_failures(tmp_path: Path, fake_cli: Any) -> None:
    missing = claude(str(tmp_path / "no-such-cli"), tmp_path / "x.json")
    r = await run(missing, tmp_path)
    assert r.outcome == "model_error" and "was not found" in r.reason

    command, script = fake_cli([[{"exit": 1}], [{"exit": 1}], [{"exit": 1}]])
    r = await run(claude(command, script), tmp_path)
    assert r.outcome == "model_error" and "exited with 1" in r.reason
    assert len(logged(script)) == 3  # retried up to the policy's attempts


# -- OpenCode ---------------------------------------------------------------------------------


async def test_opencode_run(tmp_path: Path, fake_cli: Any) -> None:
    command, script = fake_cli(
        [
            [
                {"call": "echo", "args": {"text": "hi"}},
                {"call": "finish", "args": {"answer": 42}},
            ]
        ]
    )
    runner = OpenCodeRunner(
        ProviderConfig(type="opencode", command=command, env={"FAKE_CLI_SCRIPT": str(script)}),
        RoleModel(provider="oc", model="anthropic/claude-sonnet-5-5"),
    )
    r = await run(runner, tmp_path)
    assert r.outcome == "finished" and r.result == Result(answer=42)
    assert r.cost_usd == pytest.approx(0.002)
    assert r.usage.output_tokens == 6 and r.usage.cache_creation_input_tokens == 2
    argv = logged(script)[0]["argv"]
    assert argv[:3] == ["run", "--format", "json"]
    assert argv[argv.index("--agent") + 1] == "harness" and "--auto" in argv
    assert argv[argv.index("--dir") + 1] == str(tmp_path)
    assert argv[-1] == "Echo hi, then finish with 42."

    env_cfg = json.loads(
        runner.command_line(
            system=runner._system("S"), prompt="p", url="http://x", cwd=tmp_path, session_id="ses_1"
        )[1]["OPENCODE_CONFIG_CONTENT"]
    )
    tools = env_cfg["agent"]["harness"]["tools"]
    assert tools["bash"] is False and tools["edit"] is False and tools["harness_*"] is True
    assert "harness_finish" in env_cfg["agent"]["harness"]["prompt"]


# -- choosing a runner ----------------------------------------------------------------------------


def test_runner_for_role() -> None:
    cfg = HarnessConfig.model_validate(
        {
            "providers": {
                "cc": {"type": "claude_code"},
                "oc": {"type": "opencode"},
                "f": {"type": "fake"},
            },
            "models": {
                "default": {"provider": "cc", "model": "opus"},
                "roles": {
                    "implementer": {"provider": "oc", "model": "a/b"},
                    "librarian": {"provider": "f", "model": "x"},
                },
            },
        }
    )
    assert isinstance(runner_for_role(cfg, "reviewer"), ClaudeCodeRunner)
    assert isinstance(runner_for_role(cfg, "implementer"), OpenCodeRunner)
    assert isinstance(runner_for_role(cfg, "librarian"), ApiRunner)
    assert isinstance(
        runner_for_role(cfg, "reviewer", client_factory=lambda r: FakeModelClient()), ApiRunner
    )


async def test_build_loop_on_claude_code(tmp_path: Path, fake_cli: Any) -> None:
    command, script = fake_cli(
        [
            [  # implementer
                {"call": "read_file", "args": {"path": "docs/spec.md"}},
                {"call": "write_file", "args": {"path": "outside.py", "content": "x"}},  # refused
                {"call": "write_file", "args": {"path": "x.py", "content": "X = 1\n"}},
                {"call": "finish", "args": {"summary": "added X"}},
            ],
            [  # reviewer
                {"call": "read_file", "args": {"path": "x.py"}},
                {"call": "finish", "args": {"verdict": "approve", "summary": "fine"}},
            ],
        ]
    )
    config = (
        "providers:\n"
        f"  cc: {{type: claude_code, command: '{command}', env: {{FAKE_CLI_SCRIPT: '{script}'}}}}\n"
        "models: {default: {provider: cc, model: sonnet}}\n"
        "stacks: [python]\n"
    )
    project = make_app(tmp_path, config=config)
    project.board.add_tickets([ticket("T-1", owned_files=["x.py"])])
    project.board.queue("T-1")

    await Orchestrator(project, poll_interval_s=0.01).run(timeout_s=120)

    assert project.board.get("T-1").status is TicketStatus.DONE
    assert (project.root / "x.py").read_text() == "X = 1\n"
    impl_results, _ = results(script)
    assert (
        impl_results[1]["isError"] and "not owned by T-1" in impl_results[1]["content"][0]["text"]
    )
    implementer_cwd = logged(script)[0]
    assert "## Ticket" in implementer_cwd["stdin"]  # the context pack is the prompt
    report = usage_report(project.board, project.config)
    assert report.total.runs == 2 and report.total.cost_usd == pytest.approx(0.02)


def test_doctor(tmp_path: Path, fake_cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    from harness.cli import main

    command, script = fake_cli(
        [
            [
                {"call": "add", "args": {"a": 1234, "b": 4321}},
                {"call": "finish", "args": {"answer": 5555}},
            ]
        ]
    )
    config = (
        "providers:\n"
        f"  cc: {{type: claude_code, command: '{command}', env: {{FAKE_CLI_SCRIPT: '{script}'}}}}\n"
        "  missing: {type: opencode, command: no-such-opencode}\n"
        "models: {default: {provider: cc, model: sonnet}}\n"
    )
    project = make_app(tmp_path, config=config)
    assert main(["-C", str(project.root), "doctor", "--live"]) == 0
    out = capsys.readouterr().out
    assert "implementer  cc:sonnet" in out
    assert "live cc:sonnet" in out and "✓ finished in 2 tool calls, $0.0100" in out

    (project.harness_dir / "config.yaml").write_text(
        config.replace("default: {provider: cc", "default: {provider: missing")
    )
    assert main(["-C", str(project.root), "doctor"]) == 1
    assert "`no-such-opencode` not found on PATH" in capsys.readouterr().out

from __future__ import annotations

from pydantic import BaseModel

from harness.agents.loop import AgentAbort, RetryPolicy, run_agent
from harness.models import (
    FakeModelClient,
    ModelError,
    ModelResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from harness.tools.base import Tool, Toolbox
from tests.helpers import call, finish, say


class Result(BaseModel):
    answer: int


class EchoIn(BaseModel):
    text: str


def toolbox(log: list[str]) -> Toolbox:
    async def echo(args: EchoIn) -> str:
        log.append(args.text)
        return args.text

    return Toolbox([Tool("echo", "echo", EchoIn, echo)])


async def accept(_: Result) -> str | None:
    return None


async def no_sleep(_: float) -> None:
    return None


async def run(client: FakeModelClient, log: list[str] | None = None, on_finish=accept, **kw):  # type: ignore[no-untyped-def]
    return await run_agent(
        client,
        system="sys",
        prompt="go",
        toolbox=toolbox(log if log is not None else []),
        result_model=Result,
        finish_description="done",
        on_finish=on_finish,
        retry=RetryPolicy(sleep=no_sleep),
        **kw,
    )


async def test_tools_then_finish() -> None:
    log: list[str] = []
    client = FakeModelClient([call("echo", text="hi"), finish(answer=42)])
    r = await run(client, log)
    assert r.outcome == "finished" and r.result == Result(answer=42) and log == ["hi"]
    assert {t.name for t in client.calls[0].tools or []} == {"echo", "finish"}
    # History is append-only: each call extends the previous call's messages.
    assert client.calls[1].messages[:1] == client.calls[0].messages[:1]


async def test_finish_rejection_then_acceptance() -> None:
    seen = []

    async def picky(res: Result) -> str | None:
        seen.append(res.answer)
        return None if res.answer == 2 else "wrong, try again"

    client = FakeModelClient([finish(answer=1), finish(answe=2), finish(answer=2)])
    r = await run(client, on_finish=picky)
    assert r.outcome == "finished" and seen == [1, 2]
    errors = [
        b.content
        for m in client.calls[-1].messages
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.is_error
    ]
    assert errors[0] == "wrong, try again" and "invalid finish input" in errors[1]


async def test_abort_from_on_finish() -> None:
    async def abort(_: Result) -> str | None:
        raise AgentAbort("gates keep failing")

    r = await run(FakeModelClient([finish(answer=1)]), on_finish=abort)
    assert r.outcome == "aborted" and r.reason == "gates keep failing"


async def test_truncated_tool_call_is_not_executed() -> None:
    log: list[str] = []
    truncated = ModelResponse(
        content=[ToolUseBlock(id="t1", name="echo", input={"text": "partial"})],
        stop_reason="max_tokens",
    )
    r = await run(FakeModelClient([truncated, finish(answer=1)]), log)
    assert r.outcome == "finished" and log == []


async def test_refusal_and_nudges() -> None:
    refusal = ModelResponse(content=[TextBlock(text="")], stop_reason="refusal")
    assert (await run(FakeModelClient([refusal]))).outcome == "refusal"
    r = await run(FakeModelClient([say("hmm")] * 4))
    assert r.outcome == "no_finish"
    r = await run(FakeModelClient([say("hmm"), finish(answer=3)]))
    assert r.outcome == "finished"


async def test_retries_retryable_errors_only() -> None:
    attempts = []

    def flaky(call):  # type: ignore[no-untyped-def]
        attempts.append(1)
        raise ModelError("429", retryable=True)

    client = FakeModelClient([flaky, flaky, finish(answer=1)])
    assert (await run(client)).outcome == "finished"

    def fatal(call):  # type: ignore[no-untyped-def]
        raise ModelError("400 bad request")

    r = await run(FakeModelClient([fatal]))
    assert r.outcome == "model_error" and "400" in r.reason


async def test_max_turns() -> None:
    r = await run(FakeModelClient([call("echo", text=str(i)) for i in range(5)]), max_turns=3)
    assert r.outcome == "max_turns" and r.turns == 3

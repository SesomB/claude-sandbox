"""The agent tool-use loop: call the model, run its tools, repeat until it calls `finish`.

Each agent ends by calling a role-specific `finish` tool whose input is the role's
structured result. The caller's ``on_finish`` validates it (e.g. runs the gates);
a rejection goes back to the model as a tool error and the loop continues.
The message history is append-only, which keeps provider prompt caches valid.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from harness.models.base import (
    ContentBlock,
    Message,
    ModelClient,
    ModelError,
    ModelResponse,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    Usage,
)
from harness.tools.base import Toolbox

R = TypeVar("R", bound=BaseModel)

FINISH = "finish"


class AgentAbort(Exception):
    """Raised by ``on_finish`` (or a hook) to stop the agent for good."""


@dataclass
class RetryPolicy:
    attempts: int = 8
    base_delay_s: float = 2.0
    max_delay_s: float = 120.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


Outcome = Literal["finished", "aborted", "max_turns", "refusal", "no_finish", "model_error"]


@dataclass
class AgentRun(Generic[R]):
    outcome: Outcome
    result: R | None = None
    reason: str = ""
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    messages: list[Message] = field(default_factory=list)


OnFinish = Callable[[R], Awaitable[str | None]]


async def _complete_with_retry(
    client: ModelClient,
    messages: list[Message],
    *,
    system: str,
    tools: list[ToolSpec],
    max_tokens: int,
    retry: RetryPolicy,
) -> ModelResponse:
    for attempt in range(retry.attempts):
        try:
            return await client.complete(
                messages, system=system, tools=tools, max_tokens=max_tokens
            )
        except ModelError as e:
            if not e.retryable or attempt == retry.attempts - 1:
                raise
            delay = min(retry.base_delay_s * 2**attempt, retry.max_delay_s)
            await retry.sleep(delay + random.uniform(0, delay / 4))
    raise AssertionError("unreachable")


async def run_agent(
    client: ModelClient,
    *,
    system: str,
    prompt: str,
    toolbox: Toolbox,
    result_model: type[R],
    finish_description: str,
    on_finish: OnFinish[R],
    max_turns: int = 80,
    max_tokens: int = 32_000,
    retry: RetryPolicy | None = None,
    on_turn: Callable[[], Awaitable[None]] | None = None,
) -> AgentRun[R]:
    retry = retry or RetryPolicy()
    finish_schema = result_model.model_json_schema()
    finish_schema.pop("title", None)
    tools = [
        *toolbox.specs(),
        ToolSpec(name=FINISH, description=finish_description, input_schema=finish_schema),
    ]
    messages: list[Message] = [Message.user(prompt)]
    run: AgentRun[R] = AgentRun(outcome="max_turns", messages=messages)
    nudges = 0

    for turn in range(1, max_turns + 1):
        run.turns = turn
        if on_turn is not None:
            await on_turn()
        try:
            resp = await _complete_with_retry(
                client, messages, system=system, tools=tools, max_tokens=max_tokens, retry=retry
            )
        except ModelError as e:
            run.outcome, run.reason = "model_error", str(e)
            return run
        run.usage = run.usage + resp.usage
        messages.append(Message(role="assistant", content=list(resp.content)))

        if resp.stop_reason == "refusal":
            run.outcome, run.reason = "refusal", "the model declined this request"
            return run

        uses = resp.tool_uses
        if resp.stop_reason == "max_tokens":
            # A tool input cut off mid-stream may still parse; never run it.
            note = (
                "Your output hit the token limit and was cut off, so no tool ran. "
                "Work in smaller steps (e.g. write large files in parts with edit_file)."
            )
            messages.append(_results_or_text(uses, note))
            continue

        if not uses:
            nudges += 1
            if nudges > 3:
                run.outcome, run.reason = "no_finish", "the agent stopped without calling finish"
                return run
            messages.append(
                Message.user(f"Continue with the tools. When you are done, call `{FINISH}`.")
            )
            continue

        results: list[ContentBlock] = []
        finish_uses = [u for u in uses if u.name == FINISH]
        for use in uses:
            if use.name != FINISH:
                results.append(await toolbox.call(use))
        for use in finish_uses:
            try:
                parsed = result_model.model_validate(use.input)
            except ValidationError as e:
                results.append(_err(use.id, f"invalid finish input: {e}"))
                continue
            try:
                rejection = await on_finish(parsed)
            except AgentAbort as e:
                run.outcome, run.reason = "aborted", str(e)
                return run
            if rejection is None:
                run.outcome, run.result = "finished", parsed
                return run
            results.append(_err(use.id, rejection))
        messages.append(Message(role="user", content=results))

    run.reason = f"no finish after {max_turns} turns"
    return run


def _err(tool_use_id: str, text: str) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=tool_use_id, content=text, is_error=True)


def _results_or_text(uses: list, note: str) -> Message:  # type: ignore[type-arg]
    if uses:
        return Message(role="user", content=[_err(u.id, note) for u in uses])
    return Message(role="user", content=[TextBlock(text=note)])

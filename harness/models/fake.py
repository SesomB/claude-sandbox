"""A scripted ModelClient for offline tests: replays canned responses in order."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from harness.models.base import (
    Message,
    ModelError,
    ModelResponse,
    TextBlock,
    ToolSpec,
    ToolUseBlock,
)


@dataclass
class RecordedCall:
    messages: list[Message]
    system: str | None
    tools: list[ToolSpec] | None
    max_tokens: int


Responder = Callable[[RecordedCall], ModelResponse]


def text_response(text: str) -> ModelResponse:
    return ModelResponse(content=[TextBlock(text=text)], stop_reason="end_turn", model="fake")


def tool_response(name: str, input: dict[str, object], id: str = "tu_1") -> ModelResponse:
    return ModelResponse(
        content=[ToolUseBlock(id=id, name=name, input=dict(input))],
        stop_reason="tool_use",
        model="fake",
    )


class FakeModelClient:
    """Returns each scripted response (or calls each responder) once, in order."""

    def __init__(self, script: Sequence[ModelResponse | Responder] = ()) -> None:
        self._script = list(script)
        self.calls: list[RecordedCall] = []

    def push(self, item: ModelResponse | Responder) -> None:
        self._script.append(item)

    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 8192,
    ) -> ModelResponse:
        call = RecordedCall(list(messages), system, tools, max_tokens)
        self.calls.append(call)
        if not self._script:
            raise ModelError("FakeModelClient script exhausted")
        item = self._script.pop(0)
        return item if isinstance(item, ModelResponse) else item(call)

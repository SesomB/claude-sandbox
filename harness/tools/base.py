"""Typed tools: a pydantic input model, a description, and an async handler.

The Toolbox validates every tool input against its model before the handler runs,
so malformed or truncated inputs from the model never reach the filesystem.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from harness.models.base import ToolResultBlock, ToolSpec, ToolUseBlock

MAX_RESULT_CHARS = 30_000


class ToolError(Exception):
    """An expected failure, reported to the model as an error tool result."""


Handler = Callable[[Any], Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler

    def spec(self) -> ToolSpec:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, input_schema=schema)


def tail(text: str, limit: int = 6000) -> str:
    """The end of ``text`` (where test failures and compiler errors usually are)."""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} earlier chars omitted ...]\n{text[-limit:]}"


class Toolbox:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.add(t)

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name!r}")
        self._tools[tool.name] = tool

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    async def call(self, use: ToolUseBlock) -> ToolResultBlock:
        tool = self._tools.get(use.name)
        if tool is None:
            return _error(use, f"unknown tool {use.name!r}; available: {', '.join(self._tools)}")
        try:
            args = tool.input_model.model_validate(use.input)
        except ValidationError as e:
            return _error(use, f"invalid input for {use.name}: {e}")
        try:
            content = await tool.handler(args)
        except ToolError as e:
            return _error(use, str(e))
        except Exception as e:  # a bug in a tool must not kill the agent
            return _error(use, f"internal error in {use.name}: {type(e).__name__}: {e}")
        return ToolResultBlock(tool_use_id=use.id, content=_clip(content))


def _clip(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n[... truncated {len(text) - MAX_RESULT_CHARS} chars ...]"


def _error(use: ToolUseBlock, message: str) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=use.id, content=_clip(message), is_error=True)

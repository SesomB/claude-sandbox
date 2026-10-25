"""Anthropic (Claude) adapter for the provider-neutral ModelClient protocol.

Provider config keys (``providers.<name>`` with ``type: anthropic``):
  api_key_env   env var holding the API key (default: SDK credential resolution)
  base_url      optional API base URL
  fallbacks     server-side refusal fallback; ``"default"`` (the default) or ``null`` to disable
  max_retries   SDK-level retries per request (default 2); the agent loop retries further
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

from harness.config import ProviderConfig, RoleModel
from harness.models.base import (
    ContentBlock,
    Message,
    ModelError,
    ModelResponse,
    OpaqueBlock,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

PROVIDER = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "refusal": "refusal",
}


def _block_to_api(block: ContentBlock) -> dict[str, Any] | None:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    # Opaque blocks go back exactly as received; another provider's are dropped.
    return block.data if block.provider == PROVIDER else None


def messages_to_api(messages: list[Message]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        content = [b for b in (_block_to_api(x) for x in m.content) if b is not None]
        if content:
            out.append({"role": m.role, "content": content})
    return out


def response_from_api(msg: Any) -> ModelResponse:
    """Convert an SDK ``Message``/``BetaMessage`` into a ModelResponse."""
    content: list[ContentBlock] = []
    for block in msg.content:
        if block.type == "text":
            content.append(TextBlock(text=block.text))
        elif block.type == "tool_use":
            content.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input)))
        else:  # thinking, redacted_thinking, fallback, ... - must be passed back unchanged
            content.append(OpaqueBlock(provider=PROVIDER, data=block.to_dict()))
    u = msg.usage
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
    )
    return ModelResponse(
        content=content,
        stop_reason=_STOP_REASONS.get(msg.stop_reason or "", "other"),
        usage=usage,
        model=msg.model,
    )


class AnthropicClient:
    def __init__(
        self,
        provider: ProviderConfig,
        role_model: RoleModel,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.role_model = role_model
        extra = provider.model_extra or {}
        fallbacks = extra.get("fallbacks", "default")
        # Haiku models have no server-side fallback; sending the parameter there is wrong.
        self.fallbacks = None if "haiku" in role_model.model else fallbacks
        if client is None:
            kwargs: dict[str, Any] = {"max_retries": int(extra.get("max_retries", 2))}
            if provider.api_key_env:
                kwargs["api_key"] = os.environ.get(provider.api_key_env)
            if provider.base_url:
                kwargs["base_url"] = provider.base_url
            client = anthropic.AsyncAnthropic(**kwargs)
        self._client = client

    def build_params(
        self,
        messages: list[Message],
        *,
        system: str | None,
        tools: list[ToolSpec] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.role_model.model,
            "max_tokens": max_tokens,
            "messages": messages_to_api(messages),
            # Agent loops are append-only, so caching the growing prefix pays off every turn.
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    # Large file bodies stream as generated. Inputs are validated by the
                    # harness's tool layer (pydantic) before any tool runs.
                    "eager_input_streaming": True,
                }
                for t in tools
            ]
        if self.role_model.effort:
            params["output_config"] = {"effort": self.role_model.effort}
        if self.fallbacks:
            params["fallbacks"] = self.fallbacks
            params["betas"] = [FALLBACK_BETA]
        return params

    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 32_000,
    ) -> ModelResponse:
        params = self.build_params(messages, system=system, tools=tools, max_tokens=max_tokens)
        try:
            async with self._client.beta.messages.stream(**params) as stream:
                msg = await stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise ModelError(f"rate limited: {e.message}", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise ModelError(
                f"API error {e.status_code}: {e.message}", retryable=e.status_code >= 500
            ) from e
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise ModelError(f"connection error: {e}", retryable=True) from e
        except ValueError as e:
            # The stream's tool-input JSON could not be parsed at all; there is no
            # tool_use id to answer, so the request is re-issued.
            raise ModelError(f"unparseable tool input: {e}", retryable=True) from e
        return response_from_api(msg)

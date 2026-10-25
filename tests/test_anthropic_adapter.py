"""The Anthropic adapter, against a stand-in for the SDK client (no network)."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2 as httpx
import pytest
from anthropic.types.beta import BetaMessage

from harness.config import ProviderConfig, RoleModel
from harness.models import (
    Message,
    ModelError,
    OpaqueBlock,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from harness.models.anthropic import FALLBACK_BETA, AnthropicClient

RAW = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7},
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "text", "text": "Reading the file."},
        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}},
    ],
}


class FakeStream:
    def __init__(self, sdk: FakeSDK) -> None:
        self.sdk = sdk

    async def __aenter__(self) -> FakeStream:
        if self.sdk.error is not None:
            raise self.sdk.error
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> BetaMessage:
        return BetaMessage.model_validate(self.sdk.response)


class FakeSDK:
    def __init__(self, response: dict[str, Any] = RAW, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.params: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    def stream(self, **params: Any) -> FakeStream:
        self.params.append(params)
        return FakeStream(self)


def client(sdk: FakeSDK, model: str = "claude-opus-5-5", **provider: Any) -> AnthropicClient:
    return AnthropicClient(
        ProviderConfig(type="anthropic", **provider),
        RoleModel(provider="anthropic", model=model, effort="high"),
        client=sdk,  # type: ignore[arg-type]
    )


async def test_request_and_response_mapping() -> None:
    sdk = FakeSDK()
    tool = ToolSpec(name="read_file", description="read", input_schema={"type": "object"})
    history = [
        Message.user("go"),
        Message(
            role="assistant",
            content=[
                OpaqueBlock(
                    provider="anthropic",
                    data={"type": "thinking", "thinking": "", "signature": "s0"},
                ),
                OpaqueBlock(provider="other", data={"type": "whatever"}),
                ToolUseBlock(id="toolu_0", name="read_file", input={"path": "b.py"}),
            ],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="toolu_0", content="x")]),
    ]
    resp = await client(sdk).complete(history, system="sys", tools=[tool], max_tokens=1000)

    p = sdk.params[0]
    assert p["model"] == "claude-opus-5-5" and p["max_tokens"] == 1000 and p["system"] == "sys"
    assert p["output_config"] == {"effort": "high"}
    assert p["fallbacks"] == "default" and p["betas"] == [FALLBACK_BETA]
    assert p["cache_control"] == {"type": "ephemeral"}
    assert p["tools"][0]["eager_input_streaming"] is True
    assert "temperature" not in p
    assistant = p["messages"][1]["content"]
    # Our own thinking block goes back unchanged; another provider's is dropped.
    assert assistant[0] == {"type": "thinking", "thinking": "", "signature": "s0"}
    assert [b["type"] for b in assistant] == ["thinking", "tool_use"]
    assert p["messages"][2]["content"][0]["type"] == "tool_result"

    assert resp.stop_reason == "tool_use"
    assert isinstance(resp.content[0], OpaqueBlock)
    assert resp.content[0].data["signature"] == "sig-abc"
    assert resp.content[1] == TextBlock(text="Reading the file.")
    assert resp.tool_uses[0].input == {"path": "a.py"}
    assert resp.usage.cache_read_input_tokens == 7


async def test_fallbacks_off_for_haiku_and_when_disabled() -> None:
    sdk = FakeSDK()
    await client(sdk, model="claude-haiku-5-5").complete([Message.user("x")])
    await client(sdk, fallbacks=None).complete([Message.user("x")])
    for p in sdk.params:
        assert "fallbacks" not in p and "betas" not in p


def _status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (_status_error(anthropic.RateLimitError, 429), True),
        (_status_error(anthropic.InternalServerError, 500), True),
        (_status_error(anthropic.BadRequestError, 400), False),
        (ValueError("bad partial json"), True),
    ],
)
async def test_error_mapping(error: Exception, retryable: bool) -> None:
    with pytest.raises(ModelError) as info:
        await client(FakeSDK(error=error)).complete([Message.user("x")])
    assert info.value.retryable is retryable


async def test_refusal_stop_reason() -> None:
    raw = {**RAW, "stop_reason": "refusal", "content": []}
    resp = await client(FakeSDK(response=raw)).complete([Message.user("x")])
    assert resp.stop_reason == "refusal"

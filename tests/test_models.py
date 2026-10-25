from __future__ import annotations

import pytest

from harness.models import FakeModelClient, Message, ModelError, ToolSpec
from harness.models.fake import text_response, tool_response


async def test_fake_replays_script_and_records_calls() -> None:
    tool = ToolSpec(name="read_file", description="read", input_schema={"type": "object"})
    client = FakeModelClient([tool_response("read_file", {"path": "a.py"}), text_response("done")])

    first = await client.complete([Message.user("go")], system="sys", tools=[tool])
    assert first.stop_reason == "tool_use"
    assert first.tool_uses[0].input == {"path": "a.py"}

    second = await client.complete([Message.user("go")])
    assert second.text == "done"
    assert client.calls[0].system == "sys"
    assert client.calls[0].tools == [tool]

    with pytest.raises(ModelError):
        await client.complete([Message.user("again")])


async def test_fake_responder_sees_the_call() -> None:
    client = FakeModelClient([lambda call: text_response(f"{len(call.messages)} msgs")])
    resp = await client.complete([Message.user("a"), Message.assistant("b"), Message.user("c")])
    assert resp.text == "3 msgs"


def test_message_roundtrip() -> None:
    msg = Message.model_validate(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        }
    )
    assert Message.model_validate(msg.model_dump()) == msg

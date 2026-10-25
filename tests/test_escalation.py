from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.board import Board
from harness.escalation.cli import parse_choice, render_request
from harness.escalation.decisions import Decision, DecisionRequest, ask_board, ask_user_tool
from harness.models import ToolUseBlock
from harness.tools.base import Toolbox
from tests.helpers import ScriptedHuman

REQ = {
    "question": "Hash reset tokens?",
    "context": "Tokens are stored in the DB.",
    "options": [
        {"name": "SHA-256", "pros": ["leak-safe"], "cons": ["can't show token again"]},
        {"name": "Plain", "pros": ["simplest"], "cons": ["DB leak = takeover"]},
    ],
    "recommendation": "SHA-256",
    "confidence": "high",
}


def test_requests_must_be_ready_to_decide() -> None:
    DecisionRequest.model_validate(REQ)
    one_option = {**REQ, "options": REQ["options"][:1]}
    no_cons = {**REQ, "options": [{**REQ["options"][0], "cons": []}, REQ["options"][1]]}
    bad_rec = {**REQ, "recommendation": "Bcrypt"}
    for bad in (one_option, no_cons, bad_rec):
        with pytest.raises(ValidationError):
            DecisionRequest.model_validate(bad)


def test_parse_choice_and_render() -> None:
    req = DecisionRequest.model_validate(REQ)
    assert parse_choice(req, "") == Decision(choice="SHA-256")
    assert parse_choice(req, "2 it's internal only") == Decision(
        choice="Plain", note="it's internal only"
    )
    assert parse_choice(req, "plain") == Decision(choice="Plain")
    assert parse_choice(req, "use bcrypt") == Decision(note="use bcrypt")
    text = render_request("D-1", req)
    assert "[1] SHA-256  (recommended)" in text and "- DB leak = takeover" in text


async def test_ask_board_records_the_decision(tmp_path: Path) -> None:
    board = Board(tmp_path / "b.db")
    human = ScriptedHuman(decisions=[Decision(choice="Plain", note="internal tool")])
    decision = await ask_board(board, human, DecisionRequest.model_validate(REQ), phase="spec")
    assert decision.choice == "Plain"
    (d,) = board.decisions()
    assert d["id"] == "D-1" and d["phase"] == "spec" and d["answer"]["note"] == "internal tool"
    assert [e.type for e in board.events()] == ["decision.requested", "decision.answered"]


async def test_ask_user_tool_rejects_bare_questions(tmp_path: Path) -> None:
    board = Board(tmp_path / "b.db")
    human = ScriptedHuman(decisions=[Decision(choice="SHA-256")])
    box = Toolbox([ask_user_tool(board, human, phase="spec")])
    bare = await box.call(ToolUseBlock(id="1", name="ask_user", input={"question": "hash?"}))
    assert bare.is_error and human.requests == []
    ok = await box.call(ToolUseBlock(id="2", name="ask_user", input=REQ))
    assert not ok.is_error and ok.content == "The Board chose: SHA-256."


async def test_cli_human(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from harness.escalation.cli import CliHuman

    answers = iter(["2 cheaper", "n", "add rate limits", "please add logging", "y"])
    human = CliHuman()

    async def fake_input(prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr(human, "_input", fake_input)
    d = await human.decide("D-1", DecisionRequest.model_validate(REQ))
    assert d == Decision(choice="Plain", note="cheaper")
    assert "Hash reset tokens?" in capsys.readouterr().out
    a1 = await human.approve("A-1", "spec", "summary")
    assert not a1.approved and a1.feedback == "add rate limits"
    a2 = await human.approve("A-1", "spec", "summary")
    assert not a2.approved and a2.feedback == "please add logging"
    assert (await human.approve("A-1", "spec", "summary")).approved

"""The board as the channel to the human: BoardHuman waits, any process can answer."""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness.board import Board
from harness.cli import main
from harness.escalation.decisions import (
    BoardHuman,
    DecisionRequest,
    ask_board,
    request_approval,
)
from harness.project import init_project
from tests.test_escalation import REQ


async def test_board_human_waits_for_answers_from_elsewhere(tmp_path: Path) -> None:
    board = Board(tmp_path / "b.db")
    said: list[str] = []
    human = BoardHuman(board, poll_s=0.01, announce=said.append)

    async def answer_later() -> None:
        while not board.decisions(open_only=True):
            await asyncio.sleep(0.01)
        other = Board(tmp_path / "b.db")  # e.g. the dashboard process
        other.answer_decision(
            other.decisions(open_only=True)[0]["id"], {"choice": "Plain", "note": ""}
        )
        while not board.approvals(open_only=True):
            await asyncio.sleep(0.01)
        other.resolve_approval(other.approvals(open_only=True)[0]["id"], False, "tighten AC-2")

    task = asyncio.create_task(answer_later())
    decision = await ask_board(board, human, DecisionRequest.model_validate(REQ))
    approval = await request_approval(board, human, "spec", "summary")
    await task
    assert decision.choice == "Plain"
    assert not approval.approved and approval.feedback == "tighten AC-2"
    assert "harness answer D-1" in said[0] and "harness approve A-1" in said[1]


def test_cli_answer_and_approve(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project = init_project(tmp_path / "app", user_config=tmp_path / "none.yaml")
    did = project.board.create_decision(REQ)
    aid = project.board.create_approval("plan", "3 tickets")
    root = str(project.root)
    assert main(["-C", root, "answer", did, "2", "it's", "internal"]) == 0
    assert project.board.get_decision(did)["answer"] == {"choice": "Plain", "note": "it's internal"}
    assert main(["-C", root, "answer", did, "1"]) == 1  # already answered
    assert main(["-C", root, "approve", aid, "--reject", "split T-2"]) == 0
    assert project.board.get_approval(aid)["feedback"] == "split T-2"
    assert main(["-C", root, "usage"]) == 0
    assert "total cost: $0.000 over 0 agent runs" in capsys.readouterr().out

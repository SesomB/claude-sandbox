"""Escalated tickets get a researched decision; the owner's answer moves the ticket."""

from __future__ import annotations

from pathlib import Path

from harness.board import TicketStatus
from harness.config import HarnessConfig
from harness.core.usage import run_cost
from harness.escalation.researcher import chosen_action
from tests.helpers import ScriptedAgents, call, finish, make_app, say, ticket
from tests.test_build_loop import orchestrator

S = TicketStatus

RESEARCHED = {
    "question": "T-1 keeps stopping without finishing. How should we proceed?",
    "context": "The implementer never called finish.",
    "options": [
        {
            "name": "Retry with a smaller scope",
            "action": "retry",
            "description": "Only create x.py with X = 1.",
            "pros": ["simple"],
            "cons": ["slower"],
        },
        {"name": "Cancel", "action": "cancel", "pros": ["unblocks"], "cons": ["no feature"]},
    ],
    "recommendation": "Retry with a smaller scope",
    "confidence": "medium",
}


async def test_escalation_research_answer_and_retry(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    project.board.add_tickets([ticket("T-1", owned_files=["x.py"])])
    project.board.queue("T-1")
    agents = ScriptedAgents()
    agents.add("implementer", "T-1", *[say("hmm") for _ in range(4)])
    agents.add(
        "researcher",
        "T-1",
        call("list_files"),
        finish(
            **{
                **RESEARCHED,
                "options": [{**RESEARCHED["options"][0], "action": None}, RESEARCHED["options"][1]],
            }
        ),  # rejected: no action
        finish(**RESEARCHED),
    )
    await orchestrator(project, agents).run(timeout_s=60)

    assert project.board.get("T-1").status is S.ESCALATED
    (decision,) = project.board.decisions(open_only=True)
    assert decision["ticket_id"] == "T-1" and decision["request"]["question"].startswith("T-1")

    # The owner answers (e.g. on the dashboard); the next run applies it.
    project.board.answer_decision(
        decision["id"], {"choice": "Retry with a smaller scope", "note": "Keep it to one line."}
    )
    agents.add(
        "implementer",
        "T-1",
        call("write_file", path="x.py", content="X = 1\n"),
        finish(summary="x"),
    )
    agents.add("reviewer", "T-1", finish(verdict="approve", summary="ok"))
    await orchestrator(project, agents).run(timeout_s=60)

    assert project.board.get("T-1").status is S.DONE
    assert agents.unused() == {}
    # The guidance reached the retrying implementer's context.
    retry_prompt = [c for r, t, c in agents.clients if r == "implementer"][1].calls[0].messages[0]
    text = retry_prompt.content[0].text  # type: ignore[union-attr]
    assert "Only create x.py with X = 1." in text and "Keep it to one line." in text


async def test_researcher_failure_falls_back_to_a_generic_decision(tmp_path: Path) -> None:
    project = make_app(tmp_path)
    project.board.add_tickets([ticket("T-1", owned_files=["x.py"])])
    project.board.queue("T-1")
    agents = ScriptedAgents()
    agents.add("implementer", "T-1", *[say("hmm") for _ in range(4)])  # no researcher script
    await orchestrator(project, agents).run(timeout_s=60)
    (decision,) = project.board.decisions(open_only=True)
    names = [o["name"] for o in decision["request"]["options"]]
    assert names == [
        "Retry with guidance",
        "Send to a fixer",
        "Handle it manually",
        "Cancel the ticket",
    ]

    project.board.answer_decision(decision["id"], {"choice": "Cancel the ticket", "note": ""})
    await orchestrator(project, agents).run(timeout_s=30)
    assert project.board.get("T-1").status is S.CANCELLED


def test_free_text_answers_mean_retry_with_that_guidance() -> None:
    d = {"request": RESEARCHED, "answer": {"choice": None, "note": "use a dict, not a list"}}
    assert chosen_action(d) == ("retry", "use a dict, not a list")
    d = {"request": RESEARCHED, "answer": {"choice": "Cancel", "note": ""}}
    assert chosen_action(d) == ("cancel", "")


def test_run_cost() -> None:
    cfg = HarnessConfig.model_validate({"pricing": {"my-local": {"input": 0, "output": 0}}})
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }
    assert run_cost(cfg, "claude-opus-5-5", usage) == 4 + 20 + 0.2 + 5
    assert run_cost(cfg, "claude-haiku-5-5", usage) == 0.1 + 0.5 + 0.01 + 0.125
    assert run_cost(cfg, "my-local", usage) == 0
    assert run_cost(cfg, "mystery", usage) is None

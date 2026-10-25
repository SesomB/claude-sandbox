"""Learning: feedback and recurring problems become lessons that reach the right agents."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.agents.loop import RetryPolicy
from harness.board import TicketStatus
from harness.cli import main
from harness.learning.learner import APPLY, PROMOTE_AFTER_HITS, Learner, validate_scope
from harness.project import Project
from harness.web.app import create_app
from tests.helpers import ScriptedAgents, call, finish, make_app, ticket
from tests.test_build_loop import orchestrator


async def no_sleep(_: float) -> None:
    return None


def learner(project: Project, agents: ScriptedAgents) -> Learner:
    return Learner(project, client_factory=agents.factory, retry=RetryPolicy(sleep=no_sleep))


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return make_app(tmp_path)


def test_validate_scope() -> None:
    for ok in ("global", "role:implementer", "path:src/api/**", "stack:cpp-cmake"):
        assert validate_scope(ok) == ok
    for bad in ("everyone", "role:wizard", "path:", "role:Implementer"):
        with pytest.raises(ValueError):
            validate_scope(bad)


def test_lessons_reach_only_relevant_agents(project: Project) -> None:
    b = project.board
    b.add_lesson("G rule", scope="global", source="owner", priority="high")
    b.add_lesson("Impl rule", scope="role:implementer", source="review")
    b.add_lesson("API rule", scope="path:src/api/**", source="review")
    b.add_lesson("Python rule", scope="stack:python", source="lint:E501")
    b.add_lesson("Old rule", scope="global", source="review", status="retired")
    b.add_lesson("Skill rule", scope="role:implementer", source="owner", status="promoted")
    lr = Learner(project)
    api_ticket = ticket("T-1", owned_files=["src/api/routes.py"])
    rules = lr.lessons_for(api_ticket, "implementer")
    assert rules[0] == "G rule"  # the owner's high-priority rules come first
    assert set(rules) == {"G rule", "Impl rule", "API rule", "Python rule"}
    assert lr.lessons_for(ticket("T-2", owned_files=["README.md"]), "reviewer") == ["G rule"]
    assert lr.lessons_for(None, "spec_writer") == ["G rule"]
    assert b.get_lesson("L-1")["uses"] == 3  # counted every time it was injected
    assert "Skill rule" in lr.skills_for("implementer")
    assert lr.skills_for("reviewer") == ""


def test_injection_is_capped(project: Project) -> None:
    for i in range(30):
        project.board.add_lesson(f"rule number {i} " + "x" * 200, scope="global", source="review")
    rules = Learner(project).lessons_for(None, "implementer")
    assert 0 < len(rules) <= 10
    assert sum(len(r) for r in rules) < 1200 * 4


async def test_owner_feedback_becomes_a_lesson(project: Project) -> None:
    aid = project.board.create_approval("spec", "v1")
    project.board.resolve_approval(aid, False, "Never store passwords in plain text.")
    agents = ScriptedAgents()
    agents.add(
        "librarian",
        "-",
        finish(
            add=[
                {
                    "rule": "Hash passwords with a slow KDF (argon2/bcrypt); never store "
                    "them in plain text.",
                    "scope": "global",
                    "observations": [1],
                }
            ]
        ),
    )
    assert await learner(project, agents).sync() == 1
    (lesson,) = project.board.lessons()
    assert lesson["source"] == "owner" and lesson["priority"] == "high"
    assert lesson["rule"].startswith("Hash passwords")
    assert await learner(project, agents).sync() == 0  # each event is learned from once


async def test_owner_feedback_survives_a_librarian_failure(project: Project) -> None:
    did = project.board.create_decision(
        {"question": "Which logger?", "options": [], "recommendation": "x"}, phase="architecture"
    )
    project.board.answer_decision(did, {"choice": None, "note": "Use structured JSON logs."})
    await learner(project, ScriptedAgents()).sync()  # no librarian script: it fails
    (lesson,) = project.board.lessons()
    assert "Use structured JSON logs." in lesson["rule"] and lesson["priority"] == "high"


async def test_recurring_findings_reinforce_and_get_promoted(project: Project) -> None:
    b = project.board
    lesson_id = b.add_lesson(
        "Validate input at the API boundary", scope="role:implementer", source="review"
    )
    b.add_tickets([ticket("T-1", owned_files=["a.py"])])
    agents = ScriptedAgents()
    for n in range(PROMOTE_AFTER_HITS - 1):
        b.log_event(
            "T-1",
            "ticket.changes_requested",
            source="review",
            findings=[
                {"kind": "bug", "severity": "high", "file": "a.py", "detail": f"no validation {n}"}
            ],
        )
        agents.add("librarian", "-", finish(reinforce=[lesson_id]))
        await learner(project, agents).sync()
    lesson = b.get_lesson(lesson_id)
    assert lesson["hits"] == PROMOTE_AFTER_HITS and lesson["status"] == "promoted"
    assert "Validate input" in Learner(project).skills_for("implementer")


async def test_merge_retire_and_unknown_ids(project: Project) -> None:
    b = project.board
    a = b.add_lesson("Use pathlib", scope="global", source="review")
    c = b.add_lesson("Prefer pathlib over os.path", scope="global", source="owner", priority="high")
    stale = b.add_lesson("Use Python 3.8 syntax", scope="global", source="review")
    aid = b.create_approval("plan", "x")
    b.resolve_approval(aid, False, "tickets too big")
    agents = ScriptedAgents()
    agents.add(
        "librarian",
        "-",
        finish(reinforce=["L-99"]),  # rejected: unknown id
        finish(
            merge=[{"ids": [a, c], "rule": "Use pathlib, not os.path", "scope": "global"}],
            retire=[{"id": stale, "reason": "the project targets 3.12"}],
        ),
    )
    await learner(project, agents).sync()
    by_id = {x["id"]: x for x in b.lessons()}
    assert by_id[a]["status"] == by_id[c]["status"] == by_id[stale]["status"] == "retired"
    merged = [x for x in by_id.values() if x["rule"] == "Use pathlib, not os.path"]
    assert merged and merged[0]["priority"] == "high"  # keeps the owner's priority
    assert any(
        e.type == "lesson.updated" and e.payload.get("why", "").startswith("merged")
        for e in b.events()
    )


async def test_spec_conflicts_go_to_the_owner(project: Project) -> None:
    aid = project.board.create_approval("plan", "x")
    project.board.resolve_approval(aid, False, "Division by zero should return None")
    agents = ScriptedAgents()
    agents.add(
        "librarian",
        "-",
        finish(
            add=[
                {
                    "rule": "div returns None on division by zero",
                    "scope": "global",
                    "observations": [1],
                    "conflicts_with_spec": "AC-2 says dividing by zero raises ValueError",
                }
            ]
        ),
    )
    lr = learner(project, agents)
    await lr.sync()
    (lesson,) = project.board.lessons()
    assert lesson["status"] == "pending"
    assert lr.lessons_for(None, "implementer") == []  # not applied while pending
    (decision,) = project.board.decisions(open_only=True)
    assert decision["phase"] == f"lesson:{lesson['id']}"
    project.board.answer_decision(decision["id"], {"choice": APPLY, "note": ""})
    lr.apply_conflict_decision(decision["id"])
    assert project.board.get_lesson(lesson["id"])["status"] == "active"


async def test_repeated_lint_failures_become_a_lesson_without_a_model(project: Project) -> None:
    project.board.add_tickets(
        [ticket("T-1", owned_files=["a.py"]), ticket("T-2", owned_files=["b.py"])]
    )
    stack = project.stacks()[0]
    project.stacks()[0] = stack.model_copy(update={"lint_code_pattern": r"\b([A-Z]\d{3})\b"})
    for tid in ("T-1", "T-1", "T-2"):
        project.board.log_event(
            tid,
            "gates.failed",
            ok=False,
            results=[
                {
                    "name": "python:lint",
                    "ok": False,
                    "detail": "x.py:1:80: E501 Line too long (101 > 100)",
                },
                {
                    "name": "python:lint",
                    "ok": False,
                    "detail": "y.py:3:1: F401 unused import" if tid == "T-1" else "",
                },
            ],
        )
    lr = learner(project, ScriptedAgents())
    await lr.sync()
    await lr.sync()
    (lesson,) = project.board.lessons()  # only E501 failed on two different tickets
    assert lesson["source"] == "lint:E501" and lesson["scope"] == "stack:python"
    assert lesson["examples"] == ["T-1", "T-2"] and "E501 Line too long" in lesson["rule"]


async def test_lessons_and_skills_reach_build_agents(project: Project) -> None:
    project.board.add_lesson(
        "Add type hints to every function.",
        scope="role:implementer",
        source="owner",
        priority="high",
    )
    project.board.add_lesson(
        "Keep functions under 30 lines.", scope="global", source="owner", status="promoted"
    )
    project.board.add_tickets([ticket("T-1", owned_files=["x.py"])])
    project.board.queue("T-1")
    agents = ScriptedAgents()
    agents.add(
        "implementer",
        "T-1",
        call("write_file", path="x.py", content="X: int = 1\n"),
        finish(summary="x"),
    )
    agents.add("reviewer", "T-1", finish(verdict="approve", summary="ok"))
    await orchestrator(project, agents).run(timeout_s=60)
    assert project.board.get("T-1").status is TicketStatus.DONE
    impl = next(c for r, _, c in agents.clients if r == "implementer").calls[0]
    assert "Add type hints to every function." in impl.messages[0].content[0].text  # type: ignore[union-attr]
    assert "Keep functions under 30 lines." in (impl.system or "")
    review = next(c for r, _, c in agents.clients if r == "reviewer").calls[0]
    assert "Add type hints" not in review.messages[0].content[0].text  # type: ignore[union-attr]


def test_lessons_api_and_cli(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    client = TestClient(create_app(project))
    r = client.post("/api/lessons", json={"rule": "Never log secrets", "scope": "global"})
    assert r.status_code == 200 and r.json()["priority"] == "high"
    lid = r.json()["id"]
    assert (
        client.post("/api/lessons", json={"rule": "x", "scope": "role:wizard"}).status_code == 400
    )
    assert (
        client.post(f"/api/lessons/{lid}", json={"status": "retired"}).json()["status"] == "retired"
    )
    assert client.post(f"/api/lessons/{lid}", json={"rule": " "}).status_code == 400
    assert client.post("/api/lessons/L-99", json={"status": "active"}).status_code == 404
    assert client.get("/api/state").json()["lessons"][0]["id"] == lid

    root = str(project.root)
    assert main(["-C", root, "lesson", "add", "Use", "UTC", "--scope", "role:implementer"]) == 0
    assert main(["-C", root, "lesson", "restore", lid]) == 0
    assert main(["-C", root, "lesson", "edit", lid, "Never", "log", "secrets", "or", "tokens"]) == 0
    assert main(["-C", root, "lesson", "add", "x", "--scope", "bogus"]) == 1
    capsys.readouterr()
    assert main(["-C", root, "lessons"]) == 0
    out = capsys.readouterr().out
    assert "Never log secrets or tokens" in out and "role:implementer" in out

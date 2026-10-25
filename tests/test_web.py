from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.board import TicketStatus
from harness.project import Project
from harness.web.app import create_app
from tests.helpers import make_app, ticket
from tests.test_escalation import REQ


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return make_app(tmp_path)


@pytest.fixture
def client(project: Project) -> TestClient:
    return TestClient(create_app(project))


def test_index_and_state(project: Project, client: TestClient) -> None:
    project.board.add_tickets([ticket("T-1", owned_files=["a.py"])])
    project.board.queue("T-1")
    project.board.log_event(
        "T-1",
        "agent.finished",
        role="implementer",
        agent="i-1",
        model="claude-opus-5-5",
        outcome="finished",
        usage={"input_tokens": 1_000_000, "output_tokens": 100_000},
    )
    assert "Harness Board" in client.get("/").text
    s = client.get("/api/state").json()
    assert s["phase"] == "spec" and s["tickets"][0]["status"] == "ready"
    assert s["usage"]["total"]["cost_usd"] == pytest.approx(4.0 + 2.0)
    assert s["usage"]["by_role"]["implementer"]["runs"] == 1
    events = client.get("/api/events?after=0").json()
    assert [e["type"] for e in events][:2] == ["ticket.draft", "ticket.ready"]


def test_answering_decisions(project: Project, client: TestClient) -> None:
    did = project.board.create_decision(REQ, phase="spec")
    assert client.get("/api/state").json()["decisions_open"][0]["id"] == did
    assert client.post(f"/api/decisions/{did}", json={"choice": "Bcrypt"}).status_code == 400
    assert client.post(f"/api/decisions/{did}", json={"note": "  "}).status_code == 400
    r = client.post(f"/api/decisions/{did}", json={"choice": "Plain", "note": "internal"})
    assert r.status_code == 200 and r.json()["answer"] == {"choice": "Plain", "note": "internal"}
    assert client.post(f"/api/decisions/{did}", json={"choice": "Plain"}).status_code == 409
    assert client.post("/api/decisions/D-99", json={"choice": "x"}).status_code == 404
    assert client.get("/api/state").json()["decisions_answered"][0]["id"] == did


def test_approvals_and_documents(project: Project, client: TestClient) -> None:
    aid = project.board.create_approval("spec", "Spec v1")
    docs = client.get(f"/api/approvals/{aid}/documents").json()
    assert "AC-2" in docs["docs/spec.md"]  # falls back to main when no phase worktree
    assert client.post(f"/api/approvals/{aid}", json={"approved": False}).status_code == 400
    r = client.post(
        f"/api/approvals/{aid}", json={"approved": False, "feedback": "more edge cases"}
    )
    assert r.json()["approved"] is False and r.json()["feedback"] == "more edge cases"
    assert client.post(f"/api/approvals/{aid}", json={"approved": True}).status_code == 409
    assert client.get("/api/state").json()["approvals_open"] == []


def test_ticket_detail_and_transcripts(project: Project, client: TestClient) -> None:
    project.board.add_tickets([ticket("T-1", owned_files=["a.py"])])
    log_dir = project.harness_dir / "logs" / "T-1"
    log_dir.mkdir(parents=True)
    (log_dir / "implementer-1.jsonl").write_text(
        '{"role": "user", "content": [{"type": "text", "text": "hi"}]}\n'
    )
    d = client.get("/api/tickets/T-1").json()
    assert d["ticket"]["status"] == TicketStatus.DRAFT.value and d["logs"] == ["implementer-1"]
    msgs = client.get("/api/logs/T-1/implementer-1").json()
    assert msgs[0]["content"][0]["text"] == "hi"
    assert client.get("/api/logs/T-1/..%2F..%2Fboard").status_code in (400, 404)
    assert client.get("/api/tickets/nope").status_code == 404


def test_cross_site_form_posts_are_rejected(project: Project, client: TestClient) -> None:
    did = project.board.create_decision(REQ)
    r = client.post(f"/api/decisions/{did}", data={"choice": "Plain"})  # form-encoded
    assert r.status_code == 422
    assert project.board.get_decision(did)["answer"] is None

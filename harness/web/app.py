"""The Board dashboard: a local web UI over the project's board.

Everything goes through the board database, so the dashboard can run in its own
process next to `harness start` / `harness run`. Answers written here are picked up by
the waiting pipeline and by the orchestrator's hooks.

Bind it to localhost only. Write endpoints take JSON bodies, which browsers can't send
cross-site without a CORS preflight (and CORS is not enabled), so other sites can't
answer on your behalf.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from harness.board import BoardError, TicketStatus
from harness.context.builder import ARCHITECTURE_PATH, CONVENTIONS_PATH, SPEC_PATH
from harness.core.usage import usage_report
from harness.escalation.decisions import Decision, DecisionRequest
from harness.project import Project

STATIC = Path(__file__).parent / "static"
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")

# Which documents to show for each approval gate (read from that phase's worktree).
GATE_DOCUMENTS = {
    "spec": [SPEC_PATH],
    "architecture": [ARCHITECTURE_PATH, CONVENTIONS_PATH],
}


class DecisionAnswer(BaseModel):
    choice: str | None = None
    note: str = ""


class ApprovalAnswer(BaseModel):
    approved: bool
    feedback: str = ""


def _safe(part: str) -> str:
    if not _SAFE.match(part):
        raise HTTPException(400, f"invalid name {part!r}")
    return part


def create_app(project: Project) -> FastAPI:
    app = FastAPI(title="Harness Board", docs_url=None, redoc_url=None)
    board = project.board

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text()

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        project.reload()
        tickets = [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "agent": t.agent,
                "review_rounds": t.review_rounds,
                "depends_on": t.depends_on,
                "priority": t.priority,
            }
            for t in board.tickets()
        ]
        decisions = board.decisions()
        events = board.events(after_seq=0, limit=1)
        return {
            "project": project.root.name,
            "phase": board.get_meta("phase", "spec"),
            "tickets": tickets,
            "statuses": [s.value for s in TicketStatus],
            "decisions_open": [d for d in decisions if d["answer"] is None],
            "decisions_answered": [d for d in decisions if d["answer"] is not None][-10:][::-1],
            "approvals_open": board.approvals(open_only=True),
            "usage": usage_report(board, project.config).as_dict(),
            "has_events": bool(events),
        }

    @app.get("/api/events")
    def events(after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return [e.__dict__ for e in board.events(after_seq=after, limit=min(limit, 1000))]

    @app.get("/api/tickets/{ticket_id}")
    def ticket(ticket_id: str) -> dict[str, Any]:
        try:
            t = board.get(_safe(ticket_id))
        except BoardError as e:
            raise HTTPException(404, str(e)) from e
        log_dir = project.harness_dir / "logs" / ticket_id
        logs = sorted(p.stem for p in log_dir.glob("*.jsonl")) if log_dir.is_dir() else []
        return {
            "ticket": t.model_dump(mode="json"),
            "events": [e.__dict__ for e in board.events_for(ticket_id)],
            "logs": logs,
            "decisions": [d for d in board.decisions() if d["ticket_id"] == ticket_id],
        }

    @app.get("/api/logs/{key}/{agent}")
    def transcript(key: str, agent: str) -> list[dict[str, Any]]:
        path = project.harness_dir / "logs" / _safe(key) / f"{_safe(agent)}.jsonl"
        if not path.is_file():
            raise HTTPException(404, "no such transcript")
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    @app.post("/api/decisions/{decision_id}")
    def answer(decision_id: str, body: DecisionAnswer) -> dict[str, Any]:
        try:
            d = board.get_decision(_safe(decision_id))
        except BoardError as e:
            raise HTTPException(404, str(e)) from e
        if d["answer"] is not None:
            raise HTTPException(409, f"{decision_id} is already answered")
        request = DecisionRequest.model_validate(d["request"])
        if body.choice is not None and body.choice not in [o.name for o in request.options]:
            raise HTTPException(400, f"{body.choice!r} is not one of the options")
        if body.choice is None and not body.note.strip():
            raise HTTPException(400, "choose an option or write an answer")
        board.answer_decision(
            decision_id, Decision(choice=body.choice, note=body.note).model_dump()
        )
        return board.get_decision(decision_id)

    @app.post("/api/approvals/{approval_id}")
    def approve(approval_id: str, body: ApprovalAnswer) -> dict[str, Any]:
        if not body.approved and not body.feedback.strip():
            raise HTTPException(400, "say what should change")
        try:
            board.resolve_approval(_safe(approval_id), body.approved, body.feedback)
        except BoardError as e:
            raise HTTPException(409, str(e)) from e
        return board.get_approval(approval_id)

    @app.get("/api/approvals/{approval_id}/documents")
    def approval_documents(approval_id: str) -> dict[str, str]:
        try:
            gate = board.get_approval(_safe(approval_id))["gate"]
        except BoardError as e:
            raise HTTPException(404, str(e)) from e
        wt = project.worktrees.worktrees_dir / f"phase-{gate}"
        base = wt if wt.is_dir() else project.root
        return {
            rel: (base / rel).read_text()
            for rel in GATE_DOCUMENTS.get(gate, [])
            if (base / rel).is_file()
        }

    @app.get("/api/documents")
    def documents() -> dict[str, str]:
        return {
            rel: (project.root / rel).read_text()
            for rel in (SPEC_PATH, ARCHITECTURE_PATH, CONVENTIONS_PATH)
            if (project.root / rel).is_file()
        }

    return app


async def serve(project: Project, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the dashboard inside the current event loop (e.g. next to the pipeline)."""
    import uvicorn

    config = uvicorn.Config(create_app(project), host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()

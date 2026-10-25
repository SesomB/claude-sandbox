"""Persist agent transcripts and record each agent run on the board."""

from __future__ import annotations

import json
from typing import Any

from harness.agents.loop import AgentRun
from harness.project import Project


def log_agent_run(
    project: Project,
    key: str,
    role: str,
    agent: str,
    run: AgentRun[Any],
    *,
    ticket_id: str | None = None,
    model: str = "",
) -> None:
    log_dir = project.harness_dir / "logs" / key
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"{agent}.jsonl").open("w") as f:
        for m in run.messages:
            f.write(json.dumps(m.model_dump(mode="json")) + "\n")
    project.board.log_event(
        ticket_id,
        "agent.finished",
        role=role,
        agent=agent,
        model=model,
        outcome=run.outcome,
        reason=run.reason,
        turns=run.turns,
        usage=run.usage.model_dump(),
        cost_usd=run.cost_usd,
    )

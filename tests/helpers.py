"""Shared fixtures for build-loop tests: a tiny Python app and scripted agents."""

from __future__ import annotations

import itertools
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from harness.board import Ticket
from harness.models import FakeModelClient, Message, ModelResponse
from harness.models.fake import text_response, tool_response
from harness.project import Project, init_project
from tests.conftest import run_git

PY = sys.executable

STACK = f"""\
name: python
detect: [calc.py]
test: {PY} -m pytest -q -p no:cacheprovider
test_one: {PY} -m pytest -q -p no:cacheprovider {{test_id}}
source_globs: ["**/*.py"]
test_globs: ["tests/**/test_*.py"]
shared_files: [requirements.txt]
artifact_globs: ["**/__pycache__/**", "**/*.pyc", ".pytest_cache/**"]
test_failed_exit_codes: [1]
env: {{PYTHONDONTWRITEBYTECODE: "1"}}
"""

CONFIG = """\
providers: {fake: {type: fake}}
models: {default: {provider: fake, model: scripted}}
stacks: [python]
orchestrator: {lease_s: 60}
"""

SPEC = """\
# Calculator spec

## Behavior

- AC-1: add(a, b) returns the sum.
- AC-2: div(a, b) returns a / b; dividing by zero raises ValueError.
"""


def make_app(
    tmp_path: Path, *, config: str = CONFIG, extra_files: dict[str, str] | None = None
) -> Project:
    app = tmp_path / "app"
    init_project(app, user_config=tmp_path / "no-user-config.yaml")
    files = {
        ".harness/config.yaml": config,
        ".harness/stacks/python.yaml": STACK,
        "docs/spec.md": SPEC,
        "requirements.txt": "",
        ".gitignore": "__pycache__/\n.pytest_cache/\n",
        "tests/test_smoke.py": "def test_smoke():\n    pass\n",
        **(extra_files or {}),
    }
    for rel, content in files.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text(content)
    run_git(app, "add", "-A")
    run_git(app, "commit", "-q", "-m", "app skeleton")
    return Project.open(app, user_config=tmp_path / "no-user-config.yaml")


_ids = itertools.count(1)


def call(name: str, **input: Any) -> ModelResponse:
    return tool_response(name, input, id=f"tu_{next(_ids)}")


def finish(**input: Any) -> ModelResponse:
    return call("finish", **input)


def say(text: str) -> ModelResponse:
    return text_response(text)


_TICKET_RE = re.compile(r"\*\*([A-Za-z0-9._-]+): ")


class ScriptedAgents:
    """Hands each agent run the next script for its (role, ticket).

    The ticket is read from the context pack in the agent's first prompt.
    """

    def __init__(self) -> None:
        self.scripts: dict[tuple[str, str], list[list[ModelResponse]]] = defaultdict(list)
        self.clients: list[tuple[str, str, FakeModelClient]] = []

    def add(self, role: str, ticket_id: str, *responses: ModelResponse) -> None:
        self.scripts[(role, ticket_id)].append(list(responses))

    def factory(self, role: str) -> RoutingClient:
        return RoutingClient(self, role)

    def unused(self) -> dict[tuple[str, str], int]:
        return {k: len(v) for k, v in self.scripts.items() if v}


class RoutingClient:
    def __init__(self, agents: ScriptedAgents, role: str) -> None:
        self.agents = agents
        self.role = role
        self.inner: FakeModelClient | None = None

    async def complete(self, messages: list[Message], **kwargs: Any) -> ModelResponse:
        if self.inner is None:
            first = messages[0].content[0]
            text = getattr(first, "text", "")
            m = _TICKET_RE.search(text)
            ticket_id = m.group(1) if m else "-"  # phase agents have no ticket
            runs = self.agents.scripts[(self.role, ticket_id)]
            assert runs, f"no script left for {self.role} on {ticket_id}"
            self.inner = FakeModelClient(runs.pop(0))
            self.agents.clients.append((self.role, ticket_id, self.inner))
        return await self.inner.complete(messages, **kwargs)


def ticket(id: str, **kw: Any) -> Ticket:
    return Ticket(id=id, title=kw.pop("title", f"ticket {id}"), **kw)


class ScriptedHuman:
    """Answers decisions and approval gates from scripts, recording what it was shown."""

    def __init__(
        self, decisions: list[Any] | None = None, approvals: list[Any] | None = None
    ) -> None:
        self.decisions = list(decisions or [])
        self.approvals = list(approvals or [])
        self.requests: list[Any] = []
        self.gates: list[tuple[str, str]] = []

    async def decide(self, decision_id: str, request: Any) -> Any:
        self.requests.append((decision_id, request))
        assert self.decisions, f"unexpected decision request: {request.question}"
        return self.decisions.pop(0)

    async def approve(self, gate: str, summary: str) -> Any:
        self.gates.append((gate, summary))
        assert self.approvals, f"unexpected approval gate: {gate}"
        return self.approvals.pop(0)

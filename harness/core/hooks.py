"""Event hooks: functions run when a Board event of a given type is appended.

The default hooks are the build loop itself: a ticket becoming ``ready`` spawns an
Implementer, ``awaiting_review`` a Reviewer, ``changes_requested`` a Fixer, and
``approved`` the merge queue. Add your own with ``registry.on("ticket.done")``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from harness.board import Event
from harness.escalation.researcher import apply_ticket_decision

if TYPE_CHECKING:
    from harness.core.orchestrator import Orchestrator

Hook = Callable[[Event, "Orchestrator"], Awaitable[None]]


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[str, list[Hook]] = defaultdict(list)

    def on(self, *event_types: str) -> Callable[[Hook], Hook]:
        """Register a hook for event types (``"*"`` matches every event)."""

        def register(fn: Hook) -> Hook:
            for t in event_types:
                self._hooks[t].append(fn)
            return fn

        return register

    def handlers(self, event_type: str) -> list[Hook]:
        return [*self._hooks.get(event_type, []), *self._hooks.get("*", [])]


# Which role picks a ticket up from each claimable status.
ROLE_FOR_EVENT = {
    "ticket.ready": "implementer",
    "ticket.awaiting_review": "reviewer",
    "ticket.changes_requested": "fixer",
    "ticket.approved": "merger",
}


def default_hooks() -> HookRegistry:
    registry = HookRegistry()

    async def spawn_role(event: Event, orch: Orchestrator) -> None:
        if event.ticket_id is not None:
            orch.spawn(ROLE_FOR_EVENT[event.type], event.ticket_id)

    registry.on(*ROLE_FOR_EVENT)(spawn_role)

    async def research(event: Event, orch: Orchestrator) -> None:
        if event.ticket_id is not None:
            orch.spawn("researcher", event.ticket_id)

    async def apply_decision(event: Event, orch: Orchestrator) -> None:
        if event.ticket_id is not None:
            apply_ticket_decision(orch.board, str(event.payload["decision"]))

    registry.on("ticket.escalated")(research)
    registry.on("decision.answered")(apply_decision)
    return registry

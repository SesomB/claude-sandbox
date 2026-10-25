"""Token usage and cost, aggregated from the board's `agent.finished` events."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from harness.board import Board
from harness.config import HarnessConfig


@dataclass
class UsageLine:
    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_runs: int = 0  # runs on a model with no known price

    def add(self, usage: dict[str, Any], cost: float | None) -> None:
        self.runs += 1
        self.input_tokens += int(usage.get("input_tokens", 0))
        self.output_tokens += int(usage.get("output_tokens", 0))
        self.cache_read_input_tokens += int(usage.get("cache_read_input_tokens", 0))
        self.cache_creation_input_tokens += int(usage.get("cache_creation_input_tokens", 0))
        if cost is None:
            self.unpriced_runs += 1
        else:
            self.cost_usd += cost


@dataclass
class UsageReport:
    total: UsageLine = field(default_factory=UsageLine)
    by_role: dict[str, UsageLine] = field(default_factory=lambda: defaultdict(UsageLine))
    by_ticket: dict[str, UsageLine] = field(default_factory=lambda: defaultdict(UsageLine))
    by_model: dict[str, UsageLine] = field(default_factory=lambda: defaultdict(UsageLine))

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total.__dict__,
            "by_role": {k: v.__dict__ for k, v in sorted(self.by_role.items())},
            "by_ticket": {k: v.__dict__ for k, v in sorted(self.by_ticket.items())},
            "by_model": {k: v.__dict__ for k, v in sorted(self.by_model.items())},
        }


def run_cost(config: HarnessConfig, model: str, usage: dict[str, Any]) -> float | None:
    price = config.price_for(model)
    if price is None:
        return None
    cache_read = price.cache_read if price.cache_read is not None else price.input * 0.1
    cache_write = price.cache_write if price.cache_write is not None else price.input * 1.25
    return (
        int(usage.get("input_tokens", 0)) * price.input
        + int(usage.get("output_tokens", 0)) * price.output
        + int(usage.get("cache_read_input_tokens", 0)) * cache_read
        + int(usage.get("cache_creation_input_tokens", 0)) * cache_write
    ) / 1_000_000


def usage_report(board: Board, config: HarnessConfig) -> UsageReport:
    report = UsageReport()
    after = 0
    while True:
        events = board.events(after_seq=after, limit=5000)
        if not events:
            return report
        for e in events:
            if e.type != "agent.finished":
                continue
            usage = e.payload.get("usage") or {}
            model = str(e.payload.get("model") or "")
            cost = run_cost(config, model, usage)
            for line in (
                report.total,
                report.by_role[str(e.payload.get("role", "?"))],
                report.by_ticket[e.ticket_id or "(phases)"],
                report.by_model[model or "?"],
            ):
                line.add(usage, cost)
        after = events[-1].seq

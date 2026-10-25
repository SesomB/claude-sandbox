"""A terminal implementation of the Human channel."""

from __future__ import annotations

import asyncio
import textwrap

from harness.escalation.decisions import Approval, Decision, DecisionRequest


def render_request(decision_id: str, req: DecisionRequest) -> str:
    lines = [f"\n=== Decision {decision_id}: {req.question}"]
    if req.context:
        lines.append(textwrap.indent(req.context.strip(), "  "))
    for i, opt in enumerate(req.options, 1):
        star = "  (recommended)" if opt.name == req.recommendation else ""
        lines.append(f"\n  [{i}] {opt.name}{star}")
        if opt.description:
            lines.append(textwrap.indent(opt.description.strip(), "      "))
        lines += [f"      + {p}" for p in opt.pros]
        lines += [f"      - {c}" for c in opt.cons]
    lines.append(f"\n  Recommendation: {req.recommendation} (confidence: {req.confidence})")
    if req.reason:
        lines.append(f"  Why: {req.reason}")
    return "\n".join(lines)


def parse_choice(req: DecisionRequest, answer: str) -> Decision:
    """`3`, `3 because ...`, an option name, empty (= recommendation), or free text."""
    answer = answer.strip()
    if not answer:
        return Decision(choice=req.recommendation)
    head, _, rest = answer.partition(" ")
    if head.isdigit() and 1 <= int(head) <= len(req.options):
        return Decision(choice=req.options[int(head) - 1].name, note=rest.strip())
    for opt in req.options:
        if answer.lower() == opt.name.lower():
            return Decision(choice=opt.name)
    return Decision(note=answer)


class CliHuman:
    async def _input(self, prompt: str) -> str:
        return await asyncio.to_thread(input, prompt)

    async def decide(self, decision_id: str, request: DecisionRequest) -> Decision:
        print(render_request(decision_id, request))
        answer = await self._input(
            "\nYour choice (number, optionally followed by a note; Enter = recommendation; "
            "or type your own answer): "
        )
        return parse_choice(request, answer)

    async def approve(self, gate: str, summary: str) -> Approval:
        print(f"\n=== Approval gate: {gate}\n{textwrap.indent(summary.strip(), '  ')}")
        answer = (await self._input("\nApprove? [y/N, or type feedback to revise]: ")).strip()
        if answer.lower() in ("y", "yes"):
            return Approval(approved=True)
        if answer.lower() in ("", "n", "no"):
            feedback = (await self._input("What should change? ")).strip()
            return Approval(approved=False, feedback=feedback)
        return Approval(approved=False, feedback=answer)

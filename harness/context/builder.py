"""Builds each agent's minimal context pack and enforces its token budget.

Agents never browse the repo. They get exactly: their ticket, the spec sections it
references, the contracts it touches, the files in its scope, conventions, and
the lessons relevant to it. A pack over budget means the ticket is too big.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from harness.board import Ticket

SPEC_PATH = "docs/spec.md"
ARCHITECTURE_PATH = "docs/architecture.md"
CONVENTIONS_PATH = "docs/conventions.md"


class ContextBudgetExceeded(Exception):
    def __init__(self, role: str, tokens: int, budget: int) -> None:
        super().__init__(
            f"{role} context needs ~{tokens} tokens, budget is {budget}: split the ticket"
        )
        self.tokens = tokens
        self.budget = budget


def estimate_tokens(text: str) -> int:
    """A deliberately conservative estimate (~3.5 chars per token for code)."""
    return int(len(text) / 3.5) + 1


_HEADING = re.compile(r"^(#{1,6})\s")


def extract_sections(doc: str, ids: Iterable[str]) -> dict[str, str]:
    """For each id, the markdown that defines it.

    If the id appears in a heading, the whole section under that heading is used
    (until the next heading of the same or higher level). Otherwise the paragraph
    or list item containing it, with its indented continuation lines.
    """
    lines = doc.splitlines()
    out: dict[str, str] = {}
    for ident in ids:
        pattern = re.compile(rf"(?<![\w.-]){re.escape(ident)}(?![\w-]|\.\d)")
        for i, line in enumerate(lines):
            if not pattern.search(line):
                continue
            m = _HEADING.match(line)
            if m:
                level = len(m.group(1))
                end = i + 1
                while end < len(lines):
                    h = _HEADING.match(lines[end])
                    if h and len(h.group(1)) <= level:
                        break
                    end += 1
            else:
                end = i + 1
                while (
                    end < len(lines)
                    and lines[end].strip()
                    and (lines[end].startswith((" ", "\t")) or not _starts_item(lines[end]))
                    and not _HEADING.match(lines[end])
                ):
                    end += 1
            out[ident] = "\n".join(lines[i:end]).rstrip()
            break
    return out


def _starts_item(line: str) -> bool:
    return bool(re.match(r"^\s*([-*+]|\d+\.)\s", line))


@dataclass
class Pack:
    role: str
    sections: list[tuple[str, str]] = field(default_factory=list)

    def add(self, title: str, body: str) -> None:
        if body.strip():
            self.sections.append((title, body.strip()))

    def render(self) -> str:
        return "\n\n".join(f"## {title}\n\n{body}" for title, body in self.sections)

    def check_budget(self, budget: int) -> str:
        text = self.render()
        tokens = estimate_tokens(text)
        if tokens > budget:
            raise ContextBudgetExceeded(self.role, tokens, budget)
        return text


def _read(root: Path, rel: str) -> str | None:
    p = root / rel
    return p.read_text() if p.is_file() else None


def ticket_card(t: Ticket) -> str:
    lines = [f"**{t.id}: {t.title}**", ""]
    if t.acceptance:
        lines.append("Acceptance criteria:")
        lines += [f"- {a}" for a in t.acceptance]
    lines.append(f"\nFiles you own (create/modify only these): {', '.join(t.owned_files) or '-'}")
    if t.tests_required:
        lines.append(f"Required test files: {', '.join(t.tests_required)}")
    if t.readable_files:
        lines.append(f"Read-only context files: {', '.join(t.readable_files)}")
    if t.contracts:
        lines.append(f"Contracts: {', '.join(t.contracts)}")
    return "\n".join(lines)


def base_pack(
    role: str,
    ticket: Ticket,
    root: Path,
    *,
    lessons: list[str] | None = None,
    include_files: bool = True,
    guidance: str = "",
) -> Pack:
    pack = Pack(role)
    pack.add("Ticket", ticket_card(ticket))
    spec = _read(root, SPEC_PATH)
    if spec and ticket.spec_refs:
        found = extract_sections(spec, ticket.spec_refs)
        missing = [r for r in ticket.spec_refs if r not in found]
        body = "\n\n".join(found.values())
        if missing:
            body += f"\n\n(not found in {SPEC_PATH}: {', '.join(missing)})"
        pack.add("Spec (only the sections this ticket references)", body)
    arch = _read(root, ARCHITECTURE_PATH)
    if arch and ticket.contracts:
        pack.add("Contracts", "\n\n".join(extract_sections(arch, ticket.contracts).values()))
    conventions = _read(root, CONVENTIONS_PATH)
    if conventions:
        pack.add("Conventions", conventions)
    if guidance:
        pack.add("Guidance from the product owner (follow it)", guidance)
    if lessons:
        pack.add(
            "Lessons (rules learned on this project; follow them)",
            "\n".join(f"- {x}" for x in lessons),
        )
    if include_files:
        files = []
        for rel in sorted(
            set(ticket.owned_files) | set(ticket.tests_required) | set(ticket.readable_files)
        ):
            content = _read(root, rel)
            tag = "read-only" if rel in ticket.readable_files else "owned"
            if content is None:
                files.append(f"### {rel} ({tag}, does not exist yet)")
            else:
                files.append(f"### {rel} ({tag})\n```\n{content}\n```")
        pack.add("Files", "\n\n".join(files))
    return pack


def implementer_pack(
    ticket: Ticket,
    root: Path,
    budget: int,
    lessons: list[str] | None = None,
    guidance: str = "",
) -> str:
    return base_pack("implementer", ticket, root, lessons=lessons, guidance=guidance).check_budget(
        budget
    )


def reviewer_pack(
    ticket: Ticket,
    root: Path,
    budget: int,
    *,
    diff: str,
    gate_summary: str,
    implementer_summary: str = "",
    lessons: list[str] | None = None,
) -> str:
    pack = base_pack("reviewer", ticket, root, lessons=lessons, include_files=False)
    if implementer_summary:
        pack.add("Implementer's summary", implementer_summary)
    pack.add("Mechanical gate results (already verified by the harness)", gate_summary)
    pack.add("Diff against main", f"```diff\n{diff}\n```")
    return pack.check_budget(budget)


def fixer_pack(
    ticket: Ticket,
    root: Path,
    budget: int,
    *,
    diff: str,
    findings: list[dict[str, object]],
    merge_failure: str = "",
    lessons: list[str] | None = None,
) -> str:
    pack = base_pack("fixer", ticket, root, lessons=lessons)
    if findings:
        pack.add("Review findings to address", json.dumps(findings, indent=2))
    if merge_failure:
        pack.add("Problem to fix (a failed merge or the product owner's guidance)", merge_failure)
    pack.add("Diff of this ticket against main so far", f"```diff\n{diff}\n```")
    return pack.check_budget(budget)

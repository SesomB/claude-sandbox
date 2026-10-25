"""Mechanical checks on the spec sheet.

Spec items carry stable ids so tickets can reference exact sections:
G-* goals, US-* user stories, AC-* acceptance criteria, NFR-* non-functional
requirements, C-* constraints, OOS-* out of scope, Q-* open questions.
An id is *defined* where it starts a heading or a list item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PREFIXES = ("G", "US", "AC", "NFR", "C", "OOS", "Q")
REQUIRED = {"G": "goals", "US": "user stories", "AC": "acceptance criteria"}

_DEF = re.compile(
    r"^(?:#{1,6}\s+|\s*(?:[-*+]|\d+\.)\s+)(?:\*\*)?"
    rf"((?:{'|'.join(PREFIXES)})-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)\b"
)


@dataclass(frozen=True)
class SpecIssue:
    message: str
    line: int | None = None

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}" if self.line else self.message


def defined_ids(spec: str) -> dict[str, int]:
    """Spec ids mapped to the (1-based) line that defines them; first definition wins."""
    out: dict[str, int] = {}
    for n, line in enumerate(spec.splitlines(), 1):
        m = _DEF.match(line)
        if m and m.group(1) not in out:
            out[m.group(1)] = n
    return out


def lint_spec(spec: str) -> list[SpecIssue]:
    issues: list[SpecIssue] = []
    seen: dict[str, int] = {}
    for n, line in enumerate(spec.splitlines(), 1):
        m = _DEF.match(line)
        if not m:
            continue
        ident = m.group(1)
        if ident in seen:
            issues.append(SpecIssue(f"{ident} is defined twice (first on line {seen[ident]})", n))
        else:
            seen[ident] = n
        if ident.startswith("Q-"):
            issues.append(
                SpecIssue(f"open question {ident}: resolve it (ask the Board) and remove it", n)
            )
    for prefix, what in REQUIRED.items():
        if not any(i.startswith(prefix + "-") for i in seen):
            issues.append(SpecIssue(f"no {what} ({prefix}-*) defined"))
    return issues

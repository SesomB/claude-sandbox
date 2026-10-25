from __future__ import annotations

from pathlib import Path

from harness.board import TicketSpec
from harness.phases.spec_lint import defined_ids, lint_spec
from harness.phases.ticket_lint import lint_plan
from harness.stacks import builtin_profiles

SPEC = """\
# App
## Goals
- G-1: be useful
## Stories
### US-1 Reset password
- AC-1.1: tokens expire after 30 minutes
- **AC-1.2**: expired tokens are rejected
## Out of scope
- OOS-1: SSO
Text mentioning AC-9 is not a definition.
"""


def test_spec_ids_and_lint() -> None:
    assert list(defined_ids(SPEC)) == ["G-1", "US-1", "AC-1.1", "AC-1.2", "OOS-1"]
    assert lint_spec(SPEC) == []
    issues = [str(i) for i in lint_spec(SPEC + "- AC-1.1: again\n- Q-1: what about SSO?\n")]
    assert any("AC-1.1 is defined twice" in i for i in issues)
    assert any("open question Q-1" in i for i in issues)
    assert [str(i) for i in lint_spec("# nothing\n")] == [
        "no goals (G-*) defined",
        "no user stories (US-*) defined",
        "no acceptance criteria (AC-*) defined",
    ]


def T(id: str, **kw: object) -> TicketSpec:  # noqa: N802
    base: dict[str, object] = {
        "title": id,
        "spec_refs": ["AC-1.1"],
        "acceptance": ["works"],
        "owned_files": [f"src/{id}.py", f"tests/test_{id}.py"],
        "tests_required": [f"tests/test_{id}.py"],
    }
    base.update(kw)
    return TicketSpec(id=id, **base)  # type: ignore[arg-type]


def lint(tmp_path: Path, tickets: list[TicketSpec], budget: int = 50_000) -> list[str]:
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs/architecture.md").write_text("## Contracts\n### auth.tokens\nsig\n")
    return [
        str(i)
        for i in lint_plan(
            tickets,
            root=tmp_path,
            spec=SPEC,
            stacks=[builtin_profiles()["python"]],
            budget=budget,
        )
    ]


def test_good_plan_passes(tmp_path: Path) -> None:
    plan = [
        T("a", spec_refs=["AC-1.1"], contracts=["auth.tokens"]),
        T("b", spec_refs=["AC-1.2"], depends_on=["a"], readable_files=["src/a.py"]),
    ]
    assert lint(tmp_path, plan) == []


def test_plan_problems_are_all_reported(tmp_path: Path) -> None:
    plan = [
        T("a", size="M", spec_refs=["AC-7"], contracts=["nope"]),
        T("b", owned_files=["src/a.py", "pyproject.toml", "docs/spec.md"], tests_required=[]),
        T("c", readable_files=["src/a.py", "src/ghost.py"], depends_on=["zz"]),
        T("d", depends_on=["e"]),
        T("e", depends_on=["d"], tests_required=["tests/test_other.py"]),
    ]
    issues = lint(tmp_path, plan)
    expected = [
        "a: size M: split it into S tickets",
        "a: spec ref AC-7 is not defined in the spec",
        "a: contract nope is not described in docs/architecture.md",
        "b: src/a.py is also owned by a",
        "b: pyproject.toml is a shared file; tickets change it via edit_shared_file",
        "b: docs/spec.md is a harness/phase document and cannot be owned by a ticket",
        "b: owns source files but requires no tests",
        "c: depends on unknown ticket zz",
        "c: reads src/a.py, created by a: add a to depends_on",
        "c: readable file src/ghost.py doesn't exist and no ticket creates it",
        "d: is part of a dependency cycle",
        "e: required test tests/test_other.py must also be in owned_files",
        "AC-1.2 is not covered by any ticket's spec_refs",
    ]
    for e in expected:
        assert e in issues, f"missing: {e}\n got: {issues}"


def test_context_budget_is_checked(tmp_path: Path) -> None:
    issues = lint(tmp_path, [T("a", spec_refs=["AC-1.1", "AC-1.2"])], budget=10)
    assert any("split the ticket" in i for i in issues)

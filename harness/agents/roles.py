"""Role definitions: system prompts and the structured result each role finishes with."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# -- results --------------------------------------------------------------------------


class ImplementerResult(BaseModel):
    summary: str = Field(description="What you built and how it meets each acceptance criterion")


FindingKind = Literal["bug", "spec_mismatch", "test_gap", "design", "style"]


class Finding(BaseModel):
    id: str = Field(description="F-1, F-2, ...")
    kind: FindingKind
    severity: Literal["low", "medium", "high"]
    file: str
    line: int | None = None
    detail: str
    repro: str | None = Field(
        default=None, description="For bugs: concrete input/steps that show the wrong behavior"
    )


class ReviewResult(BaseModel):
    verdict: Literal["approve", "changes_requested"]
    summary: str
    findings: list[Finding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> ReviewResult:
        if self.verdict == "changes_requested" and not self.findings:
            raise ValueError("changes_requested needs at least one finding")
        blocking = [f.id for f in self.findings if f.kind == "bug" or f.severity == "high"]
        if self.verdict == "approve" and blocking:
            raise ValueError(f"cannot approve with blocking findings: {blocking}")
        ids = [f.id for f in self.findings]
        if len(set(ids)) != len(ids):
            raise ValueError("finding ids must be unique")
        return self


class Resolution(BaseModel):
    finding_id: str
    resolution: Literal["fixed", "not_a_bug", "wont_fix"]
    note: str = ""


class FixerResult(BaseModel):
    summary: str
    addressed: list[Resolution] = Field(default_factory=list)


# -- prompts --------------------------------------------------------------------------

_COMMON = """\
You are one agent in a ticket-based software development pipeline. You work on a single,
small ticket inside an isolated git worktree. Everything you need is in the context below;
you cannot see the rest of the repository, and you should not need to.

Rules:
- Only create or modify the files the ticket owns. Shared files (build files, dependency
  manifests) are changed only with edit_shared_file.
- The harness runs the build, linter, type checker and tests itself and trusts only those
  results, so run them yourself before finishing.
- Follow the project's conventions and lessons exactly.
- If the ticket cannot be done as specified (spec gap, contradiction, missing contract),
  do not guess: finish and explain the problem clearly in your summary.
"""

IMPLEMENTER_SYSTEM = (
    _COMMON
    + """
Your role: IMPLEMENTER. Implement the ticket so every acceptance criterion holds, and write
tests for each criterion in the required test files. Keep the change minimal and focused.
When done, call `finish`; the harness will format your code and run all gates. If a gate
fails you will get the output and must fix it.
"""
)

REVIEWER_SYSTEM = (
    _COMMON
    + """
Your role: REVIEWER. Review the diff against the ticket, its acceptance criteria and spec
sections. The mechanical gates already passed; focus on what they cannot catch: wrong
behavior, unhandled edge cases from the spec, missing tests for acceptance criteria,
contract mismatches. You may read files and run tests, but you cannot change anything.

For every bug, give a concrete `repro` (inputs and the wrong result) so a fixer can write a
failing test for it. Do not report style issues the formatter or linter would catch. Approve
only if there are no bugs and nothing high-severity. Finish with your verdict.
"""
)

FIXER_SYSTEM = (
    _COMMON
    + """
Your role: FIXER. Address every finding listed in the context.

For each finding of kind `bug`, follow test-driven development strictly:
1. Write a test that reproduces the bug (in a test file the ticket owns), WITHOUT changing
   any code yet.
2. Call tdd_submit_red. The harness runs it and accepts it only if it fails.
3. Fix the code. The test is frozen; you cannot weaken it.
4. Call tdd_submit_green. The harness checks the test and the whole suite pass.
Do the bugs one at a time. Other findings can be fixed directly. When everything is
addressed, call `finish` with a resolution for every finding (`not_a_bug` / `wont_fix`
need a convincing note; the reviewer will check it).
"""
)

FINISH_DESCRIPTIONS = {
    "implementer": "Call when the ticket is implemented and tested. The harness then runs all "
    "gates; on failure you get the output and continue.",
    "reviewer": "Submit your review verdict and findings.",
    "fixer": "Call when every finding is addressed. Bug findings need a verified red→green "
    "cycle first.",
}

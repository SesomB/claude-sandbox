"""Stack Profiles: everything language-specific (build, test, lint, ...) lives here.

The engine and the agents call the same steps (``test``, ``lint``, ...) for every
language; the project's profile decides what command each step runs.
"""

from __future__ import annotations

import shlex
from fnmatch import fnmatch
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from harness.executors.base import CommandResult, Executor

Step = Literal["configure", "build", "test", "test_one", "lint", "format", "typecheck", "coverage"]
STEPS: tuple[Step, ...] = (
    "configure",
    "build",
    "test",
    "test_one",
    "lint",
    "format",
    "typecheck",
    "coverage",
)


class StackError(Exception):
    pass


def match_glob(path: str, pattern: str) -> bool:
    """fnmatch where a leading ``**/`` also matches files at the repo root."""
    return fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch(path, pattern[3:]))


class StackProfile(BaseModel):
    name: str
    description: str = ""
    detect: list[str] = Field(default_factory=list)

    configure: str | None = None
    build: str | None = None
    test: str | None = None
    test_one: str | None = None
    lint: str | None = None
    format: str | None = None
    typecheck: str | None = None
    coverage: str | None = None

    test_framework: str | None = None
    contracts: str | None = None
    source_globs: list[str] = Field(default_factory=list)
    test_globs: list[str] = Field(default_factory=list)
    shared_files: list[str] = Field(default_factory=list)
    conventions: str | None = None

    def command(
        self, step: Step, *, files: list[str] | None = None, test_id: str | None = None
    ) -> str | None:
        """The shell command for ``step``, or ``None`` if this stack doesn't define it."""
        template: str | None = getattr(self, step)
        if template is None:
            return None
        if "{files}" in template and not files:
            raise StackError(f"{self.name}.{step} needs files")
        if "{test_id}" in template and not test_id:
            raise StackError(f"{self.name}.{step} needs a test_id")
        return template.replace("{files}", shlex.join(files or [])).replace(
            "{test_id}", shlex.quote(test_id or "")
        )

    def is_shared_file(self, path: str) -> bool:
        return any(match_glob(path, p) for p in self.shared_files)

    def is_source_file(self, path: str) -> bool:
        return any(match_glob(path, p) for p in self.source_globs)

    def is_test_file(self, path: str) -> bool:
        return any(match_glob(path, p) for p in self.test_globs)

    async def run(
        self,
        step: Step,
        executor: Executor,
        *,
        files: list[str] | None = None,
        test_id: str | None = None,
        timeout_s: float = 1800.0,
    ) -> CommandResult | None:
        """Run ``step`` in ``executor``; ``None`` if the stack has no such step."""
        cmd = self.command(step, files=files, test_id=test_id)
        if cmd is None:
            return None
        return await executor.run(cmd, timeout_s=timeout_s)


def _parse(text: str, source: str) -> StackProfile:
    try:
        data = yaml.safe_load(text)
        return StackProfile.model_validate(data)
    except (yaml.YAMLError, ValueError) as e:
        raise StackError(f"{source}: invalid stack profile: {e}") from e


def builtin_profiles() -> dict[str, StackProfile]:
    out: dict[str, StackProfile] = {}
    for entry in resources.files("harness.stacks.builtin").iterdir():
        if entry.name.endswith(".yaml"):
            profile = _parse(entry.read_text(), f"builtin/{entry.name}")
            out[profile.name] = profile
    return out


def load_profiles(project_root: Path | None = None) -> dict[str, StackProfile]:
    """Built-in profiles, overridden/extended by ``<project>/.harness/stacks/*.yaml``."""
    profiles = builtin_profiles()
    if project_root is not None:
        stack_dir = project_root / ".harness" / "stacks"
        if stack_dir.is_dir():
            for path in sorted(stack_dir.glob("*.yaml")):
                profile = _parse(path.read_text(), str(path))
                profiles[profile.name] = profile
    return profiles


def detect_stacks(root: Path, profiles: dict[str, StackProfile]) -> list[str]:
    """Names of profiles whose ``detect`` markers exist at the project root."""
    return [
        name
        for name, profile in profiles.items()
        if any(any(root.glob(marker)) for marker in profile.detect)
    ]


def resolve_stacks(project_root: Path, configured: list[str] | None = None) -> list[StackProfile]:
    """The project's stacks: from config if given, otherwise auto-detected."""
    profiles = load_profiles(project_root)
    names = configured if configured is not None else detect_stacks(project_root, profiles)
    unknown = [n for n in names if n not in profiles]
    if unknown:
        raise StackError(f"unknown stack(s): {', '.join(unknown)} (known: {', '.join(profiles)})")
    return [profiles[n] for n in names]

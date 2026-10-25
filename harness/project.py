"""A harness project: an application's own git repo plus its ``.harness/`` state dir."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from harness.board import Board
from harness.config import HarnessConfig, load_config
from harness.executors.worktree import WorktreeManager, git
from harness.stacks import StackProfile, resolve_stacks

HARNESS_DIR = ".harness"

DEFAULT_CONFIG = """\
# Harness project config. Personal defaults can live in ~/.config/harness/config.yaml;
# values here override them. Never put API keys here, only env var names.

# Who runs the agents. A coding-agent CLI uses its own login (e.g. your Claude
# subscription) and works only through the harness's tools; an API provider needs a key.
providers:
  claude_code: {type: claude_code}      # Claude Code CLI (`claude`), your existing login
  # opencode:  {type: opencode}         # OpenCode CLI; models as provider/model
  # anthropic: {type: anthropic, api_key_env: ANTHROPIC_API_KEY}   # direct API

models:
  default: {provider: claude_code, model: opus}
  roles:
    implementer: {provider: claude_code, model: sonnet}
    fixer:       {provider: claude_code, model: sonnet}
    # reviewer:  {provider: opencode, model: anthropic/claude-opus-5-5}

context_budget_tokens:
  implementer: 12000
  fixer: 12000
  reviewer: 16000

# stacks: [python, cpp-cmake]   # optional; auto-detected when omitted
"""

HARNESS_GITIGNORE = """\
board.db
board.db-*
worktrees/
logs/
"""


class ProjectError(Exception):
    pass


def find_project_root(start: Path) -> Path:
    """Walk up from ``start`` to the directory containing ``.harness/``."""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / HARNESS_DIR).is_dir():
            return d
    raise ProjectError(f"no {HARNESS_DIR}/ found in {start} or its parents (run `harness init`)")


def _is_git_repo(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True
    )
    return proc.returncode == 0 and Path(proc.stdout.strip()).resolve() == path.resolve()


def _has_commits(path: Path) -> bool:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True)
    return proc.returncode == 0


@dataclass
class Project:
    root: Path
    config: HarnessConfig
    board: Board
    worktrees: WorktreeManager
    user_config: Path | None = None
    _stacks: list[StackProfile] | None = field(default=None, repr=False)

    @property
    def harness_dir(self) -> Path:
        return self.root / HARNESS_DIR

    @classmethod
    def open(cls, path: Path, *, user_config: Path | None = None) -> Project:
        root = find_project_root(path)
        return cls(
            root=root,
            config=load_config(root, user_config),
            board=Board(root / HARNESS_DIR / "board.db"),
            worktrees=WorktreeManager(root),
            user_config=user_config,
        )

    def reload(self) -> None:
        """Re-read config and stacks (after a phase changed them on main)."""
        self.config = load_config(self.root, self.user_config)
        self._stacks = None

    def stacks(self) -> list[StackProfile]:
        if self._stacks is None:
            self._stacks = resolve_stacks(self.root, self.config.stacks)
        return self._stacks


def set_stacks_in_config(config_path: Path, stacks: list[str]) -> None:
    """Set the top-level ``stacks:`` key in a config file, keeping its comments."""
    text = config_path.read_text() if config_path.exists() else ""
    line = f"stacks: [{', '.join(stacks)}]"
    out, replaced = [], False
    for existing in text.splitlines():
        if existing.startswith("stacks:"):
            out.append(line)
            replaced = True
        else:
            out.append(existing)
    if not replaced:
        out.append(line)
    config_path.write_text("\n".join(out) + "\n")


def init_project(path: Path, *, user_config: Path | None = None) -> Project:
    """Make ``path`` a harness project: git repo on ``main`` + ``.harness/``. Idempotent."""
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    if not _is_git_repo(path):
        git(path, "init", "-q", "-b", "main")
    hdir = path / HARNESS_DIR
    hdir.mkdir(exist_ok=True)
    created = []
    for name, content in (("config.yaml", DEFAULT_CONFIG), (".gitignore", HARNESS_GITIGNORE)):
        target = hdir / name
        if not target.exists():
            target.write_text(content)
            created.append(f"{HARNESS_DIR}/{name}")
    if created:
        git(path, "add", *created)
        if not _has_commits(path) or git(path, "diff", "--cached", "--name-only").strip():
            git(path, "commit", "-q", "-m", "Initialize harness project")
    return Project.open(path, user_config=user_config)

# claude-sandbox: Software Development Harness

A spec-driven, ticket-based, multi-agent software development harness. You
approve a detailed spec; agents break it into small tickets, implement them in
isolated worktrees, review each other's work, fix review findings test-first,
and merge into `main`. Only real decisions are escalated to you, each with
options and pros/cons. See [`docs/PLAN.md`](docs/PLAN.md) for the full design.

## Status

**M0 – Foundations** (done):

- `harness.config`: user + project config, provider and model per role
- `harness.models`: provider-neutral message/tool types, `ModelClient` protocol,
  `FakeModelClient` for offline tests, provider registry
- `harness.executors`: `LocalExecutor` (confined to a root dir, timeouts kill the
  whole process group) and `WorktreeManager` (one git worktree + branch per ticket)
- `harness.board`: SQLite ticket board with atomic claims, leases, file ownership,
  dependency unblocking, escalations, and an event log
- `harness.stacks`: language-agnostic Stack Profiles; built-in `python` and `cpp-cmake`
- `harness` CLI: `init`, `config`, `stacks`, `ticket add|queue`, `board`, `events`

Next: **M1 – the build loop** (agent tool loop, hooks, implement → review → fix with
red→green verification, merge queue).

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## Quick try

```bash
uv run harness init /tmp/my-app
cd /tmp/my-app
cat > tickets.yaml <<'YAML'
- id: T-1
  title: Token model
  owned_files: [app/tokens.py]
- id: T-2
  title: Reset endpoint
  depends_on: [T-1]
  owned_files: [app/reset.py]
YAML
uv run --project <path-to-this-repo> harness ticket add tickets.yaml --queue
uv run --project <path-to-this-repo> harness board
```

# Software Development Harness — Plan (draft v0)

A Python, provider-agnostic, multi-agent orchestration engine. Role agents
(planner, coder, tester, reviewer, ...) collaborate on a shared workspace to
carry a software task from request to verified change.

## Decisions so far

| Area      | Decision                                              |
|-----------|-------------------------------------------------------|
| Scope     | General multi-agent orchestrator over a shared workspace |
| Language  | Python (3.12+, asyncio)                               |
| Models    | Provider-agnostic: one `ModelClient` interface, adapters per provider |
| Execution | Abstract `Executor` interface now; backend chosen later |

## Architecture

```
            ┌──────────────────────── Orchestrator ────────────────────────┐
 Task  ──▶  │  Workflow (graph of steps)  ──▶  Scheduler  ──▶  Step runner │
            └───────────────┬───────────────────────┬──────────────────────┘
                            │                       │
                    Agent (role + policy)     Event bus / run log
                            │                       │
              ┌─────────────┼─────────────┐         ▼
              ▼             ▼             ▼     Persistence (resume, replay)
         ModelClient     Tool registry   Memory / Blackboard
         (adapters)          │
                             ▼
                         Executor (local | worktree | docker | remote)
```

### Core components

1. **ModelClient** — `async complete(messages, tools, **opts) -> Response`.
   Normalized message/tool-call schema; adapters translate (Anthropic first,
   then OpenAI-compatible, local). Handles retries, streaming, token/cost accounting.
2. **Agent** — a role prompt + allowed tools + model config + the tool-use loop
   (call model → run tool calls → feed results → until done or budget hit).
3. **Tools** — typed (Pydantic) schemas, registered by name: `read_file`,
   `write_file`, `edit_file`, `search`, `run_command`, `run_tests`, `git_*`.
   Every tool runs through the Executor, never directly on the host.
4. **Executor** — `run(cmd, cwd, timeout, env)`, `read/write file`, `snapshot/diff`.
   First implementation: `LocalExecutor` (dev only). Later: worktree, Docker.
5. **Workspace / Blackboard** — shared state agents read and write: task spec,
   plan, artifacts (diffs, test reports, review notes), decisions.
6. **Workflow engine** — declarative graph (YAML or Python) of steps with
   roles, inputs/outputs, conditions, and loops (e.g. code → test → fix,
   capped at N iterations). Steps whose inputs are ready run in parallel.
7. **Policy / guardrails** — per-role tool allowlists, command allow/deny
   rules, budget limits (tokens, cost, wall-clock, iterations), human approval gates.
8. **Observability** — structured event log (JSONL) of every model call, tool call,
   and state change; enables resume, replay, and a later web UI.

### Default workflow ("feature")

```
plan ──▶ implement ──▶ test ──┬─ pass ──▶ review ──┬─ approve ──▶ done
              ▲               │                    │
              └──── fix ◀─────┴─ fail      changes ┘
```

## Proposed layout

```
harness/
  core/        # orchestrator, scheduler, workflow graph, events
  agents/      # Agent loop, role definitions (prompts + policies)
  models/      # ModelClient protocol + adapters
  tools/       # tool registry + built-in tools
  executors/   # Executor protocol + local/worktree/docker
  workspace/   # blackboard, artifacts, persistence
  cli.py       # `harness run <workflow> --task ...`
workflows/     # YAML workflow definitions
tests/
```

## Milestones

- **M0 – Skeleton:** package, CI (ruff, mypy, pytest), core protocols, a fake
  `ModelClient` that replays scripted responses, so everything can be tested offline.
- **M1 – Single agent:** tool-use loop + file/command tools + LocalExecutor + Anthropic adapter.
  Demo: one agent fixes a failing test in a toy repo.
- **M2 – Multi-agent:** blackboard, workflow graph, the planner/coder/tester/reviewer workflow,
  JSONL event log.
- **M3 – Safety & budgets:** policies, approval gates, budget enforcement, resume from log.
- **M4 – Isolation:** worktree and Docker executors; parallel tasks.
- **M5 – Extensibility:** second model provider, plugin tools (MCP), simple web dashboard.

## Open questions

1. How agents hand off: shared blackboard only, or also direct messages between agents?
2. How workflows are defined: YAML, a Python DSL, or both?
3. Is there a human in the loop: approval gates, or fully autonomous?
4. What target repos/languages the agents will work on first (this decides the test tooling).
5. Should the harness open PRs (via a git hosting integration) or just produce a branch/diff?
6. What "done" means: tests pass, reviewer approves, both, or a custom check?

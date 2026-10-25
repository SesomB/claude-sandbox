# Software Development Harness — Plan (draft v3)

A Python, provider-agnostic engine that turns a product idea into working
software through a **ticket-driven, waterfall pipeline** run by small,
focused agents. The human (you) acts as **the Board**: you only see what is
escalated, and every escalation arrives as a ready-to-decide set of options.

## Guiding principles

1. **Spec first.** Nothing is built until a detailed, approved spec sheet exists.
2. **Tiny context per agent.** Each agent sees only what its ticket needs. Small
   context makes agents focused and accurate.
3. **Atomic tickets, no clashes.** Agents claim tickets atomically, work on
   isolated branches, and only touch the files their ticket owns.
4. **Hooks drive the flow.** Events (implemented, reviewed, failed, ...) trigger
   the next agent automatically. No polling, no manual hand-offs.
5. **Mechanical gates over model judgment.** Wherever a tool can verify
   something (tests red→green, lint, types, file scope), the tool decides, not the LLM.
6. **Escalate with options.** Anything sent to the Board comes with alternatives,
   pros/cons, and a recommendation.
7. **The system learns.** Board feedback and recurring review findings become
   lessons that change future agent behavior.

## Decisions so far

| Area      | Decision                                              |
|-----------|-------------------------------------------------------|
| Engine language | Python 3.12+, asyncio                           |
| Generated apps | **Any language** via pluggable Stack Profiles (§11); Python and C++ first |
| Models    | Provider-agnostic `ModelClient`; provider and model per role set in config (§12) |
| Process   | Waterfall phases with gates, ticket-based execution   |
| Execution | Runs on your machine with your installed toolchains; per-ticket git worktree |
| Repos     | Each generated application has its own git repository |
| Limits    | No cost budget, no cap on parallel agents (only provider rate limits apply, §4) |
| Learning  | Lessons apply automatically; visible and revocable on the dashboard |
| Dashboard | Local web dashboard only, no external notifications   |
| Merging   | Harness merges approved tickets into `main` itself via the merge queue |

---

## 1. Waterfall pipeline

```
 ┌──────────┐   ┌──────────────┐   ┌───────────────┐   ┌────────────────┐   ┌─────────────┐
 │ 1. Spec  │──▶│ 2. Architect │──▶│ 3. Decompose  │──▶│ 4. Build loop  │──▶│ 5. Integrate│
 │  sheet   │   │  & contracts │   │  into tickets │   │ (per ticket)   │   │ & accept    │
 └────┬─────┘   └──────┬───────┘   └──────┬────────┘   └───────┬────────┘   └──────┬──────┘
   Board gate       Board gate        Board gate          escalations only      Board gate
```

| Phase | Agent(s) | Output | Gate |
|-------|----------|--------|------|
| 1. Spec | Spec Writer (interviews you), Spec Critic | `spec.md`: goals, users, features, user stories, acceptance criteria, non-functional reqs, out-of-scope, open questions | You approve the spec |
| 2. Architecture | Architect | `architecture.md`: stack, modules, data model, **interface contracts** (signatures, schemas, API routes), conventions, repo skeleton | You approve; skeleton compiles and lints |
| 3. Decomposition | Decomposer, Ticket Critic | Ticket DAG on the board | Every ticket passes the ticket linter (§3); you approve the board |
| 4. Build | Implementer → Reviewer → Fixer (hook loop) | Merged, tested code per ticket | Mechanical gates + review approval |
| 5. Integration | Integrator, QA agent | End-to-end tests against the spec's acceptance criteria | You accept the release |

Going back to an earlier phase (e.g. a spec gap found mid-build) is an escalation.

## 2. The spec sheet

Structured Markdown with stable IDs so tickets can link to exact sections:

- `G-*` goals, `US-*` user stories, `AC-*` acceptance criteria (testable,
  Given/When/Then), `NFR-*` non-functional requirements, `C-*` constraints,
  `OOS-*` out of scope, `Q-*` open questions (must be empty to pass the gate).

The Spec Critic scores the spec (ambiguity, missing edge cases, untestable
criteria) and bounces it back until it passes, escalating only real product decisions.

## 3. Tickets

### Ticket schema

```yaml
id: T-042
title: Add password-reset token model
spec_refs: [US-7, AC-7.1, AC-7.2]
depends_on: [T-040]
contracts: [auth.tokens.ResetToken]           # from architecture.md
owned_files:  [app/auth/tokens.py, tests/auth/test_tokens.py]
readable_files: [app/auth/models.py, app/db/base.py]
acceptance:
  - "Given a user, create_reset_token returns a token expiring in 30 min"
  - "Expired tokens are rejected by verify_reset_token"
tests_required: [tests/auth/test_tokens.py]
size: S                                        # S only; M is split, L is rejected
```

### Ticket linter (mechanical)
Rejects a ticket if: it has no acceptance criteria or spec refs; its owned files overlap
another *open* ticket's owned files; the estimated context pack is over budget
(e.g. 12k tokens); its dependencies form a cycle; or it is larger than size S.

### Lifecycle (state machine)

```
draft ─▶ waiting ─▶ ready ─▶ implementing ─▶ awaiting_review ─▶ reviewing ─┬─▶ approved ─▶ merging ─▶ done
          (deps)                                   ▲                       │                 │
                                                   └── fixing ◀── changes_requested ◀────────┘ (merge failed)
any non-final state ──▶ escalated ──▶ (Board decides) ready / awaiting_review / changes_requested / cancelled
```

`ready`, `awaiting_review`, `changes_requested` and `approved` are **claimable**: an
agent (Implementer, Reviewer, Fixer, merge queue) atomically claims the ticket and
holds it under a lease. If the lease expires, the ticket returns to where it was
claimed from.

A ticket becomes `ready` only when all `depends_on` tickets are `done`.

## 4. The board & atomic claiming

- **Storage:** SQLite (WAL mode) as the single source of truth for tickets,
  events, leases, decisions, lessons. Simple, transactional, file-based.
- **Atomic claim:** `UPDATE tickets SET status='claimed', agent=?, lease_until=?
  WHERE id=(SELECT id FROM tickets WHERE status='ready' ORDER BY priority LIMIT 1)
  AND status='ready' RETURNING id` — a single transaction, so two agents can never
  get the same ticket.
- **Leases:** an agent sends a heartbeat to renew its lease; an expired lease
  returns the ticket to `ready` (or `fixing`) for another agent.
- **No file clashes:**
  1. Ownership: open tickets never share `owned_files` (enforced by the linter).
  2. Isolation: each ticket runs in its own git worktree/branch.
  3. Scope check: the diff may only touch `owned_files`, otherwise it is rejected mechanically.
  4. Merge queue: merges are serialized; each merge rebases and re-runs the gates.
  5. Shared files (build files, dependency manifests, package `__init__`s) are never
     owned by a ticket. Agents request changes to them through structured tools
     (`add_source_file`, `add_dependency`, ...) and the merge queue applies those
     requests one at a time (§11).
- **Concurrency:** no fixed cap. Every `ready` ticket gets an Implementer at once.
  The only throttle is the model provider's rate limit: the scheduler backs off
  on rate-limit errors and resumes automatically. Token usage and cost are tracked
  and shown, but never limit the work.
- **Merging into `main`:** the merge queue takes each approved ticket, rebases it on
  `main`, runs the full gates on the combined result, and fast-forwards `main`.
  If `main` ever goes red after a merge, the harness reverts that merge automatically,
  reopens the ticket with the failure attached, and continues.
- **Dashboard:** a local web UI (FastAPI + a simple frontend) showing a kanban of
  tickets, the live event feed, token/cost usage, the **Decisions** inbox, and the
  **Lessons** list. No external notifications. A CLI (`harness board`) shows the same data.

## 5. Hooks (event-driven orchestration)

Every state change emits an event; the orchestrator maps events to handlers:

| Event | Hook action |
|-------|-------------|
| `ticket.ready` | Spawn an Implementer (up to the concurrency limit) |
| `ticket.implemented` | Run mechanical gates; if green, spawn a Reviewer |
| `gates.failed` | Send the failure output back to the same Implementer (max N tries) |
| `review.changes_requested` | Spawn a **Fixer** with only the ticket + diff + review comments |
| `review.approved` | Put the ticket in the merge queue |
| `ticket.merged` | Mark dependents `ready`; record lessons |
| `ticket.failed_n_times` / `contract.change_needed` / `spec.gap` | Escalate to the Board |
| `board.decided` | Apply the decision; record feedback as a lesson |

Hooks are plain Python functions registered on event types, so users can add their own
(e.g. notify Slack, run a security scan before merging).

## 6. Small context: the context pack

Agents never browse the whole repo. Before spawning one, a **Context Builder**
assembles a minimal pack and the agent's tools are scoped to it:

| Role | Gets | Doesn't get |
|------|------|-------------|
| Implementer | Ticket, referenced spec sections only, relevant contracts, contents of `owned_files` + `readable_files`, conventions, relevant lessons | Other tickets, full spec, chat history |
| Reviewer | Ticket, acceptance criteria, the diff, gate results, review checklist, lessons | Implementer's reasoning |
| Fixer | Ticket, diff, review comments, the specific files, TDD rule | Original implementation conversation |

- Hard token budget per pack (configurable per role); the builder trims or the
  ticket is sent back to be split.
- Write tools are limited to `owned_files`; read tools to the pack's files.
- Every agent starts fresh; there are no long-running conversations.

## 7. Review & fix loop with TDD for bugs

Reviewer output is structured, not free text:

```yaml
verdict: changes_requested
findings:
  - id: F-1
    kind: bug            # bug | spec_mismatch | style | test_gap | design
    severity: high
    file: app/auth/tokens.py
    line: 31
    detail: Expiry compares naive and aware datetimes
    repro: verify_reset_token accepts a token 31 minutes old
```

For each `bug` finding, the Fixer must follow **red → green**, and the harness
verifies each step mechanically:

1. Fixer writes a test reproducing the bug, then calls `tdd.submit_red()`.
   The harness runs that test on the current code: it **must fail**, or the
   step is rejected.
2. Fixer writes the fix, then calls `tdd.submit_green()`. The harness runs that test
   (**must pass**) and the full test suite (**no regressions**).
3. The commit history records the failing test and the fix as separate commits.

The ticket then goes back to `in_review`. After N review rounds it is escalated.

## 8. Escalations to the Board

Only escalated items reach you. Each one is a **Decision Request**:

```yaml
id: D-007
ticket: T-042
question: Should reset tokens be stored hashed or in plain text?
context: (short, linked to spec/ticket)
options:
  - name: Hash with SHA-256
    pros: [DB leak doesn't expose usable tokens, cheap]
    cons: [token can't be shown again, slightly more code]
  - name: Store plain
    pros: [simplest]
    cons: [DB leak = account takeover]
recommendation: Hash with SHA-256
confidence: high
impact_if_delayed: blocks T-042, T-045
```

- A **Researcher** agent builds the options (at least 2, with pros and cons, and
  research or citations where useful), so you never have to research on your own.
- You choose an option, write your own, or ask a follow-up question. Your reason is
  saved as a lesson (§9).
- What gets escalated: spec gaps, contract changes, repeated failures, conflicting
  findings, security trade-offs, a new lesson that contradicts the approved spec.

## 9. Learning (lessons & skills)

- **Lesson store** (SQLite + text search; embeddings later): each lesson has a
  rule, a source (Board feedback / review finding / failed gate), a scope
  (global, role, module, technology), examples, and a hit count.
- **Sources:**
  - Your feedback ("don't do X") becomes a lesson immediately, at high priority.
  - The same review finding kind appearing 2+ times becomes a candidate lesson.
  - Repeated gate failures (e.g. the same lint rule) become a lesson.
- **Auto-apply:** new lessons take effect for the next agent spawned, with no approval
  step. Every lesson appears on the dashboard with its source and hit count, and you
  can edit or revoke any of them. The only exception is a lesson that contradicts
  the approved spec: that is escalated instead of applied.
- **Use:** the Context Builder injects only the lessons relevant to the ticket's scope
  (top-k, within budget), so learning doesn't bloat context.
- **Curation:** a Librarian agent periodically merges duplicates, generalizes,
  and retires lessons that no longer match anything (all logged, all revocable).
- **Skills:** stable lesson clusters are promoted into role prompts / skill files
  (versioned), so the lessons become part of the agents' standard instructions.

## 10. Mechanical toolbox

Deterministic tools the agents call; the harness trusts their results, not the agent's claims:

| Tool | Purpose |
|------|---------|
| `run_tests(selector)` | Run tests, return structured pass/fail + output |
| `tdd.submit_red / submit_green` | Enforce and verify red→green for bug fixes |
| `lint`, `typecheck`, `format` | Language-specific gates (pluggable per stack) |
| `scope_check` | Diff touches only `owned_files` |
| `contract_check` | Public signatures match `architecture.md` contracts |
| `coverage_check` | New code has tests (threshold per ticket) |
| `ticket_lint` | Validate tickets (§3) |
| `spec_trace` | Map tests ↔ acceptance criteria; find uncovered `AC-*` |
| `context_budget` | Measure a context pack's token size |
| `merge_check` | Simulate rebase/merge and re-run gates |

Every tool that depends on the language (tests, lint, types, coverage, contracts)
is resolved through the project's Stack Profile (§11), so the agents call the same
tool names in every language.

## 11. Stack Profiles (language-agnostic)

The engine itself knows no language. Everything language-specific lives in a
**Stack Profile**: a YAML file plus an optional small Python plugin.

```yaml
name: cpp-cmake
detect: [CMakeLists.txt]
build:      cmake --build build -j
test:       ctest --test-dir build --output-on-failure
test_one:   ctest --test-dir build -R "{test_id}" --output-on-failure
lint:       clang-tidy -p build {files}
format:     clang-format -i {files}
typecheck:  "{build}"                     # compiling is the type check
coverage:   gcovr -r . --json
test_framework: googletest
contracts:  libclang                      # plugin: extract public signatures
shared_files: ["**/CMakeLists.txt", "vcpkg.json", "conanfile.txt"]
conventions: docs/conventions/cpp.md
```

| Profile | Build / deps | Tests | Lint / format | Types | Coverage | Contracts |
|---------|--------------|-------|---------------|-------|----------|-----------|
| `python` | uv / pyproject | pytest | ruff | mypy / pyright | coverage.py | `ast` |
| `cpp-cmake` | CMake + vcpkg/Conan | GoogleTest or Catch2 via CTest | clang-tidy, clang-format | compiler (+ `-Wall -Werror`) | gcovr / llvm-cov | libclang |

- **Choosing a stack:** the Architect proposes the stack per module (one project can
  mix, e.g. a C++ core with Python bindings); you approve it at the architecture gate.
- **Any other language:** if no profile exists, a Toolchain agent drafts one, and the
  harness validates it mechanically on the skeleton (builds, a sample test goes red
  then green, lint runs). Only a profile that passes is used; otherwise it is escalated.
- **C++ details:** tickets own header + source + test files together; `CMakeLists.txt`
  is a shared file, changed only through `add_source_file` / `add_test_target` requests
  applied by the merge queue, so parallel tickets never clash on build files.
  Sanitizers (ASan/UBSan) run as an extra gate in debug builds.
- **Python details:** `pyproject.toml` dependencies and package `__init__.py`
  exports are shared files, changed through `add_dependency` / `export_symbol`.

## 12. Projects & configuration

Each application is its **own git repository**. The harness is installed once on
your machine and pointed at a repo; all of its state lives inside that repo:

```
my-app/                      # the application's own repo
  .harness/
    config.yaml              # project config (committed)
    board.db                 # tickets, events, leases, decisions, lessons (gitignored)
    worktrees/T-042/         # per-ticket worktrees (gitignored)
    logs/                    # JSONL event + model-call logs (gitignored)
  docs/spec.md               # approved spec (committed)
  docs/architecture.md       # approved architecture + contracts (committed)
  src/ ...
```

`harness init` creates `.harness/` in a repo (running `git init` first if needed).

Models are fully configurable: providers are declared once, and each role picks a
provider + model. Config is merged from `~/.config/harness/config.yaml` (personal
defaults, API key env var names) and the project's `.harness/config.yaml`.

```yaml
providers:
  anthropic: {type: anthropic, api_key_env: ANTHROPIC_API_KEY}
  openai:    {type: openai_compatible, base_url: https://api.openai.com/v1, api_key_env: OPENAI_API_KEY}
  local:     {type: openai_compatible, base_url: http://localhost:11434/v1}

models:
  default: {provider: anthropic, model: claude-opus-5-5}
  roles:
    implementer: {provider: anthropic, model: claude-sonnet-5-5}
    fixer:       {provider: anthropic, model: claude-sonnet-5-5}
    librarian:   {provider: local, model: qwen3-coder, max_tokens: 4096}

context_budget_tokens:
  implementer: 12000
  reviewer: 16000

stacks: [python, cpp-cmake]   # optional; auto-detected otherwise
```

API keys are never written to config; only the name of the environment variable is.

---

## Architecture

```
          ┌────────────── Orchestrator ───────────────┐
 events ─▶│ Event bus ─▶ Hook registry ─▶ Agent runner │─▶ spawns role agents
          └──────┬─────────────┬──────────────┬───────┘
                 │             │              │
            Board (SQLite)  Context Builder  Gates/Toolbox
          tickets, events,  spec, contracts,  tests, lint, TDD,
          leases, decisions lessons, files    scope, merge queue
                 │                            │
          Web dashboard + CLI            Executor (worktree → docker)
                                              │
                                         ModelClient adapters
```

## Proposed layout

```
harness/
  core/        # event bus, hooks, orchestrator, state machine
  board/       # SQLite models, atomic claim, leases, merge queue
  phases/      # spec, architecture, decomposition, build, integration
  agents/      # agent loop + role definitions (prompts, tool scopes)
  context/     # context builder, token budgeting
  gates/       # mechanical toolbox (tests, tdd, scope, contracts, ...)
  learning/    # lessons store, librarian, skill promotion
  escalation/  # decision requests, researcher
  models/      # ModelClient protocol + adapters (Anthropic first, fake for tests)
  executors/   # Executor protocol + worktree (+ docker later)
  web/         # FastAPI dashboard
  cli.py
tests/
```

## Milestones

- **M0 – Foundations:** package, CI, config loading + role→model resolution,
  `ModelClient` + fake model, `Executor` + worktree manager, `harness init`,
  SQLite board with atomic claim + leases (with concurrency tests), Stack Profile
  loader with `python` and `cpp-cmake` profiles.
- **M1 – Build loop:** event bus + hooks; Implementer → gates → Reviewer → Fixer with
  TDD red→green verification; merge queue with shared-file requests and auto-revert.
  Tickets written by hand, tested on a small Python repo and a small C++ repo.
- **M2 – Upstream phases:** Spec Writer/Critic, Architect, Decomposer + ticket linter;
  full pipeline on a toy app.
- **M3 – Board:** decision requests + Researcher, local web dashboard + CLI, usage tracking.
- **M4 – Learning:** lesson store with auto-apply, injection into context packs,
  Librarian, skill promotion, revoke/edit on the dashboard.
- **M5 – Hardening:** integration/QA phase, more providers, optional Docker executor,
  Toolchain agent for new languages.

M1 is first on purpose: the build loop is the core of the system, and it can be
tested with hand-written tickets before the spec/decomposition agents exist.

## Open questions

None blocking M0.

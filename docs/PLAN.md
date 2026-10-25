# Software Development Harness — Plan (draft v1)

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
| Language  | Python 3.12+, asyncio                                 |
| Models    | Provider-agnostic `ModelClient`; different models per role allowed |
| Process   | Waterfall phases with gates, ticket-based execution   |
| Execution | `Executor` interface; per-ticket git worktree as the first backend |

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
draft ─▶ ready ─▶ claimed ─▶ implementing ─▶ in_review ─┬─▶ approved ─▶ merging ─▶ done
                                  ▲                     │
                                  └──── fixing ◀────────┘ changes_requested
any state ──▶ blocked / escalated  (on repeated failure, spec gap, contract change)
```

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
- **Dashboard:** a web UI (FastAPI + simple frontend) showing a kanban of tickets,
  the live event feed, costs, and the **Decisions** inbox. A CLI (`harness board`)
  shows the same data.

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
  findings, security trade-offs, anything exceeding a budget.

## 9. Learning (lessons & skills)

- **Lesson store** (SQLite + text search; embeddings later): each lesson has a
  rule, a source (Board feedback / review finding / failed gate), a scope
  (global, role, module, technology), examples, and a hit count.
- **Sources:**
  - Your feedback ("don't do X") becomes a lesson immediately, at high priority.
  - The same review finding kind appearing 2+ times becomes a candidate lesson.
  - Repeated gate failures (e.g. the same lint rule) become a lesson.
- **Use:** the Context Builder injects only the lessons relevant to the ticket's scope
  (top-k, within budget), so learning doesn't bloat context.
- **Curation:** a Librarian agent periodically merges duplicates, generalizes,
  and retires lessons that no longer match anything; changes to high-impact rules go to the Board.
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

- **M0 – Foundations:** package, CI, `ModelClient` + fake model, `Executor` + worktree,
  SQLite board with atomic claim + leases (with concurrency tests).
- **M1 – Build loop:** event bus + hooks; Implementer → gates → Reviewer → Fixer with
  TDD red→green verification; merge queue. Tickets written by hand.
- **M2 – Upstream phases:** Spec Writer/Critic, Architect, Decomposer + ticket linter;
  full pipeline on a toy app.
- **M3 – Board:** decision requests + Researcher, web dashboard + CLI, budgets.
- **M4 – Learning:** lesson store, injection into context packs, Librarian, skill promotion.
- **M5 – Hardening:** Docker executor, integration/QA phase, more providers, more language stacks.

M1 is first on purpose: the build loop is the core of the system, and it can be
tested with hand-written tickets before the spec/decomposition agents exist.

## Open questions

1. First target stack for the generated apps (e.g. Python/FastAPI, TypeScript/React)?
   This decides the first gate implementations.
2. Max parallel implementers, and a cost budget per project?
3. Should new lessons apply automatically, or wait for your approval?
4. Dashboard: is a local web UI enough, or do you also want notifications (Slack/email/phone)?
5. Merging: does the harness merge into `main` itself, or open PRs for you?

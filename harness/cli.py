"""Command-line entry point: ``harness <command>``."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

import yaml

from harness.board import BoardError, Ticket, TicketStatus
from harness.config import ConfigError
from harness.project import Project, ProjectError, init_project
from harness.stacks import StackError


def _cmd_init(args: argparse.Namespace) -> int:
    project = init_project(Path(args.path))
    print(f"Initialized harness project in {project.root}")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    project = Project.open(Path(args.project))
    cfg = project.config
    roles = sorted(set(cfg.models.roles) | {args.role} if args.role else set(cfg.models.roles))
    if cfg.models.default is not None:
        print(f"default      {cfg.models.default.provider}:{cfg.models.default.model}")
    for role in roles:
        rm = cfg.model_for(role)
        print(f"{role:<12} {rm.provider}:{rm.model}  (max_tokens={rm.max_tokens})")
    return 0


def _cmd_stacks(args: argparse.Namespace) -> int:
    project = Project.open(Path(args.project))
    stacks = project.stacks()
    if not stacks:
        print("No stacks detected. Set `stacks:` in .harness/config.yaml.")
    for s in stacks:
        print(f"{s.name:<12} {s.description}")
    return 0


def _cmd_ticket_add(args: argparse.Namespace) -> int:
    project = Project.open(Path(args.project))
    data = yaml.safe_load(Path(args.file).read_text())
    items = data if isinstance(data, list) else [data]
    tickets = [Ticket.model_validate(item) for item in items]
    project.board.add_tickets(tickets)
    for t in tickets:
        status = project.board.queue(t.id) if args.queue else TicketStatus.DRAFT
        print(f"{t.id:<10} {status.value:<10} {t.title}")
    return 0


def _cmd_ticket_queue(args: argparse.Namespace) -> int:
    project = Project.open(Path(args.project))
    for tid in args.ids:
        print(f"{tid:<10} {project.board.queue(tid).value}")
    return 0


def _cmd_board(args: argparse.Namespace) -> int:
    project = Project.open(Path(args.project))
    status = TicketStatus(args.status) if args.status else None
    tickets = project.board.tickets(status)
    if args.json:
        print(json.dumps([t.model_dump(mode="json") for t in tickets], indent=2))
        return 0
    if not tickets:
        print("Board is empty.")
    for t in tickets:
        holder = f"  [{t.agent}]" if t.agent else ""
        print(f"{t.id:<10} {t.status.value:<18} {t.title}{holder}")
    return 0


def _cmd_events(args: argparse.Namespace) -> int:
    project = Project.open(Path(args.project))
    for e in project.board.events(after_seq=args.after, limit=args.limit):
        print(f"{e.seq:>6}  {e.ticket_id or '-':<10} {e.type:<26} {json.dumps(e.payload)}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import asyncio

    from harness.core.orchestrator import Orchestrator

    project = Project.open(Path(args.project))
    summary = asyncio.run(
        Orchestrator(project).run(until_idle=not args.watch, timeout_s=args.timeout)
    )
    counts = ", ".join(f"{k}={v}" for k, v in sorted(summary.by_status.items()))
    print(f"{summary.agent_runs} agent runs in {summary.seconds:.0f}s. Tickets: {counts or 'none'}")
    escalated = project.board.tickets(TicketStatus.ESCALATED)
    for t in escalated:
        print(f"  escalated: {t.id} {t.title}")
    return 0


def _idea(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text()
    if args.idea:
        return str(args.idea)
    raise ValueError("describe the product idea as an argument or with --file")


async def _run_phases(
    project: Project, phases: list[str], idea: str | None, args: argparse.Namespace
) -> int:
    import asyncio

    from harness.core.orchestrator import Orchestrator
    from harness.escalation.cli import CliHuman
    from harness.escalation.decisions import BoardHuman, Human
    from harness.phases.pipeline import Pipeline

    human: Human
    server = None
    if args.interactive:
        human = CliHuman()
    else:
        url = f"http://127.0.0.1:{args.port}"
        human = BoardHuman(project.board, where=f"the dashboard ({url})")
        if not args.no_dashboard:
            from harness.web.app import serve

            server = asyncio.create_task(serve(project, port=args.port))
            print(f"Dashboard: {url}")
    try:
        pipeline = Pipeline(project, human)
        for phase in phases:
            if phase == "spec":
                assert idea is not None
                result = await pipeline.spec(idea)
            elif phase == "architecture":
                result = await pipeline.architecture()
            elif phase == "plan":
                result = await pipeline.plan()
            else:
                summary = await Orchestrator(project).run(until_idle=not args.watch)
                counts = ", ".join(f"{k}={v}" for k, v in sorted(summary.by_status.items()))
                print(f"Build loop finished: {counts}")
                continue
            print(f"[{phase}] {result.message}")
            if not result.ok:
                return 1
        return 0
    finally:
        if server is not None:
            server.cancel()


def _cmd_phase(args: argparse.Namespace) -> int:
    import asyncio

    from harness.phases.pipeline import PHASES, PhaseError

    project = Project.open(Path(args.project))
    current = project.board.get_meta("phase", "spec") or "spec"
    if args.phase_command == "status":
        print(f"Current phase: {current}")
        return 0
    if args.phase_command == "start":
        phases = list(PHASES[PHASES.index(current) :])
    else:
        phases = [args.phase_command]
    idea = _idea(args) if "spec" in phases else None
    try:
        return asyncio.run(_run_phases(project, phases, idea, args))
    except PhaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _cmd_decisions(args: argparse.Namespace) -> int:
    project = Project.open(Path(args.project))
    for d in project.board.decisions():
        req, ans = d["request"], d["answer"]
        status = "open" if ans is None else (ans.get("choice") or ans.get("note") or "answered")
        print(
            f"{d['id']:<6} [{d['phase'] or d['ticket_id'] or '-'}] {req['question']}  ->  {status}"
        )
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    import asyncio

    from harness.web.app import serve

    project = Project.open(Path(args.project))
    print(f"Dashboard: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(project, port=args.port))
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    from harness.escalation.cli import parse_choice
    from harness.escalation.decisions import DecisionRequest

    project = Project.open(Path(args.project))
    d = project.board.get_decision(args.id)
    if d["answer"] is not None:
        raise BoardError(f"{args.id} is already answered")
    decision = parse_choice(DecisionRequest.model_validate(d["request"]), " ".join(args.answer))
    project.board.answer_decision(args.id, decision.model_dump())
    print(f"{args.id}: {decision.describe()}")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    project = Project.open(Path(args.project))
    approved = args.reject is None
    project.board.resolve_approval(args.id, approved, args.reject or "")
    print(f"{args.id}: {'approved' if approved else 'changes requested'}")
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    from harness.core.usage import usage_report

    project = Project.open(Path(args.project))
    report = usage_report(project.board, project.config)
    for title, lines in (("role", report.by_role), ("ticket", report.by_ticket)):
        print(f"{'by ' + title:<16} {'runs':>5} {'in':>10} {'out':>10} {'cost':>9}")
        for key, u in sorted(lines.items()):
            tokens_in = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
            print(
                f"{key:<16} {u.runs:>5} {tokens_in:>10} {u.output_tokens:>10} ${u.cost_usd:>8.3f}"
            )
        print()
    print(f"total cost: ${report.total.cost_usd:.3f} over {report.total.runs} agent runs")
    return 0


def _cmd_lessons(args: argparse.Namespace) -> int:
    project = Project.open(Path(args.project))
    statuses = None if args.all else ("promoted", "active", "pending")
    for x in project.board.lessons(statuses=statuses):
        flag = "!" if x["priority"] == "high" else " "
        print(
            f"{x['id']:<6}{flag} {x['status']:<9} {x['scope']:<22} hits={x['hits']:<3} "
            f"uses={x['uses']:<4} {x['rule']}"
        )
    return 0


def _cmd_lesson(args: argparse.Namespace) -> int:
    from harness.learning.learner import validate_scope

    board = Project.open(Path(args.project)).board
    if args.lesson_command == "add":
        lesson_id = board.add_lesson(
            " ".join(args.rule),
            scope=validate_scope(args.scope),
            source="owner",
            priority="high",
        )
        print(f"{lesson_id} added")
    elif args.lesson_command == "edit":
        fields: dict[str, str] = {}
        if args.rule:
            fields["rule"] = " ".join(args.rule)
        if args.scope:
            fields["scope"] = validate_scope(args.scope)
        board.update_lesson(args.id, why="edited by the owner", **fields)
        print(f"{args.id} updated")
    else:
        status = {"retire": "retired", "restore": "active", "promote": "promoted"}[
            args.lesson_command
        ]
        board.update_lesson(args.id, status=status, why="changed by the owner")
        print(f"{args.id} {status}")
    return 0


def _cmd_learn(args: argparse.Namespace) -> int:
    import asyncio

    from harness.learning.learner import Learner

    project = Project.open(Path(args.project))
    n = asyncio.run(Learner(project).sync())
    print(f"processed {n} new observations")
    return _cmd_lessons(argparse.Namespace(project=args.project, all=False))


DOCTOR_ROLES = (
    "spec_writer",
    "spec_critic",
    "architect",
    "decomposer",
    "plan_critic",
    "implementer",
    "reviewer",
    "fixer",
    "researcher",
    "librarian",
)


def _cmd_doctor(args: argparse.Namespace) -> int:
    import asyncio
    import os
    import shutil
    import subprocess

    from harness.agents.runners import CLI_RUNNERS

    project = Project.open(Path(args.project))
    cfg = project.config
    ok = True
    used: dict[tuple[str, str], str] = {}
    for role in DOCTOR_ROLES:
        try:
            rm = cfg.model_for(role)
        except ConfigError as e:
            print(f"{role:<12} ✗ {e}")
            ok = False
            continue
        used.setdefault((rm.provider, rm.model), role)
        print(f"{role:<12} {rm.provider}:{rm.model}")
    print()
    for name, provider in cfg.providers.items():
        if not any(p == name for p, _ in used):
            continue
        extra = provider.model_extra or {}
        if provider.type in CLI_RUNNERS:
            command = str(extra.get("command") or CLI_RUNNERS[provider.type].default_command)
            path = shutil.which(command)
            if path is None:
                print(f"{name:<12} ✗ `{command}` not found on PATH")
                ok = False
                continue
            version = subprocess.run(
                [command, "--version"], capture_output=True, text=True, timeout=30
            ).stdout.strip()
            print(f"{name:<12} ✓ {provider.type}: {path} ({version or 'version unknown'})")
        elif provider.api_key_env:
            has_key = bool(os.environ.get(provider.api_key_env))
            ok = ok and has_key
            mark = "✓" if has_key else "✗"
            print(
                f"{name:<12} {mark} {provider.type}: ${provider.api_key_env} "
                f"{'is set' if has_key else 'is not set'}"
            )
        else:
            print(f"{name:<12} ✓ {provider.type}")
    if args.live:
        print()
        for (prov, model), role in used.items():
            ok = asyncio.run(_live_check(project, role, f"{prov}:{model}")) and ok
    return 0 if ok else 1


async def _live_check(project: Project, role: str, label: str) -> bool:
    """Run a tiny real agent: it must use a harness tool and then finish correctly."""
    from pydantic import BaseModel

    from harness.agents.runners import runner_for_role
    from harness.tools.base import Tool, Toolbox

    class AddIn(BaseModel):
        a: int
        b: int

    class Answer(BaseModel):
        answer: int

    async def add(x: AddIn) -> str:
        return str(x.a + x.b)

    async def check(res: Answer) -> str | None:
        return None if res.answer == 1234 + 4321 else "wrong answer: use the add tool"

    run = await runner_for_role(project.config, role).run(
        system="You are a health check for an agent harness. Use only the tools given.",
        prompt="Use the add tool to add 1234 and 4321, then call finish with the result.",
        toolbox=Toolbox([Tool("add", "Add two integers.", AddIn, add)]),
        result_model=Answer,
        finish_description="Submit the sum.",
        on_finish=check,
        max_turns=6,
        cwd=project.root,
    )
    used_tool = any(getattr(b, "name", "") == "add" for m in run.messages for b in m.content)
    good = run.outcome == "finished" and used_tool
    cost = f", ${run.cost_usd:.4f}" if run.cost_usd is not None else ""
    print(
        f"live {label:<30} {'✓' if good else '✗'} {run.outcome} in {run.turns} tool calls"
        f"{cost}{'' if good else ': ' + (run.reason or 'did not use the tool')}"
    )
    return good


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description=__doc__)
    parser.add_argument("-C", "--project", default=".", help="project directory (default: .)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="make a directory a harness project")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("config", help="show which model each role uses")
    p.add_argument("--role", help="also resolve this role")
    p.set_defaults(func=_cmd_config)

    p = sub.add_parser("stacks", help="show the project's stack profiles")
    p.set_defaults(func=_cmd_stacks)

    ticket = sub.add_parser("ticket", help="manage tickets").add_subparsers(
        dest="ticket_command", required=True
    )
    p = ticket.add_parser("add", help="add ticket(s) from a YAML file")
    p.add_argument("file")
    p.add_argument("--queue", action="store_true", help="also queue them onto the board")
    p.set_defaults(func=_cmd_ticket_add)
    p = ticket.add_parser("queue", help="queue draft ticket(s)")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=_cmd_ticket_queue)

    p = sub.add_parser("board", help="list tickets")
    p.add_argument("--status", choices=[s.value for s in TicketStatus])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_board)

    for name, help_text in (
        ("start", "run all remaining phases, from the spec interview to merged code"),
        ("spec", "phase 1: write and approve the spec sheet"),
        ("architecture", "phase 2: design, contracts and skeleton"),
        ("plan", "phase 3: break the spec into tickets"),
        ("status", "show the current phase"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("idea", nargs="?", help="the product idea (spec/start)")
        p.add_argument("--file", help="read the product idea from a file")
        p.add_argument(
            "--interactive",
            action="store_true",
            help="answer questions in this terminal instead of the dashboard",
        )
        p.add_argument(
            "--no-dashboard",
            action="store_true",
            help="don't serve the dashboard from this process",
        )
        p.add_argument("--port", type=int, default=8765)
        p.add_argument(
            "--watch",
            action="store_true",
            help="keep the build loop running for escalation answers",
        )
        p.set_defaults(func=_cmd_phase, phase_command=name)

    p = sub.add_parser("dashboard", help="serve the Board dashboard on localhost")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=_cmd_dashboard)

    p = sub.add_parser("answer", help="answer a decision: option number [note], or free text")
    p.add_argument("id")
    p.add_argument("answer", nargs="*")
    p.set_defaults(func=_cmd_answer)

    p = sub.add_parser("approve", help="approve a phase gate (or --reject with feedback)")
    p.add_argument("id")
    p.add_argument("--reject", metavar="FEEDBACK", help="request changes with this feedback")
    p.set_defaults(func=_cmd_approve)

    p = sub.add_parser("lessons", help="list the lessons the team has learned")
    p.add_argument("--all", action="store_true", help="include retired lessons")
    p.set_defaults(func=_cmd_lessons)

    lesson = sub.add_parser("lesson", help="add, edit, retire, restore or promote a lesson")
    lesson_sub = lesson.add_subparsers(dest="lesson_command", required=True)
    p = lesson_sub.add_parser("add", help="teach a rule directly")
    p.add_argument("rule", nargs="+")
    p.add_argument(
        "--scope", default="global", help="global | role:<role> | path:<glob> | stack:<name>"
    )
    p.set_defaults(func=_cmd_lesson)
    p = lesson_sub.add_parser("edit", help="change a lesson's rule text or scope")
    p.add_argument("id")
    p.add_argument("rule", nargs="*")
    p.add_argument("--scope")
    p.set_defaults(func=_cmd_lesson)
    for name in ("retire", "restore", "promote"):
        p = lesson_sub.add_parser(name)
        p.add_argument("id")
        p.set_defaults(func=_cmd_lesson)

    p = sub.add_parser("learn", help="learn from new feedback and findings now")
    p.set_defaults(func=_cmd_learn)

    p = sub.add_parser("doctor", help="check each role's model backend (CLI or API)")
    p.add_argument(
        "--live",
        action="store_true",
        help="also run a tiny real task on each backend (uses your model quota)",
    )
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("usage", help="token usage and cost by role and ticket")
    p.set_defaults(func=_cmd_usage)

    p = sub.add_parser("decisions", help="list the Board's decisions")
    p.set_defaults(func=_cmd_decisions)

    p = sub.add_parser("run", help="run the build loop until no work is left")
    p.add_argument("--watch", action="store_true", help="keep running and wait for new work")
    p.add_argument("--timeout", type=float, default=None, help="stop after this many seconds")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("events", help="show the event log")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=_cmd_events)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init" and args.path == "." and args.project != ".":
        args.path = args.project
    try:
        return int(args.func(args))
    except (BoardError, ConfigError, ProjectError, StackError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

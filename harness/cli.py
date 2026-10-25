"""Command-line entry point: ``harness <command>``."""

from __future__ import annotations

import argparse
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


async def _run_phases(project: Project, phases: list[str], idea: str | None) -> int:
    from harness.core.orchestrator import Orchestrator
    from harness.escalation.cli import CliHuman
    from harness.phases.pipeline import Pipeline

    pipeline = Pipeline(project, CliHuman())
    for phase in phases:
        if phase == "spec":
            assert idea is not None
            result = await pipeline.spec(idea)
        elif phase == "architecture":
            result = await pipeline.architecture()
        elif phase == "plan":
            result = await pipeline.plan()
        else:
            summary = await Orchestrator(project).run()
            counts = ", ".join(f"{k}={v}" for k, v in sorted(summary.by_status.items()))
            print(f"Build loop finished: {counts}")
            continue
        print(f"[{phase}] {result.message}")
        if not result.ok:
            return 1
    return 0


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
        return asyncio.run(_run_phases(project, phases, idea))
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
        p.set_defaults(func=_cmd_phase, phase_command=name)

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

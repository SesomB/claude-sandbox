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

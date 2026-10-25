"""The Board: SQLite-backed ticket store with atomic claims, leases, and an event log.

Safe for many agents in many processes: every state change runs inside a
``BEGIN IMMEDIATE`` transaction, so two agents can never claim the same ticket.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.board.models import (
    CLAIM_TRANSITIONS,
    COMPLETE_TRANSITIONS,
    ESCALATION_RESOLUTIONS,
    HELD_STATUSES,
    LEASE_EXPIRY_TRANSITIONS,
    TERMINAL_STATUSES,
    Ticket,
    TicketStatus,
)

S = TicketStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT NOT NULL UNIQUE,
    spec          TEXT NOT NULL,
    status        TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    agent         TEXT,
    lease_until   REAL,
    review_rounds INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status, priority DESC, seq);
CREATE TABLE IF NOT EXISTS ticket_deps (
    ticket_id  TEXT NOT NULL REFERENCES tickets(id),
    depends_on TEXT NOT NULL REFERENCES tickets(id),
    PRIMARY KEY (ticket_id, depends_on)
);
CREATE TABLE IF NOT EXISTS file_ownership (
    path      TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id)
);
CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    ticket_id TEXT,
    type      TEXT NOT NULL,
    payload   TEXT NOT NULL
);
"""


class BoardError(Exception):
    pass


class OwnershipConflict(BoardError):
    """Two open tickets would own the same file."""


class InvalidTransition(BoardError):
    pass


class NotHolder(BoardError):
    """The agent does not hold the ticket (never claimed it, or its lease expired)."""


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    ticket_id: str | None
    type: str
    payload: dict[str, Any]


class Board:
    def __init__(self, db_path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.db_path = db_path
        self._clock = clock
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    # -- plumbing ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes the DB write lock up front."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _emit(
        self, conn: sqlite3.Connection, ticket_id: str | None, type: str, **payload: Any
    ) -> None:
        conn.execute(
            "INSERT INTO events (ts, ticket_id, type, payload) VALUES (?, ?, ?, ?)",
            (self._clock(), ticket_id, type, json.dumps(payload, sort_keys=True)),
        )

    def _set_status(
        self,
        conn: sqlite3.Connection,
        ticket_id: str,
        old: TicketStatus,
        new: TicketStatus,
        *,
        agent: str | None = None,
        lease_until: float | None = None,
        **payload: Any,
    ) -> None:
        conn.execute(
            "UPDATE tickets SET status=?, agent=?, lease_until=?, updated_at=? WHERE id=?",
            (new.value, agent, lease_until, self._clock(), ticket_id),
        )
        self._emit(conn, ticket_id, f"ticket.{new.value}", previous=old.value, **payload)

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> Ticket:
        data = json.loads(row["spec"])
        data.update(
            status=row["status"],
            agent=row["agent"],
            lease_until=row["lease_until"],
            review_rounds=row["review_rounds"],
        )
        return Ticket.model_validate(data)

    def _status_of(self, conn: sqlite3.Connection, ticket_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT status, agent, lease_until FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise BoardError(f"unknown ticket {ticket_id!r}")
        return row  # type: ignore[no-any-return]

    def _deps_done(self, conn: sqlite3.Connection, ticket_id: str) -> bool:
        row = conn.execute(
            """SELECT COUNT(*) FROM ticket_deps d JOIN tickets t ON t.id = d.depends_on
               WHERE d.ticket_id=? AND t.status != ?""",
            (ticket_id, S.DONE.value),
        ).fetchone()
        return bool(row[0] == 0)

    # -- authoring ----------------------------------------------------------------

    def add_tickets(self, tickets: Sequence[Ticket]) -> None:
        """Add tickets as drafts. All-or-nothing: validates ids, deps, cycles, ownership."""
        ids = [t.id for t in tickets]
        if len(set(ids)) != len(ids):
            raise BoardError("duplicate ticket ids in batch")
        batch = {t.id: t for t in tickets}
        _check_no_cycles(batch)
        now = self._clock()
        with self._tx() as conn:
            for t in tickets:
                if conn.execute("SELECT 1 FROM tickets WHERE id=?", (t.id,)).fetchone():
                    raise BoardError(f"ticket {t.id!r} already exists")
                for dep in t.depends_on:
                    if (
                        dep not in batch
                        and not conn.execute("SELECT 1 FROM tickets WHERE id=?", (dep,)).fetchone()
                    ):
                        raise BoardError(f"{t.id} depends on unknown ticket {dep!r}")
            for t in tickets:
                conn.execute(
                    """INSERT INTO tickets (id, spec, status, priority, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (t.id, json.dumps(t.spec()), S.DRAFT.value, t.priority, now),
                )
            for t in tickets:
                conn.executemany(
                    "INSERT INTO ticket_deps (ticket_id, depends_on) VALUES (?, ?)",
                    [(t.id, d) for d in t.depends_on],
                )
                for path in t.owned_files:
                    owner = conn.execute(
                        "SELECT ticket_id FROM file_ownership WHERE path=?", (path,)
                    ).fetchone()
                    if owner is not None:
                        raise OwnershipConflict(
                            f"{t.id} wants to own {path!r}, already owned by {owner[0]}"
                        )
                    conn.execute(
                        "INSERT INTO file_ownership (path, ticket_id) VALUES (?, ?)",
                        (path, t.id),
                    )
                self._emit(conn, t.id, "ticket.draft")

    def queue(self, ticket_id: str) -> TicketStatus:
        """Move a draft onto the board: ``ready`` if its deps are done, else ``waiting``."""
        with self._tx() as conn:
            current = S(self._status_of(conn, ticket_id)["status"])
            if current is not S.DRAFT:
                raise InvalidTransition(f"{ticket_id} is {current}, only drafts can be queued")
            new = S.READY if self._deps_done(conn, ticket_id) else S.WAITING
            self._set_status(conn, ticket_id, current, new)
            return new

    # -- claiming & leases ----------------------------------------------------------

    def claim(
        self,
        agent: str,
        from_status: TicketStatus,
        *,
        lease_s: float = 900.0,
        ticket_id: str | None = None,
    ) -> Ticket | None:
        """Atomically take the highest-priority ticket in ``from_status`` (or a specific one).

        Returns the claimed ticket, or ``None`` if nothing is claimable.
        """
        if from_status not in CLAIM_TRANSITIONS:
            raise InvalidTransition(f"tickets in {from_status} cannot be claimed")
        to_status = CLAIM_TRANSITIONS[from_status]
        with self._tx() as conn:
            if ticket_id is None:
                row = conn.execute(
                    "SELECT id FROM tickets WHERE status=? ORDER BY priority DESC, seq LIMIT 1",
                    (from_status.value,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM tickets WHERE status=? AND id=?",
                    (from_status.value, ticket_id),
                ).fetchone()
            if row is None:
                return None
            claimed_id: str = row["id"]
            lease_until = self._clock() + lease_s
            self._set_status(
                conn,
                claimed_id,
                from_status,
                to_status,
                agent=agent,
                lease_until=lease_until,
                agent_name=agent,
            )
            ticket_row = conn.execute("SELECT * FROM tickets WHERE id=?", (claimed_id,)).fetchone()
            return self._row_to_ticket(ticket_row)

    def _require_holder(self, conn: sqlite3.Connection, ticket_id: str, agent: str) -> TicketStatus:
        row = self._status_of(conn, ticket_id)
        status = S(row["status"])
        if (
            status not in HELD_STATUSES
            or row["agent"] != agent
            or row["lease_until"] is None
            or row["lease_until"] < self._clock()
        ):
            raise NotHolder(f"{agent} does not hold {ticket_id} (status {status})")
        return status

    def heartbeat(self, ticket_id: str, agent: str, *, lease_s: float = 900.0) -> None:
        """Extend the agent's lease. Raises NotHolder if it was lost."""
        with self._tx() as conn:
            self._require_holder(conn, ticket_id, agent)
            conn.execute(
                "UPDATE tickets SET lease_until=? WHERE id=?",
                (self._clock() + lease_s, ticket_id),
            )

    def complete(self, ticket_id: str, agent: str, to_status: TicketStatus, **payload: Any) -> None:
        """The holding agent finishes its step and hands the ticket on."""
        with self._tx() as conn:
            current = self._require_holder(conn, ticket_id, agent)
            if to_status not in COMPLETE_TRANSITIONS[current]:
                raise InvalidTransition(f"{ticket_id}: {current} -> {to_status} not allowed")
            if current is S.REVIEWING and to_status is S.CHANGES_REQUESTED:
                conn.execute(
                    "UPDATE tickets SET review_rounds = review_rounds + 1 WHERE id=?",
                    (ticket_id,),
                )
            self._set_status(conn, ticket_id, current, to_status, agent_name=agent, **payload)
            if to_status is S.DONE:
                self._on_terminal(conn, ticket_id)

    def expire_leases(self) -> list[str]:
        """Return tickets with expired leases to their claimable status."""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT id, status, agent FROM tickets WHERE lease_until IS NOT NULL "
                "AND lease_until < ?",
                (self._clock(),),
            ).fetchall()
            expired = []
            for row in rows:
                current = S(row["status"])
                self._set_status(
                    conn,
                    row["id"],
                    current,
                    LEASE_EXPIRY_TRANSITIONS[current],
                    reason="lease_expired",
                    agent_name=row["agent"],
                )
                expired.append(row["id"])
            return expired

    # -- escalation -------------------------------------------------------------------

    def escalate(self, ticket_id: str, reason: str, **payload: Any) -> None:
        """Put a ticket in front of the Board. Any non-terminal ticket can be escalated."""
        with self._tx() as conn:
            current = S(self._status_of(conn, ticket_id)["status"])
            if current in TERMINAL_STATUSES or current is S.ESCALATED:
                raise InvalidTransition(f"{ticket_id} is {current}, cannot escalate")
            self._set_status(conn, ticket_id, current, S.ESCALATED, reason=reason, **payload)

    def resolve_escalation(self, ticket_id: str, to_status: TicketStatus, **payload: Any) -> None:
        if to_status not in ESCALATION_RESOLUTIONS:
            raise InvalidTransition(f"cannot resolve an escalation to {to_status}")
        with self._tx() as conn:
            current = S(self._status_of(conn, ticket_id)["status"])
            if current is not S.ESCALATED:
                raise InvalidTransition(f"{ticket_id} is {current}, not escalated")
            if to_status is S.READY and not self._deps_done(conn, ticket_id):
                to_status = S.WAITING
            self._set_status(conn, ticket_id, current, to_status, **payload)
            if to_status is S.CANCELLED:
                self._on_terminal(conn, ticket_id)

    def _on_terminal(self, conn: sqlite3.Connection, ticket_id: str) -> None:
        """Free the ticket's files; when done, promote dependents whose deps are all done."""
        conn.execute("DELETE FROM file_ownership WHERE ticket_id=?", (ticket_id,))
        if S(self._status_of(conn, ticket_id)["status"]) is not S.DONE:
            return
        dependents = conn.execute(
            """SELECT t.id FROM ticket_deps d JOIN tickets t ON t.id = d.ticket_id
               WHERE d.depends_on=? AND t.status=?""",
            (ticket_id, S.WAITING.value),
        ).fetchall()
        for row in dependents:
            if self._deps_done(conn, row["id"]):
                self._set_status(conn, row["id"], S.WAITING, S.READY, unblocked_by=ticket_id)

    # -- queries ------------------------------------------------------------------------

    def get(self, ticket_id: str) -> Ticket:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None:
            raise BoardError(f"unknown ticket {ticket_id!r}")
        return self._row_to_ticket(row)

    def tickets(self, status: TicketStatus | None = None) -> list[Ticket]:
        with closing(self._connect()) as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM tickets ORDER BY seq").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tickets WHERE status=? ORDER BY priority DESC, seq",
                    (status.value,),
                ).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    def owner_of(self, path: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT ticket_id FROM file_ownership WHERE path=?", (path,)
            ).fetchone()
        return None if row is None else str(row[0])

    def events(self, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after_seq, limit)
            ).fetchall()
        return [
            Event(r["seq"], r["ts"], r["ticket_id"], r["type"], json.loads(r["payload"]))
            for r in rows
        ]


def _check_no_cycles(batch: dict[str, Ticket]) -> None:
    """Existing tickets can't depend on new ones, so only the batch can form a cycle."""
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(tid: str, path: list[str]) -> None:
        if tid in done or tid not in batch:
            return
        if tid in visiting:
            cycle = path[path.index(tid) :] + [tid]
            raise BoardError(f"dependency cycle: {' -> '.join(cycle)}")
        visiting.add(tid)
        for dep in batch[tid].depends_on:
            visit(dep, path + [tid])
        visiting.discard(tid)
        done.add(tid)

    for tid in batch:
        visit(tid, [])

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from harness.board import (
    Board,
    BoardError,
    InvalidTransition,
    NotHolder,
    OwnershipConflict,
    Ticket,
    TicketStatus,
)
from tests.conftest import FakeClock

S = TicketStatus


def t(
    id: str, *, deps: list[str] | None = None, owns: list[str] | None = None, prio: int = 0
) -> Ticket:
    return Ticket(
        id=id, title=f"ticket {id}", depends_on=deps or [], owned_files=owns or [], priority=prio
    )


@pytest.fixture
def board(tmp_path: Path, clock: FakeClock) -> Board:
    return Board(tmp_path / "board.db", clock=clock)


def test_add_and_queue(board: Board) -> None:
    board.add_tickets([t("A"), t("B", deps=["A"])])
    assert board.get("A").status is S.DRAFT
    assert board.queue("A") is S.READY
    assert board.queue("B") is S.WAITING
    with pytest.raises(InvalidTransition):
        board.queue("A")


def test_add_validation(board: Board) -> None:
    with pytest.raises(BoardError, match="unknown ticket"):
        board.add_tickets([t("A", deps=["Z"])])
    with pytest.raises(BoardError, match="cycle"):
        board.add_tickets([t("A", deps=["B"]), t("B", deps=["A"])])
    with pytest.raises(BoardError, match="duplicate"):
        board.add_tickets([t("A"), t("A")])
    board.add_tickets([t("A")])
    with pytest.raises(BoardError, match="already exists"):
        board.add_tickets([t("A")])
    with pytest.raises(ValueError):
        t("C", owns=["../x.py"])


def test_ownership_conflict_is_all_or_nothing(board: Board) -> None:
    board.add_tickets([t("A", owns=["src/a.py"])])
    with pytest.raises(OwnershipConflict, match="already owned by A"):
        board.add_tickets([t("B", owns=["src/b.py"]), t("C", owns=["./src/a.py"])])
    assert [x.id for x in board.tickets()] == ["A"]
    assert board.owner_of("src/b.py") is None


def walk_to_done(board: Board, tid: str) -> None:
    assert board.claim("impl", S.READY, ticket_id=tid) is not None
    board.complete(tid, "impl", S.AWAITING_REVIEW)
    assert board.claim("rev", S.AWAITING_REVIEW, ticket_id=tid) is not None
    board.complete(tid, "rev", S.APPROVED)
    assert board.claim("merger", S.APPROVED, ticket_id=tid) is not None
    board.complete(tid, "merger", S.DONE)


def test_full_lifecycle_with_fix_loop(board: Board) -> None:
    board.add_tickets([t("A", owns=["a.py"])])
    board.queue("A")
    claimed = board.claim("impl-1", S.READY)
    assert claimed is not None and claimed.status is S.IMPLEMENTING and claimed.agent == "impl-1"
    board.complete("A", "impl-1", S.AWAITING_REVIEW)
    board.claim("rev-1", S.AWAITING_REVIEW)
    board.complete("A", "rev-1", S.CHANGES_REQUESTED, findings=2)
    assert board.get("A").review_rounds == 1
    board.claim("fix-1", S.CHANGES_REQUESTED)
    board.complete("A", "fix-1", S.AWAITING_REVIEW)
    board.claim("rev-2", S.AWAITING_REVIEW)
    board.complete("A", "rev-2", S.APPROVED)
    board.claim("merger", S.APPROVED)
    board.complete("A", "merger", S.DONE)
    final = board.get("A")
    assert final.status is S.DONE and final.agent is None
    assert board.owner_of("a.py") is None  # ownership released

    types = [e.type for e in board.events()]
    assert types == [
        "ticket.draft",
        "ticket.ready",
        "ticket.implementing",
        "ticket.awaiting_review",
        "ticket.reviewing",
        "ticket.changes_requested",
        "ticket.fixing",
        "ticket.awaiting_review",
        "ticket.reviewing",
        "ticket.approved",
        "ticket.merging",
        "ticket.done",
    ]
    assert board.events()[5].payload["findings"] == 2


def test_invalid_completion(board: Board) -> None:
    board.add_tickets([t("A")])
    board.queue("A")
    board.claim("impl", S.READY)
    with pytest.raises(InvalidTransition):
        board.complete("A", "impl", S.DONE)
    with pytest.raises(NotHolder):
        board.complete("A", "someone-else", S.AWAITING_REVIEW)
    with pytest.raises(InvalidTransition):
        board.claim("x", S.DONE)


def test_dependencies_unblock_when_done(board: Board) -> None:
    board.add_tickets([t("A"), t("B"), t("C", deps=["A", "B"])])
    for tid in "ABC":
        board.queue(tid)
    walk_to_done(board, "A")
    assert board.get("C").status is S.WAITING
    walk_to_done(board, "B")
    assert board.get("C").status is S.READY


def test_claim_order_by_priority(board: Board) -> None:
    board.add_tickets([t("low"), t("high", prio=5), t("mid", prio=1)])
    for tid in ("low", "high", "mid"):
        board.queue(tid)
    got = [board.claim(f"a{i}", S.READY) for i in range(4)]
    assert [g.id if g else None for g in got] == ["high", "mid", "low", None]


def test_lease_expiry_and_heartbeat(board: Board, clock: FakeClock) -> None:
    board.add_tickets([t("A")])
    board.queue("A")
    board.claim("impl", S.READY, lease_s=60)
    clock.advance(50)
    board.heartbeat("A", "impl", lease_s=60)
    clock.advance(50)
    assert board.expire_leases() == []
    clock.advance(20)
    assert board.expire_leases() == ["A"]
    assert board.get("A").status is S.READY
    with pytest.raises(NotHolder):
        board.complete("A", "impl", S.AWAITING_REVIEW)
    with pytest.raises(NotHolder):
        board.heartbeat("A", "impl")


def test_expired_lease_cannot_complete_even_before_sweep(board: Board, clock: FakeClock) -> None:
    board.add_tickets([t("A")])
    board.queue("A")
    board.claim("impl", S.READY, lease_s=10)
    clock.advance(11)
    with pytest.raises(NotHolder):
        board.complete("A", "impl", S.AWAITING_REVIEW)


def test_escalation(board: Board) -> None:
    board.add_tickets([t("A", owns=["a.py"]), t("B", deps=["A"])])
    board.queue("A")
    board.queue("B")
    board.claim("impl", S.READY)
    board.escalate("A", "spec gap", question="hash tokens?")
    assert board.get("A").status is S.ESCALATED and board.get("A").agent is None
    with pytest.raises(InvalidTransition):
        board.escalate("A", "again")
    board.resolve_escalation("A", S.READY, decision="D-1")
    assert board.get("A").status is S.READY

    board.escalate("B", "not needed")
    board.resolve_escalation("B", S.READY)
    assert board.get("B").status is S.WAITING  # deps still not done

    board.escalate("A", "drop it")
    board.resolve_escalation("A", S.CANCELLED)
    assert board.owner_of("a.py") is None
    with pytest.raises(InvalidTransition):
        board.resolve_escalation("A", S.DONE)


def _claim_worker(db: str, agent: str, out: mp.Queue[list[str]]) -> None:  # type: ignore[type-arg]
    board = Board(Path(db))
    got = []
    while (ticket := board.claim(agent, S.READY)) is not None:
        got.append(ticket.id)
    out.put(got)


def test_concurrent_claims_never_double_assign(tmp_path: Path) -> None:
    db = tmp_path / "board.db"
    board = Board(db)
    ids = [f"T-{i}" for i in range(60)]
    board.add_tickets([t(i) for i in ids])
    for i in ids:
        board.queue(i)

    ctx = mp.get_context("spawn")
    out: mp.Queue[list[str]] = ctx.Queue()  # type: ignore[assignment]
    procs = [ctx.Process(target=_claim_worker, args=(str(db), f"a{n}", out)) for n in range(8)]
    for p in procs:
        p.start()
    results = [out.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)

    claimed = [tid for r in results for tid in r]
    assert sorted(claimed) == sorted(ids)  # every ticket exactly once
    assert len(set(claimed)) == len(claimed)
    assert all(x.status is S.IMPLEMENTING and x.agent for x in board.tickets())

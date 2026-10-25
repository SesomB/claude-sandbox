from __future__ import annotations

from pathlib import Path

import pytest

from harness.context.builder import ContextBudgetExceeded, extract_sections, implementer_pack
from harness.tools.shared import SharedEditError, SharedOp, apply_op, replay
from tests.helpers import ticket

SPEC = """\
# Spec

## US-7 Password reset

Users can reset their password.

### Details
Tokens expire.

## US-8 Something else

- AC-7.1: tokens expire after 30 minutes
  (measured from creation)
- AC-7.2: expired tokens are rejected
- AC-7: umbrella criterion
"""


def test_extract_sections() -> None:
    found = extract_sections(SPEC, ["US-7", "AC-7.1", "AC-7", "NOPE"])
    assert found["US-7"].startswith("## US-7") and "Tokens expire." in found["US-7"]
    assert "US-8" not in found["US-7"]
    assert found["AC-7.1"] == "- AC-7.1: tokens expire after 30 minutes\n  (measured from creation)"
    assert found["AC-7"] == "- AC-7: umbrella criterion"  # not confused with AC-7.1
    assert "NOPE" not in found


def test_implementer_pack_contents_and_budget(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/spec.md").write_text(SPEC)
    (tmp_path / "a.py").write_text("A = 1\n")
    (tmp_path / "unrelated.py").write_text("SECRET = 1\n")
    t = ticket("T-1", spec_refs=["AC-7.2"], owned_files=["a.py", "b.py"], acceptance=["works"])
    pack = implementer_pack(t, tmp_path, 10_000, lessons=["Never use print for logging"])
    assert "AC-7.2: expired tokens are rejected" in pack
    assert "AC-7.1" not in pack and "SECRET" not in pack  # only what the ticket references
    assert "A = 1" in pack and "b.py (owned, does not exist yet)" in pack
    assert "Never use print for logging" in pack
    with pytest.raises(ContextBudgetExceeded):
        implementer_pack(t, tmp_path, 20)


def test_shared_ops() -> None:
    assert apply_op(None, SharedOp(op="append", text="a\n")) == "a\n"
    assert apply_op("x", SharedOp(op="append", text="a\n")) == "x\na\n"
    assert apply_op("x\na\n", SharedOp(op="append", text="a\n")) == "x\na\n"  # idempotent
    assert apply_op("k = 1\n", SharedOp(op="replace", old="1", text="2")) == "k = 2\n"
    with pytest.raises(SharedEditError, match="occurs 2 times"):
        apply_op("1 1", SharedOp(op="replace", old="1", text="2"))
    with pytest.raises(SharedEditError, match="not found"):
        apply_op("abc", SharedOp(op="replace", old="zzz", text="q"))
    assert (
        replay("base\n", [{"op": "append", "text": "x\n"}, {"op": "append", "text": "y\n"}])
        == "base\nx\ny\n"
    )

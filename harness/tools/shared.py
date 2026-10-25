"""Replayable edits to shared files (build files, dependency manifests, ...).

No ticket owns a shared file. Agents change one through these operations, which
are recorded on the Board so the merge queue can re-apply them on top of the
latest ``main`` when parallel tickets touched the same shared file.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class SharedEditError(Exception):
    pass


class SharedOp(BaseModel):
    op: Literal["append", "replace"]
    text: str  # text to append, or the replacement text
    old: str | None = None  # for "replace": the exact text to replace (must occur once)


def apply_op(content: str | None, op: SharedOp) -> str:
    """Apply ``op`` to a file's current content (``None`` if it doesn't exist yet)."""
    current = content or ""
    if op.op == "append":
        if current.endswith(op.text):  # already applied (idempotent re-apply)
            return current
        sep = "" if not current or current.endswith("\n") else "\n"
        return current + sep + op.text
    if not op.old:
        raise SharedEditError("replace needs `old`")
    count = current.count(op.old)
    if count == 0:
        if op.text in current:  # already applied
            return current
        raise SharedEditError(f"text to replace not found: {op.old[:200]!r}")
    if count > 1:
        raise SharedEditError(f"text to replace occurs {count} times; include more context")
    return current.replace(op.old, op.text, 1)


def replay(content: str | None, ops: list[dict[str, Any]]) -> str:
    for raw in ops:
        content = apply_op(content, SharedOp.model_validate(raw))
    return content or ""

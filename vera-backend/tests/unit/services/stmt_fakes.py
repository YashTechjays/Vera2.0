"""Shared helpers for the fake `AsyncSession`s in this package's unit tests.

`bound_value` reads SQLAlchemy's internal statement shape (`whereclause`, `clauses`,
`clause.left.name`), so it lives once — a version bump that changes that shape is then one
edit, not a hunt for every copy.
"""

from __future__ import annotations

from typing import Any


def bound_value(stmt: Any, column_name: str) -> Any:
    """Pull a bound literal (e.g. `FieldAnswer.field_path == reference_field`) out of a
    statement's WHERE clause by column name — lets a fake resolve which row a
    `select(...).where(...)` is asking for.

    getattr on `left`: a nested clause list (the dispatcher candidates query's OR) has no
    sides of its own, and skipping it is right — the columns we resolve on are always
    compared at the top level."""
    where = stmt.whereclause
    clauses = where.clauses if hasattr(where, "clauses") else [where]
    for clause in clauses:
        if getattr(getattr(clause, "left", None), "name", None) == column_name:
            return clause.right.value
    return None

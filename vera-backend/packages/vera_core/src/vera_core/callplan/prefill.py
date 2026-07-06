"""DB-known values → the compiler's prefill map (control-plane side, pure).

Returns raw `field_path → value` strings. PHI tokenization was removed (dev
simplification), so the values flow into the plan as-is — synthetic-data-only
until a protection mechanism is reintroduced (see adr/devops-todo.md #8).
"""

from collections.abc import Mapping


def _as_text(raw: object) -> str:
    """Stringify a stored scalar the way the schema's answers are authored
    (bools as Yes/No so compile-time rule comparisons line up)."""
    if raw is True:
        return "Yes"
    if raw is False:
        return "No"
    return str(raw)


def build_prefill(values: Mapping[str, object]) -> dict[str, str]:
    """`{field_path: raw scalar}` → `{field_path: normalized text}`, dropping
    empty/None values."""
    prefill: dict[str, str] = {}
    for field_path, raw in values.items():
        if raw is None:
            continue
        text = _as_text(raw).strip()
        if text:
            prefill[field_path] = text
    return prefill

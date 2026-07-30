"""Schema-driven ``{{token}}`` placeholder resolution, reusable by any agent prompt.

A ``{{token}}`` is either a ``system_fields`` handle or a ``context``-role leaf's path (DSL spec
§4; the same namespace :func:`prompting.validate_prompt_document` validates against).
:func:`resolve_field_path` maps a token to its schema field path (``system_fields`` first, then
``context``); :func:`resolve_prompt` fills every ``{{token}}`` in a text via a caller-supplied
value getter. Both are pure — no DB, no I/O — so the control plane (values from ``field_answer``)
and the worker (values from shipped dispatch metadata) share the same substitution.
"""

from __future__ import annotations

from collections.abc import Callable

from vera_core.forms.dsl import PLACEHOLDER_RE, FormSchemaDoc


class UnknownPlaceholderError(ValueError):
    def __init__(self, token: str) -> None:
        super().__init__(f"unknown schema placeholder {{{{{token}}}}}")
        self.token = token


def resolve_field_path(doc: FormSchemaDoc, token: str) -> str:
    """Map a ``{{token}}`` to its root-anchored schema path: a ``system_fields`` handle first,
    then a ``context``-role leaf whose path IS the token. Raises :class:`UnknownPlaceholderError`
    when the token is not a placeholder this schema defines — fail early, never silently drop it."""
    system_fields = doc.system_fields or {}
    if token in system_fields:
        return system_fields[token]
    for path, leaf in doc.leaf_items():
        if path == token and leaf.role == "context":
            return path
    raise UnknownPlaceholderError(token)


def placeholder_tokens(doc: FormSchemaDoc) -> set[str]:
    """Every token this schema resolves — ``system_fields`` handles plus ``context``-leaf paths."""
    return set(doc.system_fields or {}) | {
        path for path, leaf in doc.leaf_items() if leaf.role == "context"
    }


def resolve_prompt(text: str, resolve: Callable[[str], str | None]) -> str:
    """Replace every ``{{token}}`` in ``text`` with ``resolve(token)``. A token that resolves to
    ``None``/empty is dropped — ``resolve`` supplies any default/neutral fallback itself."""
    return PLACEHOLDER_RE.sub(lambda m: resolve(m.group(1)) or "", text)

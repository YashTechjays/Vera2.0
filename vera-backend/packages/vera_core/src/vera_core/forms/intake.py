"""Pure, DB-free helpers for IBV patient-form intake.

The intake endpoint (`control_plane.api.v1.patient_forms`) uses these to validate
the minimum required fields, flatten the nested `intake_payload` into per-field
answers, and promote the searchable identifier columns. Kept free of SQLAlchemy /
FastAPI so they unit-test without a database.

PHI note: these return field **paths** and (for promotion) typed values the caller
persists — never log the values. Validation errors carry paths only.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import PATH_PREFIX, FormSchemaDoc, parse_date_format

# Legacy v1 section the required-fields fallback reads structurally.
_PATIENT_INFO = "patient_information"

# E.164: a leading + and 1-15 digits, first digit non-zero. Intended single source of
# truth — control_plane.queueability is slated to re-import this instead of defining
# its own copy (see the insurance-phone-auto-format plan's de-duplication task).
E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


class InvalidIntakeValue(ValueError):
    """A provided value failed normalization (e.g. an unparseable date). Carries
    the offending field **path** so the endpoint can surface it without echoing the
    value (PHI)."""

    def __init__(self, field_path: str, reason: str = "invalid value") -> None:
        self.field_path = field_path
        super().__init__(f"{field_path}: {reason}")


def _is_empty(value: object) -> bool:
    """True for None, blank/whitespace strings, and empty dict/list — the values
    that count as "not provided" at intake."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (dict, list)):
        return len(value) == 0
    return False


def required_intake_fields(schema_json: dict[str, Any]) -> list[str]:
    """Every field a clinic must supply at intake. Data-driven from `schema_json`,
    version-gated on `dsl_version`:
    v2 — root-anchored (`sections.…`) targets of the schema's own `system_fields`
    block, deduplicated (two handles may alias the same leaf), excluding any
    target whose leaf carries a `default` (counts as filled even if absent). A
    `system_fields` entry is the only signal for creation-time requiredness: it's
    the schema's declaration that downstream integrations depend on the value, so
    intake is the only point it can be guaranteed present. The leaf's own
    `required`/`role` govern voice collection and gap analysis ("form filling") —
    a separate, later concern that has no bearing on what a schema needs at
    creation.
    v1 — the legacy `patient_information` section's `required` list only."""
    if is_v2(schema_json):
        doc = FormSchemaDoc.model_validate(schema_json)
        leaves = dict(doc.leaf_items())
        system_fields = doc.system_fields or {}
        required_v2: list[str] = []
        for path in system_fields.values():
            if leaves[path].default is None and path not in required_v2:
                required_v2.append(path)
        return required_v2
    for section in schema_json.get("sections", []):
        if section.get("section_key") == _PATIENT_INFO:
            required: list[str] = list(section.get("required", []))
            return required
    return []


def resolve_path(payload: dict[str, Any], path: str) -> Any:
    """Look up a root-anchored `sections.<key>...` path inside an intake payload
    nested by section key (the payload itself has no `sections` root — see
    `iter_leaf_answers`)."""
    node: Any = payload
    for part in path.removeprefix(PATH_PREFIX).split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def missing_required(payload: dict[str, Any], schema_json: dict[str, Any]) -> list[str]:
    """Paths of every `required_intake_fields` target absent/blank in `payload`
    (root-anchored `sections.…` paths for v2 documents). Names only — never the
    values."""
    if is_v2(schema_json):
        return [
            path
            for path in required_intake_fields(schema_json)
            if _is_empty(resolve_path(payload, path))
        ]
    values = payload.get(_PATIENT_INFO)
    values = values if isinstance(values, dict) else {}
    return [
        f"{_PATIENT_INFO}.{field}"
        for field in required_intake_fields(schema_json)
        if _is_empty(values.get(field))
    ]


def iter_leaf_answers(payload: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """Flatten `payload` into `(dotted_path, value)` for every non-empty scalar
    leaf — each becomes one `field_answer` row. Empty leaves and empty objects are
    skipped; nested objects recurse to arbitrary depth."""

    def walk(node: Any, prefix: str) -> Iterator[tuple[str, Any]]:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{prefix}.{key}" if prefix else key
                yield from walk(value, child)
        elif not _is_empty(node):
            yield prefix, node

    yield from walk(payload, "")


@dataclass(frozen=True)
class PromotedIdentifiers:
    """The typed `patient_form` columns a schema's `promoted_fields` maps to — both the
    searchable identifiers and the worklist display fields. Every schema maps every
    column (PromotedFields is total), but a mapped value can still come back `None`
    (payload omitted a defaulted leaf; chart_number's "N/A" normalization)."""

    patient_name: str | None = None
    patient_dob: date | None = None
    appointment_date: date | None = None
    chart_number: str | None = None
    appointment_type: str | None = None
    member_id: str | None = None
    insurance_provider: str | None = None
    insurance_provider_phone_number: str | None = None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_phone_prefix(value: Any) -> Any:
    """Trim and prepend '+' to a non-empty string phone value that doesn't already
    start with one — the only reformatting this applies (no stripping of internal
    separators, so a value with spaces/dashes still fails `E164_RE` downstream,
    unchanged from before). Non-string/blank values pass through untouched, so this is
    safe to call unconditionally on any raw answer value."""
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if not trimmed:
        return value
    return trimmed if trimmed.startswith("+") else f"+{trimmed}"


def phone_promoted_paths(doc: FormSchemaDoc) -> set[str]:
    """Root-anchored paths among `doc.promoted_fields` whose leaf is typed `"phone"` —
    the dynamic, schema-driven set this fix touches, resolved from the leaf's declared
    type rather than a hardcoded column/path name, so a future promoted phone column is
    covered with no code change."""
    leaves = dict(doc.leaf_items())
    return {
        path
        for _column, path in doc.promoted_fields.items()
        if (leaf := leaves.get(path)) is not None and leaf.type == "phone"
    }


def normalize_phone_answers(
    answers: list[tuple[str, Any]], doc: FormSchemaDoc
) -> list[tuple[str, Any]]:
    """Prefix '+' onto any flattened `(path, value)` answer whose path is a
    phone-typed promoted field (`phone_promoted_paths`) — applied before
    `field_answer` rows are built, so storage matches what `promote_columns` derives
    for the same path. Non-phone paths pass through untouched."""
    phone_paths = phone_promoted_paths(doc)
    if not phone_paths:
        return answers
    return [
        (path, normalize_phone_prefix(raw) if path in phone_paths else raw) for path, raw in answers
    ]


def _parse_date(value: Any, field_path: str, date_format: str | None = None) -> date | None:
    """ISO first — intake's caller is a separate machine system that always sends
    ISO, regardless of the leaf's own `date_format`. Falls back to `date_format`
    (the leaf's display/entry format, e.g. "M/D/YYYY") for a human-typed value —
    the review UI prompts for and submits values in exactly that format, never ISO."""
    text = _clean_str(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    if date_format is not None:
        parsed = parse_date_format(text, date_format)
        if parsed is not None:
            return parsed
    raise InvalidIntakeValue(field_path, "expected an ISO date or the field's configured format")


def unknown_payload_paths(answers: list[tuple[str, Any]], doc: FormSchemaDoc) -> list[str]:
    """Paths in `answers` that are not in `doc`'s leaf set — used to reject intake
    payloads containing keys the schema does not define. Returns a sorted, deduplicated
    list of offending root-anchored paths. Only meaningful for v2 documents (the caller
    must hold `doc` from `_v2_doc`; v1 schemas have no leaf set to validate against
    and skip this check entirely).

    Names only — never the values (PHI)."""
    known = {path for path, _ in doc.leaf_items()}
    answer_paths = {path for path, _ in answers}
    return sorted(answer_paths - known)


def promote_columns(get_value: Callable[[str], Any], doc: FormSchemaDoc) -> PromotedIdentifiers:
    """Extract + normalize the `patient_form` columns `doc.promoted_fields` maps to
    (ADR §5 rule 3 — stable input for a future blind index). `get_value(path)` resolves
    one root-anchored schema path to its raw value — the caller supplies a nested-payload
    lookup at intake (`resolve_path`) or a flat `{field_path: value}` lookup at
    dispute-resolve (`dict.get`); both share the same schema-path namespace. Raises
    `InvalidIntakeValue` on a bad date."""
    leaves = dict(doc.leaf_items())
    values: dict[str, Any] = {}
    for column, path in doc.promoted_fields.items():
        raw = get_value(path)
        leaf = leaves.get(path)
        if column in ("patient_dob", "appointment_date"):
            date_format = leaf.validation.date_format if leaf and leaf.validation else None
            values[column] = _parse_date(raw, path, date_format)
        elif column == "patient_name":
            cleaned = _clean_str(raw)
            values[column] = cleaned.lower() if cleaned is not None else None
        elif column == "chart_number":
            cleaned = _clean_str(raw)
            values[column] = None if cleaned is not None and cleaned.upper() == "N/A" else cleaned
        elif leaf is not None and leaf.type == "phone":
            cleaned = _clean_str(raw)
            if cleaned is not None:
                cleaned = normalize_phone_prefix(cleaned)
                if not E164_RE.match(cleaned):
                    raise InvalidIntakeValue(path, "expected an E.164 phone number")
            values[column] = cleaned
        else:
            values[column] = _clean_str(raw)
    return PromotedIdentifiers(**values)

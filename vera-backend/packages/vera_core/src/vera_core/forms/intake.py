"""Pure, DB-free helpers for IBV patient-form intake.

The intake endpoint (`control_plane.api.v1.patient_forms`) uses these to validate
the minimum required fields, flatten the nested `intake_payload` into per-field
answers, and promote the searchable identifier columns. Kept free of SQLAlchemy /
FastAPI so they unit-test without a database.

PHI note: these return field **paths** and (for promotion) typed values the caller
persists — never log the values. Validation errors carry paths only.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

# The schema's pre-provided context section (chart/patient identifiers) and the
# section carrying the appointment date — the only two the intake step reads
# structurally. Everything else is stored opaquely in `intake_payload`.
_PATIENT_INFO = "patient_information"
_APPOINTMENT_INFO = "appointment_information"


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
    """The `required` field keys of the `patient_information` section (the only
    fields a clinic must provide at intake). Data-driven from `schema_json`."""
    for section in schema_json.get("sections", []):
        if section.get("section_key") == _PATIENT_INFO:
            required: list[str] = list(section.get("required", []))
            return required
    return []


def missing_required(payload: dict[str, Any], schema_json: dict[str, Any]) -> list[str]:
    """Dotted paths of required `patient_information` fields absent/blank in
    `payload`. Names only — never the values."""
    section = payload.get(_PATIENT_INFO)
    values = section if isinstance(section, dict) else {}
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
    """The searchable identifier columns promoted out of `intake_payload`."""

    patient_name: str | None
    patient_dob: date | None
    appointment_date: date | None
    chart_number: str | None
    member_id: str | None  # no schema source at intake — always None here


def _get(payload: dict[str, Any], section: str, field: str) -> Any:
    sec = payload.get(section)
    return sec.get(field) if isinstance(sec, dict) else None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(value: Any, field_path: str) -> date | None:
    text = _clean_str(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidIntakeValue(field_path, "expected ISO date YYYY-MM-DD") from exc


def promote_columns(payload: dict[str, Any]) -> PromotedIdentifiers:
    """Extract + normalize the searchable identifiers (ADR §5 rule 3 — stable input
    for a future blind index). `intake_payload` keeps the original raw values; only
    these promoted copies are normalized. Raises `InvalidIntakeValue` on a bad date."""
    name = _clean_str(_get(payload, _PATIENT_INFO, "patient_name"))
    chart = _clean_str(_get(payload, _PATIENT_INFO, "chart_number"))
    if chart is not None and chart.upper() == "N/A":
        chart = None
    return PromotedIdentifiers(
        patient_name=name.lower() if name is not None else None,
        patient_dob=_parse_date(
            _get(payload, _PATIENT_INFO, "patient_dob"), f"{_PATIENT_INFO}.patient_dob"
        ),
        appointment_date=_parse_date(
            _get(payload, _APPOINTMENT_INFO, "appointment_date"),
            f"{_APPOINTMENT_INFO}.appointment_date",
        ),
        chart_number=chart,
        member_id=None,
    )

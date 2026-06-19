import pytest
from pydantic import ValidationError

from vera_core.schemas import FieldType, FormField, FormTemplate


def _field(key: str = "member_id", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "key": key,
        "label": "Member ID",
        "type": FieldType.TEXT,
        "phi": True,
        "entity_type": "BENEFICIARY_ID",
    }
    base.update(overrides)
    return base


def test_valid_template() -> None:
    template = FormTemplate(
        key="eligibility-basic",
        version=1,
        title="Basic eligibility",
        fields=[FormField(**_field())],
    )
    assert template.phi_fields[0].key == "member_id"


def test_duplicate_field_keys_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate field keys"):
        FormTemplate(
            key="t",
            version=1,
            title="t",
            fields=[FormField(**_field()), FormField(**_field())],
        )


def test_phi_field_requires_entity_type() -> None:
    with pytest.raises(ValidationError, match="needs entity_type"):
        FormField(**_field(entity_type=None))


def test_select_requires_options() -> None:
    with pytest.raises(ValidationError, match="needs options"):
        FormField(**_field(type=FieldType.SELECT, phi=False, entity_type=None))


def test_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        FormTemplate(key="t", version=1, title="t", fields=[])

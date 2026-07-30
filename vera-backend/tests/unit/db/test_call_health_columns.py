"""Call health denormalized columns + the observer flag vocabulary."""

from uuid import uuid4

from vera_core.models import Call
from vera_core.models.enums import CallHealthFlag, InterventionCategory, values_of


def test_health_flag_vocabulary_superset_of_intervention_categories() -> None:
    flags = set(values_of(CallHealthFlag))
    assert set(values_of(InterventionCategory)) <= flags
    assert "none" in flags
    assert "supervisor_requested" in flags


def test_call_health_columns_default_null() -> None:
    call = Call(id=uuid4(), tenant_id=uuid4(), form_id=uuid4(), current_status="active")
    assert call.health_score is None
    assert call.health_flag is None
    assert call.health_analyzed_at is None


def test_call_has_health_flag_check() -> None:
    table = Call.metadata.tables["call"]
    names = {c.name for c in table.constraints}
    assert "ck_call_health_flag_valid" in names

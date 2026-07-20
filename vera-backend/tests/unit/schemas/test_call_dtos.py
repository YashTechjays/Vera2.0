from datetime import UTC, datetime
from uuid import uuid4

from vera_core.schemas import CallSummary, JoinTokenResponse


def test_call_summary_grown_fields() -> None:
    s = CallSummary(
        id=uuid4(),
        tenant_id=uuid4(),
        form_id=uuid4(),
        status="active",
        room_name="call--t--c",
        patient_name="Jane Doe",
        started_at=None,
        created_at=datetime.now(UTC),
    )
    assert s.status == "active"
    assert s.room_name == "call--t--c"


def test_join_token_dto() -> None:
    jt = JoinTokenResponse(token="jwt", url="ws://x", room_name="call--t--c")
    assert jt.url == "ws://x"


def test_call_summary_health_fields_default_null() -> None:
    s = CallSummary(
        id=uuid4(),
        tenant_id=uuid4(),
        form_id=uuid4(),
        status="active",
        room_name="r",
        created_at=datetime.now(UTC),
    )
    assert s.health_score is None and s.health_flag is None and s.health_analyzed_at is None
    assert s.health_reason is None

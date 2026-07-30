"""Unit tests for `DatabaseAuditWriter.emit` — pins the exact bind params/types
sent to the `log_audit_event` SECURITY DEFINER function (migration
9514979e3fee) against drift, without needing a database. The raw-SQL path
replaced an ORM insert whose `JSONB`/enum column types used to coerce these
values automatically; this test is the one place that coercion is asserted
now that it happens by hand in `emit()`.
"""

import json
from typing import Any
from uuid import UUID

from vera_core.audit.writer import AuditRecord, DatabaseAuditWriter
from vera_core.models.audit_log import ActorType

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
ACTOR = UUID("00000000-0000-0000-0000-0000000000cc")
ELEVATION = UUID("00000000-0000-0000-0000-0000000000ee")


class _FakeResult:
    pass


class _FakeSession:
    """Captures the one statement DatabaseAuditWriter.emit executes."""

    def __init__(self) -> None:
        self.compiled_params: dict[str, Any] | None = None

    async def execute(self, statement: Any) -> _FakeResult:
        self.compiled_params = statement.compile().params
        return _FakeResult()

    def begin(self) -> "_FakeTransaction":
        return _FakeTransaction()

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeSessionmaker:
    def __init__(self) -> None:
        self.session = _FakeSession()

    def __call__(self) -> _FakeSession:
        return self.session


async def test_emit_binds_enum_value_and_json_serialized_detail() -> None:
    sessionmaker = _FakeSessionmaker()
    writer = DatabaseAuditWriter(sessionmaker)  # type: ignore[arg-type]
    record = AuditRecord(
        tenant_id=TENANT,
        actor_type=ActorType.USER,
        actor_user_id=ACTOR,
        actor_label="a@example.com",
        event_type="phi.access",
        resource_type="patient_form",
        resource_id="list",
        permission_key=None,
        decision=None,
        request_id="req-1",
        detail={"fields": ["patient_name"]},
        reason="",
        elevation_session_id=ELEVATION,
    )

    await writer.emit(record)

    params = sessionmaker.session.compiled_params
    assert params is not None
    # The enum is unwrapped to its raw string — the definer fn's p_actor_type is
    # `text`, casting to the actor_type enum on the Postgres side.
    assert params["actor_type"] == "user"
    assert isinstance(params["actor_type"], str)
    # detail is pre-serialized to a JSON string for the `CAST(:detail AS jsonb)`
    # — the raw-SQL path has no JSONB column type to do this implicitly.
    assert params["detail"] == json.dumps({"fields": ["patient_name"]})
    assert json.loads(params["detail"]) == {"fields": ["patient_name"]}
    assert params["tenant_id"] == TENANT
    assert params["elevation_session_id"] == ELEVATION
    assert params["request_id"] == "req-1"
    assert isinstance(params["id"], UUID)  # server-supplied UUIDv7 PK

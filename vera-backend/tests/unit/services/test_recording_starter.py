"""Fail-open egress kickoff: PENDING row on success, FAILED row + audit on error."""

from uuid import uuid4

from vera_core.audit import AuditRecord
from vera_core.models import Recording
from vera_core.models.enums import RecordingStatus
from vera_core.services.recordings import (
    RecordingConfig,
    recording_object_path,
    start_recording_for_call,
)


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


class _FakeAudit:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def emit(self, record: AuditRecord) -> None:
        self.records.append(record)


class _OkGateway:
    async def start_room_audio_egress(
        self, room_name: str, *, bucket: str, object_path: str
    ) -> str:
        return "EG_OK"


class _BoomGateway:
    async def start_room_audio_egress(
        self, room_name: str, *, bucket: str, object_path: str
    ) -> str:
        raise RuntimeError("egress unreachable")


CONFIG = RecordingConfig(bucket="vera-rec", prefix="recordings")


def test_object_path_is_uuid_only() -> None:
    t, c = uuid4(), uuid4()
    assert recording_object_path(CONFIG, t, c) == f"recordings/{t}/{c}.ogg"


async def test_success_inserts_pending_row() -> None:
    session, t, c = _FakeSession(), uuid4(), uuid4()
    await start_recording_for_call(session, _OkGateway(), config=CONFIG, tenant_id=t, call_id=c)
    (row,) = session.added
    assert isinstance(row, Recording)
    assert row.status == RecordingStatus.PENDING.value
    assert row.egress_id == "EG_OK"
    assert row.gcs_uri == f"gs://vera-rec/recordings/{t}/{c}.ogg"


async def test_failure_is_fail_open_with_failed_row_and_audit() -> None:
    session, audit, t, c = _FakeSession(), _FakeAudit(), uuid4(), uuid4()
    # Must NOT raise — the call proceeds unrecorded (spec decision 2).
    await start_recording_for_call(
        session, _BoomGateway(), config=CONFIG, tenant_id=t, call_id=c, audit=audit
    )
    (row,) = session.added
    assert isinstance(row, Recording)
    assert row.status == RecordingStatus.FAILED.value
    assert row.egress_id is None
    assert audit.records[0].event_type == "recording.start_failed"

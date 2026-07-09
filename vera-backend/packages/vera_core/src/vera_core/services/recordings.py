"""Recording kickoff: start the audio-only composite egress and insert the
Recording row, at both call-creation sites (manual /calls and the queue
dispatcher). FAIL-OPEN by design (spec decision 2): a recording that cannot
start must never block a payer call — the failure is recorded as a FAILED row
plus a RECORDING_START_FAILED audit event, and the call proceeds.

No PHI here: room names, buckets, and object paths carry only opaque UUIDs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vera_core.audit import AuditRecord
from vera_core.models import Recording
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import RecordingStatus
from vera_core.observability.correlation import room_name_for_call

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from vera_core.audit import AuditSink
    from vera_core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingConfig:
    bucket: str
    prefix: str


def recording_config_from(settings: Settings) -> RecordingConfig | None:
    """None ⇒ recording disabled end-to-end (bucket unset — local dev, CI)."""
    if settings.recording_bucket is None:
        return None
    return RecordingConfig(bucket=settings.recording_bucket, prefix=settings.recording_prefix)


def recording_object_path(config: RecordingConfig, tenant_id: UUID, call_id: UUID) -> str:
    """Opaque-UUID object path — no PHI in paths (bright line)."""
    key = f"{tenant_id}/{call_id}.ogg"
    prefix = config.prefix.strip("/")
    return f"{prefix}/{key}" if prefix else key


async def start_recording_for_call(
    session: AsyncSession | Any,
    livekit: Any,  # duck-typed like try_dispatch's gateway param
    *,
    config: RecordingConfig,
    tenant_id: UUID,
    call_id: UUID,
    audit: AuditSink | None = None,
) -> None:
    """Start egress + insert the Recording row. Never raises (fail-open)."""
    try:
        room_name = room_name_for_call(tenant_id, call_id)
        object_path = recording_object_path(config, tenant_id, call_id)
        gcs_uri = f"gs://{config.bucket}/{object_path}"
        try:
            egress_id = await livekit.start_room_audio_egress(
                room_name, bucket=config.bucket, object_path=object_path
            )
        except Exception:
            logger.exception("recording: egress start failed for call %s — call proceeds", call_id)
            session.add(
                Recording(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    gcs_uri=gcs_uri,
                    status=RecordingStatus.FAILED.value,
                )
            )
            if audit is not None:
                await audit.emit(
                    AuditRecord(
                        tenant_id=tenant_id,
                        actor_type=ActorType.SYSTEM,
                        actor_label="recording-starter",
                        event_type=AuditEvent.RECORDING_START_FAILED.value,
                        resource_type="call",
                        resource_id=str(call_id),
                    )
                )
            return
        session.add(
            Recording(
                tenant_id=tenant_id,
                call_id=call_id,
                gcs_uri=gcs_uri,
                status=RecordingStatus.PENDING.value,
                egress_id=egress_id,
            )
        )
    except Exception:
        logger.exception("recording: unexpected error for call %s — call proceeds", call_id)

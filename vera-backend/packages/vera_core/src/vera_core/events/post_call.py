"""Control-plane-internal post-call eval job over Redis Streams + a consumer group.

The call-end callback enqueues one job per completed call; the post-call consumer
drains it and runs the LLM re-read. PHI-free by construction: only tenant/form/call
UUIDs — never transcript text or identifiers.
"""

from uuid import UUID

from pydantic import BaseModel, TypeAdapter

from vera_core.events.stream_bus import StreamBus

POST_CALL_STREAM = "vera:post-call"
POST_CALL_GROUP = "post-call"
_JOB_FIELD = "job"


class PostCallJob(BaseModel):
    """A completed call awaiting the post-call re-read."""

    tenant_id: UUID
    form_id: UUID
    call_id: UUID


_ADAPTER: TypeAdapter[PostCallJob] = TypeAdapter(PostCallJob)


def parse_post_call_job(raw: str) -> PostCallJob:
    """Deserialize a stream payload; raises on invalid."""
    return _ADAPTER.validate_json(raw)


class PostCallJobBus(StreamBus):
    """XADD publish side (callback) + consumer-group bootstrap. One stream, one group."""

    stream = POST_CALL_STREAM
    group = POST_CALL_GROUP
    payload_field = _JOB_FIELD

    async def emit(self, job: PostCallJob) -> None:
        await self._emit_raw(job.model_dump_json())

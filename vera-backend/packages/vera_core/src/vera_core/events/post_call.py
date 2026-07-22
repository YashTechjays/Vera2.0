"""Control-plane-internal post-call eval job over Redis Streams + a consumer group.

The call-end callback enqueues one job per completed call; the post-call consumer
drains it and runs the LLM re-read. PHI-free by construction: only tenant/form/call
UUIDs — never transcript text or identifiers.
"""

from uuid import UUID

from pydantic import BaseModel, TypeAdapter
from redis.asyncio import Redis
from redis.exceptions import ResponseError

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


class PostCallJobBus:
    """XADD publish side (callback) + consumer-group bootstrap. One stream, one group."""

    def __init__(self, redis: Redis, *, maxlen: int = 10_000) -> None:
        self._redis = redis
        self._maxlen = maxlen

    async def emit(self, job: PostCallJob) -> None:
        await self._redis.xadd(
            POST_CALL_STREAM,
            {_JOB_FIELD: job.model_dump_json()},
            maxlen=self._maxlen,
            approximate=True,
        )

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                POST_CALL_STREAM, POST_CALL_GROUP, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

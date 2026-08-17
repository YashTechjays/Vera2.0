"""On-demand supervisor-handoff summary of a live call's transcript.

Snapshot the diarized transcript (Redis call-event stream while the call is
live; persisted Transcript rows once the finalizer has drained it), format it
with speaker labels, and run it through the fault-tolerant ResilientLLM chain.
Results cache in Redis for a few seconds so tab-flipping supervisors don't fan
out LLM calls; a cache outage degrades to computing fresh (type-name-only logs —
the payload is PHI).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import UUID

from opentelemetry import trace
from pydantic import BaseModel
from sqlalchemy import select

from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamService
from vera_core.db.rls import tenant_session
from vera_core.models import Transcript
from vera_core.observability import TraceLinkStore, call_trace_attributes
from vera_core.observability.correlation import room_name_for_call
from vera_core.transcript import ROLE_COACHING, ROLE_DTMF, ROLE_WHISPER, TurnRole, source_for_role

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("vera.control_plane.summary")

# Fewer real speech turns than this and there is nothing to brief — the endpoint
# reports "pending" without spending an LLM call.
_MIN_SPEECH_TURNS = 2

# Coaching/whisper turns are a supervisor talking to Vera, not to anyone on the
# call — they must never be rendered into the handoff summary's transcript (the
# LLM would read them as something a supervisor said on the call) nor count
# toward whether there's enough real speech to summarize.
_NON_SPEECH_ROLES = frozenset({ROLE_DTMF, ROLE_COACHING, ROLE_WHISPER})

SUMMARY_SYSTEM_PROMPT = """\
You are briefing a human supervisor who is about to take over or monitor a live
insurance-verification phone call mid-flight. From the diarized transcript,
produce a handoff summary.

Respond with ONLY a JSON object — no markdown fences, no prose around it — in
exactly this shape:
{
  "participants": "<who is on the call, one short line>",
  "purpose": "<why the call is happening, one short line>",
  "facts": ["<one confirmed fact per item, e.g. an ID that was accepted>"],
  "open_items": ["<one unresolved/in-progress item per item>"],
  "next_step": "<the single most likely next action, one short line>"
}
Keep every string short and skimmable (under 15 words). Use empty arrays or
null when a section has nothing. Be factual and neutral; do not invent details
that are not in the transcript."""

# Transcript.source ("rep"/"bot") -> envelope role, used only when the row's own
# `role` is blank (older rows / a source the worker didn't stamp a role for).
_SOURCE_TO_ROLE = {"rep": "user", "bot": "agent"}

_SPEAKER_LABELS = {"rep": "Payer rep", "bot": "Vera (agent)", "supervisor": "Supervisor"}


def transcript_role(row: Transcript) -> str:
    return row.role or _SOURCE_TO_ROLE.get(row.source, row.source)


@dataclass(frozen=True)
class TranscriptTurn:
    """One diarized turn, normalized from either the live stream or a DB row."""

    source: str
    role: str
    text: str


def format_diarized(turns: Sequence[TranscriptTurn]) -> str:
    """Render speaker-labelled lines: `Vera (agent): ...` / `Payer rep: ...`.
    Coaching/whisper turns are never rendered — they're a supervisor talking to
    Vera, not part of the call, and must not reach the summarization LLM as if
    someone said them on the call."""
    lines: list[str] = []
    for turn in turns:
        if turn.role in (ROLE_COACHING, ROLE_WHISPER):
            continue
        label = _SPEAKER_LABELS.get(turn.source, turn.source)
        if turn.role == ROLE_DTMF:
            label = f"{label} [keypad]"
        lines.append(f"{label}: {turn.text}")
    return "\n".join(lines)


async def snapshot_turns(
    stream: CallStreamService,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    tenant_id: UUID,
    call_id: UUID,
) -> list[TranscriptTurn]:
    """Current transcript snapshot: the live Redis stream while it exists, the
    persisted Transcript rows once the finalizer has drained it (mirrors the
    redis-or-DB branch of `stream_call_events`)."""
    events = await stream.read_all(room_name_for_call(tenant_id, call_id))
    turns = [
        TranscriptTurn(
            source=e.data.get("source") or source_for_role(cast("TurnRole", e.data["role"])),
            role=e.data["role"],
            text=e.data["text"],
        )
        for e in events
        if e.type == TYPE_TRANSCRIPT
    ]
    if turns or sessionmaker is None:
        return turns
    async with tenant_session(sessionmaker, tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(Transcript).where(Transcript.call_id == call_id).order_by(Transcript.seq)
                )
            )
            .scalars()
            .all()
        )
    return [
        TranscriptTurn(source=row.source, role=transcript_role(row), text=row.message)
        for row in rows
    ]


def summary_cache_key(room_name: str) -> str:
    return f"vera:summary:{room_name}"


class SummaryCache(Protocol):
    async def get(self, room_name: str) -> str | None: ...
    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None: ...


class RedisSummaryCache:
    """Short-TTL summary cache in the in-boundary Memorystore (PHI at rest is
    CMEK-covered there; the TTL self-clears the key)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, room_name: str) -> str | None:
        value = await self._redis.get(summary_cache_key(room_name))
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None:
        await self._redis.set(summary_cache_key(room_name), payload, ex=ttl_seconds)


class SummaryLLM(Protocol):
    """Structural view of vera_core.llm.ResilientLLM (keeps this module and its
    tests decoupled from the concrete class)."""

    async def complete(self, *, system: str, user: str) -> str: ...


class SummarySections(BaseModel):
    """The handoff summary broken into skimmable sections (the LLM's JSON
    contract). Every field is optional — the model omits what the transcript
    doesn't support."""

    participants: str | None = None
    purpose: str | None = None
    facts: list[str] = []
    open_items: list[str] = []
    next_step: str | None = None


_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_sections(text: str) -> SummarySections | None:
    """Parse the LLM's JSON handoff into sections; None when the model ignored
    the contract (the caller then falls back to the raw text). Content is PHI —
    parse failures log the exception type only."""
    raw = text.strip()
    fenced = _JSON_FENCE.match(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        return SummarySections.model_validate_json(raw)
    except Exception as exc:
        logger.warning("summary sections parse failed: %s", type(exc).__name__)
        return None


def flatten_sections(sections: SummarySections) -> str:
    """Deterministic plain-text rendering of the sections — keeps the `summary`
    field human-readable for any consumer that doesn't render sections."""
    lines: list[str] = []
    if sections.participants:
        lines.append(f"Participants: {sections.participants}")
    if sections.purpose:
        lines.append(f"Purpose: {sections.purpose}")
    if sections.facts:
        lines.append("Established:")
        lines.extend(f"- {fact}" for fact in sections.facts)
    if sections.open_items:
        lines.append("Open items:")
        lines.extend(f"- {item}" for item in sections.open_items)
    if sections.next_step:
        lines.append(f"Next step: {sections.next_step}")
    return "\n".join(lines)


class CallSummaryResponse(BaseModel):
    status: Literal["ready", "pending"]
    summary: str | None
    # Structured view of `summary` for the tabbed UI; None when the LLM's reply
    # didn't parse (the plain-text `summary` is then the raw reply).
    sections: SummarySections | None = None
    generated_at: int  # epoch milliseconds
    turn_count: int


async def summarize_call(
    *,
    llm: SummaryLLM,
    cache: SummaryCache,
    stream: CallStreamService,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    tenant_id: UUID,
    call_id: UUID,
    ttl_seconds: int,
    trace_links: TraceLinkStore | None = None,
) -> CallSummaryResponse:
    """Cache-through summary of the call's transcript so far. Raises
    LLMUnavailableError (from the llm) when every provider fails."""
    room_name = room_name_for_call(tenant_id, call_id)
    cached = None
    try:
        cached = await cache.get(room_name)
    except Exception as exc:  # cache outage degrades to fresh compute
        logger.warning("summary cache get failed: %s", type(exc).__name__)
    if cached is not None:
        try:
            return CallSummaryResponse.model_validate_json(cached)
        except Exception as exc:  # corrupt/schema-skewed payload degrades to fresh compute
            logger.warning("summary cache payload invalid: %s", type(exc).__name__)

    turns = await snapshot_turns(stream, sessionmaker, tenant_id, call_id)
    generated_at = int(time.time() * 1000)
    speech_turns = [t for t in turns if t.role not in _NON_SPEECH_ROLES]
    if len(speech_turns) < _MIN_SPEECH_TURNS:
        return CallSummaryResponse(
            status="pending", summary=None, generated_at=generated_at, turn_count=len(turns)
        )

    # The SDK auto-instruments the LLM request, but as a root trace with no link to the
    # call — real spend that no per-call cost query can find. This parents it into the
    # call's own trace; a missing/expired link degrades to a root span.
    parent = await trace_links.resolve(room_name) if trace_links is not None else None
    with _tracer.start_as_current_span(
        "vera.call_summary",
        context=parent,
        attributes=call_trace_attributes(room_name),
        record_exception=False,
        set_status_on_exception=False,
    ):
        reply = await llm.complete(system=SUMMARY_SYSTEM_PROMPT, user=format_diarized(turns))
    sections = parse_sections(reply)
    response = CallSummaryResponse(
        status="ready",
        summary=flatten_sections(sections) if sections is not None else reply,
        sections=sections,
        generated_at=generated_at,
        turn_count=len(turns),
    )
    try:
        await cache.set(room_name, response.model_dump_json(), ttl_seconds)
    except Exception as exc:
        logger.warning("summary cache set failed: %s", type(exc).__name__)
    return response

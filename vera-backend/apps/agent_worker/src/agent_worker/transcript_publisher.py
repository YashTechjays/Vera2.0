"""Publish finalized, de-identified transcript turns via the TranscriptService.

Taps AgentSession events on the de-identified side of the PHI wall: user turns are the
redacted FINAL transcript (post stt_node); agent turns are the LLM's token-only output
(pre tts_node hydration). Best-effort — a Redis failure logs and is swallowed, never
breaking the call.
"""

import asyncio
import logging
import time
from typing import Any

from vera_core.transcript import ROLE_AGENT, ROLE_USER, TranscriptService

logger = logging.getLogger("agent_worker")

_PENDING: set[asyncio.Task[None]] = set()


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _publish_user(service: TranscriptService, room_name: str, ev: Any) -> None:
    if not ev.is_final:
        return
    text = (ev.transcript or "").strip()
    if not text:
        return
    await service.publish_turn(room_name, ROLE_USER, text, ts=_now_ms())


async def _publish_agent(service: TranscriptService, room_name: str, ev: Any) -> None:
    item = ev.item
    if getattr(item, "role", None) != "assistant":
        return
    text = (getattr(item, "text_content", None) or "").strip()
    if not text:
        return
    await service.publish_turn(room_name, ROLE_AGENT, text, ts=_now_ms())


def _log_exc(t: "asyncio.Task[None]") -> None:
    exc = t.exception()
    if exc is not None:
        logger.warning("transcript publish failed: %r", exc)


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    task.add_done_callback(_log_exc)


def attach_transcript_publisher(session: Any, service: TranscriptService, room_name: str) -> None:
    """Register session handlers that publish finalized user/agent turns via `service`."""

    def _on_user(ev: Any) -> None:
        _spawn(_publish_user(service, room_name, ev))

    def _on_item(ev: Any) -> None:
        _spawn(_publish_agent(service, room_name, ev))

    session.on("user_input_transcribed", _on_user)
    session.on("conversation_item_added", _on_item)

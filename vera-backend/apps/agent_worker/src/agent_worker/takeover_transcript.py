"""Transcribe the human-to-human conversation after a supervisor takes over.

The agent's own STT stops when the bot is silenced on takeover, so this runs a
dedicated per-track STT on the caller and the intervening supervisor and publishes
their finalized turns to the live call stream. Per-track = deterministic speaker
attribution (no diarization guessing).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, NamedTuple
from uuid import UUID

from livekit import rtc
from livekit.agents import stt as agents_stt

from agent_worker.transcript_publisher import TurnPublisher
from vera_core.observability.correlation import (
    PARTICIPANT_MODE_ATTR,
    PARTICIPANT_MODE_INTERVENER,
    supervisor_user_id,
)
from vera_core.transcript import (
    ROLE_USER,
    SOURCE_REP,
    SOURCE_SUPERVISOR,
    TurnSource,
)

logger = logging.getLogger("agent_worker")

_SAMPLE_RATE = 16000


class SpeakerAttribution(NamedTuple):
    source: TurnSource
    user_id: UUID | None


def classify_source(
    identity: str, mode_attr: str | None, callee_identity: str
) -> SpeakerAttribution | None:
    """The transcript source (+ speaker's user id, if known) for a participant, or
    None to skip (listeners, the agent)."""
    if identity == callee_identity:
        return SpeakerAttribution(SOURCE_REP, None)
    if mode_attr == PARTICIPANT_MODE_INTERVENER:
        return SpeakerAttribution(SOURCE_SUPERVISOR, supervisor_user_id(identity))
    return None


async def publish_final_turns(
    events: AsyncIterator[Any], sink: TurnPublisher, room_name: str, attribution: SpeakerAttribution
) -> None:
    """Publish each FINAL STT event as a turn; skip interim/empty. Publish failures
    are logged, never raised — a transcript hiccup must not break the call."""
    source, user_id = attribution
    async for ev in events:
        if ev.type != agents_stt.SpeechEventType.FINAL_TRANSCRIPT or not ev.alternatives:
            continue
        text = (ev.alternatives[0].text or "").strip()
        if not text:
            continue
        logger.info("takeover turn: source=%s len=%d", source, len(text))  # never the text (PHI)
        try:
            await sink.publish_turn(
                room_name,
                ROLE_USER,
                text,
                ts=int(time.time() * 1000),
                source=source,
                user_id=str(user_id) if user_id is not None else None,
            )
        except Exception:
            logger.exception("takeover transcript publish failed for %s", room_name)


class TakeoverTranscriber:
    """Transcribes the caller + intervening supervisor tracks and publishes their turns."""

    def __init__(
        self,
        room: rtc.Room,
        sink: TurnPublisher,
        room_name: str,
        *,
        stt_factory: Callable[[], agents_stt.STT[Any]],
        callee_identity: str,
    ) -> None:
        self._room = room
        self._sink = sink
        self._room_name = room_name
        self._stt_factory = stt_factory
        self._callee_identity = callee_identity
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._room.on("track_subscribed", self._on_track_subscribed)
        for participant in self._room.remote_participants.values():
            for pub in participant.track_publications.values():
                if pub.track is not None and pub.kind == rtc.TrackKind.KIND_AUDIO:
                    self._maybe_transcribe(pub.track, participant)
        logger.info("takeover transcriber started: %d track(s)", len(self._tasks))

    def _on_track_subscribed(
        self, track: rtc.Track, _pub: rtc.TrackPublication, participant: rtc.RemoteParticipant
    ) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            self._maybe_transcribe(track, participant)

    def _maybe_transcribe(self, track: rtc.Track, participant: rtc.RemoteParticipant) -> None:
        existing = self._tasks.get(track.sid)
        if existing is not None and not existing.done():
            return  # a finished task (resub with the same SID) is replaced below
        attribution = classify_source(
            participant.identity,
            participant.attributes.get(PARTICIPANT_MODE_ATTR),
            self._callee_identity,
        )
        if attribution is None:
            return
        logger.info(
            "takeover: transcribing %s as source=%s", participant.identity, attribution.source
        )
        self._tasks[track.sid] = asyncio.create_task(self._transcribe_track(track, attribution))

    async def _transcribe_track(self, track: rtc.Track, attribution: SpeakerAttribution) -> None:
        audio = rtc.AudioStream(track, sample_rate=_SAMPLE_RATE, num_channels=1)
        stream = self._stt_factory().stream()
        pump = asyncio.create_task(self._pump(audio, stream))
        try:
            await publish_final_turns(stream, self._sink, self._room_name, attribution)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("takeover transcribe failed (source=%s)", attribution.source)
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump  # let the pump stop before closing the stream under it
            await stream.aclose()
            await audio.aclose()

    async def _pump(self, audio: AsyncIterator[Any], stream: Any) -> None:
        try:
            async for frame_event in audio:
                stream.push_frame(frame_event.frame)
            stream.end_input()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("takeover audio pump failed for %s", self._room_name)

    async def aclose(self) -> None:
        if not self._started:
            return
        self._room.off("track_subscribed", self._on_track_subscribed)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("takeover transcript task failed for %s", self._room_name)
        self._tasks.clear()

"""Regression tests for the stuck-turn failure on dev trace 863ba65ac918521c0518aeceea1d3d0b
(2026-07-30): four times in one 744s call the bot generated a reply and never spoke it, and
only answered after the caller nudged it ("Hello. Are you there?"). 67s of dead air, each
window holding an answer that was already written.

WHAT WENT WRONG — the end-of-turn task waits `min_delay + (last_speaking_time - now)` before
committing a turn. On a turn with no VAD speech segment, `_last_speaking_time` is None and it
falls back to the STT stream timestamp `end_time + _input_started_at`. An agent handoff
desynchronized those two clocks: `Agent.stt_node` aligns them once at stream creation
(`start_time_offset = now - _input_started_at`), then the handoff REUSES the stream while
re-seeding `_input_started_at` to the handoff instant. The offset stayed anchored to the old
value and was consumed against the new one, so transcripts mapped
`elapsed-since-stream-start` seconds into the FUTURE. The wait became that overshoot —
unbounded, never clamped by `max_delay` — and the preemptive reply sat parked on
`speech_handle._wait_for_scheduled()` with TTS never starting.

On the dev call the stream opened at t≈9s and the second handoff ran at t=207.3s, so every
later quiet turn faced a ~198s wait. It only bit turns where VAD reported no speech energy
at all; any energy, even below the interruption threshold, supplies a real timestamp instead
(`test_faint_vad_energy_keeps_the_stt_clock_out_of_the_path`). That is why 4 of ~50 turns
stalled and why it looked random.

FIXED UPSTREAM in livekit-agents 1.6.4 (we were on 1.5.17), two changes together:
`_input_started_at` became a property delegating to the STT pipeline, so the anchor travels
with the stream and cannot be re-seeded independently; and the STT-derived speaking time is
now clamped with `min(..., now)`, so no timestamp can produce an unbounded wait. These tests
assert the fixed behaviour and fail on any version before 1.6.4.

Still open, and still marked xfail below: an unclosed VAD segment can cancel the end-of-turn
task with nothing re-arming the commit. That is a second, independent route into the same
dead air and 1.6.7 does not address it. It was NOT what bit the dev call — it leaves a long
`user_speaking` span, which that trace does not have.
"""

import asyncio
import time
from typing import Any

import pytest
from cascade_harness import CascadeHarness

_OPENING = "I need to check a patient's coverage."
# The cascade pins endpointing to min 0.3s / max 0.6s, so a committed turn can never make
# the caller wait much past `max_delay`, whatever the STT stream claims.
_MAX_ENDPOINTING_DELAY = 0.6


async def test_vad_backed_turn_is_spoken() -> None:
    # Control. If this fails the harness is broken, not the pipeline — every assertion
    # below is only meaningful while this one passes.
    async with CascadeHarness() as call:
        await call.user_says(_OPENING)

        assert await call.wait_for_reply()
        assert call.agent.llm_calls == 1


async def test_transcript_only_turn_is_answered() -> None:
    # A transcript Silero never scored as speech commits fine on its own, with or without a
    # preceding agent turn. "Deepgram heard something the VAD missed" was never the bug.
    async with CascadeHarness() as call:
        await call.exchange(_OPENING)
        await asyncio.sleep(2.0)

        call.phantom_transcript("Yeah.")

        assert await call.wait_for_reply()


async def test_past_dated_stt_timestamp_answers_immediately() -> None:
    # The t=442.7 turn from the trace: transcript-only, timestamp in the past, committed
    # 0.01s after EOU.
    async with CascadeHarness() as call:
        await call.exchange(_OPENING)

        call.phantom_transcript("Yeah.", lands_at_offset=-2.0)

        assert await call.wait_for_reply(within=1.0)


async def test_future_dated_stt_timestamp_does_not_delay_the_reply() -> None:
    # The core guarantee: reply latency is independent of what the STT stream claims about
    # when the utterance ended. Pre-1.6.4 this waited the full overshoot.
    async with CascadeHarness() as call:
        await call.exchange(_OPENING)

        started = time.time()
        call.phantom_transcript("Yeah.", lands_at_offset=5.0)
        assert await call.wait_for_reply(within=_MAX_ENDPOINTING_DELAY + 1.0)

        assert time.time() - started < 2.0, "reply latency tracked the bogus timestamp"


async def test_handoff_reuses_the_stt_stream() -> None:
    # Reuse must survive the fix: tearing the Deepgram connection down at each handoff
    # would drop caller audio during the reconnect. The fix realigns the clocks instead.
    async with CascadeHarness() as call:
        await call.exchange(_OPENING)

        await call.handoff()

        assert len(call.stt.streams) == 1


async def test_handoff_keeps_the_stt_clock_aligned() -> None:
    # The mechanism, measured. Pre-1.6.4 the drift grew to the elapsed call time here.
    async with CascadeHarness() as call:
        await call.exchange(_OPENING)
        assert abs(call.mapped_speaking_time() - time.time()) < 0.5

        await asyncio.sleep(2.0)
        await call.handoff()

        drift = call.mapped_speaking_time() - time.time()
        assert abs(drift) < 0.5, f"STT clock drifted {drift:+.2f}s across the handoff"


async def test_transcript_only_turn_after_handoff_is_answered_promptly() -> None:
    # The dev call's exact failure: a quiet turn after a handoff.
    async with CascadeHarness() as call:
        await call.exchange_then_handoff(_OPENING)

        call.phantom_transcript("Yeah.")

        assert await call.wait_for_reply(within=_MAX_ENDPOINTING_DELAY + 1.0)


async def test_faint_vad_energy_keeps_the_stt_clock_out_of_the_path() -> None:
    # Why the bug was intermittent, and why the t=442.7 turn answered in 0.13s: an
    # INFERENCE_DONE too quiet to count as speech still advances `_last_speaking_time`, so
    # the STT timestamp is never consulted. It creates no `user_speaking` span, which is
    # exactly why the trace cannot tell that case apart from a stalled one.
    async with CascadeHarness() as call:
        await call.exchange_then_handoff(_OPENING)

        call.vad_hears_speech()
        call.phantom_transcript("Yeah.")

        assert await call.wait_for_reply(within=_MAX_ENDPOINTING_DELAY + 1.0)


async def test_no_turn_leaves_a_generated_reply_unspoken(otel_spans: Any) -> None:
    # The dev trace's fingerprint, asserted as absent: an `agent_turn` holding a completed
    # `llm_node` and no `tts_node` is a reply that was generated and never reached TTS.
    async with CascadeHarness() as call:
        await call.exchange_then_handoff(_OPENING)
        call.phantom_transcript("Yeah.")
        assert await call.wait_for_reply(within=_MAX_ENDPOINTING_DELAY + 1.0)
        await call.wait_until_listening()

    children: dict[int, list[str]] = {}
    for span in otel_spans.get_finished_spans():
        if span.parent:
            children.setdefault(span.parent.span_id, []).append(span.name)
    turns = [
        children.get(s.context.span_id, [])
        for s in otel_spans.get_finished_spans()
        if s.name == "agent_turn"
    ]

    assert ["llm_node"] not in turns, "a generated reply never reached TTS"


async def test_one_llm_call_per_spoken_reply() -> None:
    # No nudge needed, so no discarded generation. Pre-1.6.4 this turn cost two LLM calls:
    # one parked forever, one prompted by the caller giving up.
    async with CascadeHarness() as call:
        await call.exchange_then_handoff(_OPENING)

        call.phantom_transcript("Yeah.")

        assert await call.wait_for_reply(within=_MAX_ENDPOINTING_DELAY + 1.0)
        assert call.agent.llm_calls == 1


@pytest.mark.xfail(
    strict=True,
    reason="a second, independent route into the same dead air, unfixed as of 1.6.7: a VAD "
    "segment that opens inside the endpointing window and never closes cancels the "
    "end-of-turn task and nothing re-arms the commit. Not what bit the dev call.",
)
async def test_unclosed_vad_segment_also_wedges_the_commit() -> None:
    async with CascadeHarness() as call:
        await call.exchange(_OPENING)

        await call.wedge_turn()

        assert await call.wait_for_reply()


async def test_closed_vad_segment_recovers() -> None:
    # Same race as above but the segment ends, so END_OF_SPEECH re-runs the EOU.
    async with CascadeHarness() as call:
        await call.exchange(_OPENING)
        await call.wedge_turn()
        await asyncio.sleep(0.1)
        call.vad_ends()

        assert await call.wait_for_reply()

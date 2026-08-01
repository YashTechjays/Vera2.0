"""Drives a REAL AgentSession over the REAL cascade turn-handling config, with the
STT/VAD event streams scripted frame-by-frame.

Why this exists: the stuck-turn failure lives entirely in livekit's STT -> VAD ->
turn-commit state machine, and every other test path in this repo is blind to it.
The evals have no STT at all, and `AgentSession.run(user_input=...)` calls
`generate_reply` directly, bypassing `AudioRecognition`. So the only way to reproduce
a turn that never commits is to feed the recognizer the exact event sequence a real
call produced.

Everything vendor-facing is faked (no Deepgram / Vertex / Cartesia / network), but the
pieces under test are the production ones: `cascade_session_kwargs`' turn_handling
block, and livekit's own AgentActivity/AudioRecognition. The turn detector is stubbed
because `EnglishModel` resolves its InferenceExecutor from `get_job_context()`, which
does not exist outside a worker job — and the EOU probability is not what's broken.

Scripted rather than acoustic on purpose: a mic-and-speakers repro depends on the host's
echo cancellation and on Silero's activation threshold, so it reproduces the failure only
sometimes. Injecting the events makes it deterministic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    APIConnectOptions,
    ModelSettings,
    llm,
    stt,
    tts,
    utils,
    vad,
)
from livekit.agents.voice import io

from agent_worker.cascade import cascade_session_kwargs

_SAMPLE_RATE = 16000
_FRAME_SAMPLES = 160
_FRAME_INTERVAL = _FRAME_SAMPLES / _SAMPLE_RATE
_REPLY_FRAMES = 200
# Comfortably inside the cascade's 0.3s endpointing min_delay, so a scripted event lands
# while the end-of-turn task is still in its sleep.
_EVENT_GAP = 0.05
_SETTLE = 0.2


def _silence_frame() -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=b"\x00" * (_FRAME_SAMPLES * 2),
        sample_rate=_SAMPLE_RATE,
        num_channels=1,
        samples_per_channel=_FRAME_SAMPLES,
    )


class _SilenceInput(io.AudioInput):
    """Real-time silence, so the recognizer's audio clock keeps advancing."""

    def __init__(self) -> None:
        super().__init__(label="scripted-silence")

    async def __anext__(self) -> rtc.AudioFrame:
        await asyncio.sleep(_FRAME_INTERVAL)
        return _silence_frame()


class CapturingOutput(io.AudioOutput):
    """Records every frame the agent tries to play and signals the first one.

    Advertises `pause=True` to match the production room output, so the session's
    `resume_false_interruption` behaves as it does on a real call. Pause itself is a
    no-op here; assertions are on frames reaching the sink, not on real-time playout.
    """

    def __init__(self) -> None:
        super().__init__(label="capturing", capabilities=io.AudioOutputCapabilities(pause=True))
        self.frames: list[rtc.AudioFrame] = []
        self.spoke = asyncio.Event()
        self._captured_in_segment = False

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        self.frames.append(frame)
        self._captured_in_segment = True
        self.spoke.set()

    def flush(self) -> None:
        super().flush()
        if not self._captured_in_segment:
            return
        self._captured_in_segment = False
        self.on_playback_finished(
            playback_position=len(self.frames) * _FRAME_INTERVAL, interrupted=False
        )

    def clear_buffer(self) -> None:
        self._captured_in_segment = False


class _ScriptedVADStream(vad.VADStream):
    async def _main_task(self) -> None:
        async for _ in self._input_ch:
            pass

    def push(self, ev: vad.VADEvent) -> None:
        self._event_ch.send_nowait(ev)


class ScriptedVAD(vad.VAD):
    """A VAD that only ever emits what the test tells it to."""

    def __init__(self) -> None:
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.032))
        self.streams: list[_ScriptedVADStream] = []

    def stream(self) -> vad.VADStream:
        created = _ScriptedVADStream(self)
        self.streams.append(created)
        return created

    def push(self, ev: vad.VADEvent) -> None:
        self.streams[-1].push(ev)


class _ScriptedSpeechStream(stt.SpeechStream):
    """A real streaming STT connection whose transcripts are scripted, but whose
    TIMESTAMPS are computed the way Deepgram Flux computes them.

    Flux reports `end_time = audio_window_end + start_time_offset` (deepgram/stt_v2.py:595),
    where `audio_window_end` is this connection's own position in the audio it has received.
    `start_time_offset` is set once by the default `Agent.stt_node` to
    `now - _input_started_at` (agent.py:447), aligning the two clocks at stream creation.
    Reproducing that arithmetic is the whole point: the misalignment only appears when a
    reused stream's offset is consumed against a *different* `_input_started_at`.
    """

    def __init__(self, parent: stt.STT) -> None:
        super().__init__(stt=parent, conn_options=APIConnectOptions(max_retry=0, timeout=30.0))
        self.audio_seconds = 0.0
        self._scripted: asyncio.Queue[tuple[str, float | None]] = asyncio.Queue()

    def say(self, text: str, *, end_time: float | None = None) -> None:
        self._scripted.put_nowait((text, end_time))

    async def _run(self) -> None:
        async def count_audio() -> None:
            async for frame in self._input_ch:
                if isinstance(frame, rtc.AudioFrame):
                    self.audio_seconds += frame.duration

        counter = asyncio.create_task(count_audio())
        try:
            while True:
                text, override = await self._scripted.get()
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        alternatives=[
                            stt.SpeechData(
                                language="en",
                                text=text,
                                confidence=0.95,
                                end_time=(
                                    self.audio_seconds + self.start_time_offset
                                    if override is None
                                    else override
                                ),
                            )
                        ],
                    )
                )
        finally:
            await utils.aio.cancel_and_wait(counter)


class _FakeSTT(stt.STT):
    """Streaming STT with scripted transcripts. Deliberately a real `stream()` rather than
    an `Agent.stt_node` override, because livekit only reuses the STT pipeline across a
    handoff when neither agent overrides that node (agent_activity.py:615-621) — and that
    reuse is the mechanism under test."""

    def __init__(self) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=True))
        self.streams: list[_ScriptedSpeechStream] = []

    async def _recognize_impl(self, *args: Any, **kwargs: Any) -> stt.SpeechEvent:
        raise AssertionError("only streaming recognition is used")

    def stream(self, **kwargs: Any) -> stt.SpeechStream:
        created = _ScriptedSpeechStream(self)
        self.streams.append(created)
        return created

    def say(self, text: str, *, end_time: float | None = None) -> None:
        self.streams[-1].say(text, end_time=end_time)


class _FakeLLM(llm.LLM):
    def chat(self, *args: Any, **kwargs: Any) -> llm.LLMStream:
        raise AssertionError("llm_node is scripted; the LLM object must never be called")


class _FakeTTS(tts.TTS):
    def __init__(self) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=_SAMPLE_RATE,
            num_channels=1,
        )

    def synthesize(self, *args: Any, **kwargs: Any) -> tts.ChunkedStream:
        raise AssertionError("tts_node is scripted; the TTS object must never be called")


class _StubTurnDetector:
    """Always confident the user finished, so endpointing uses `min_delay`."""

    model = "stub-eou"
    provider = "test"

    async def unlikely_threshold(self, language: str | None) -> float | None:
        return 0.15

    async def supports_language(self, language: str | None) -> bool:
        return True

    async def predict_end_of_turn(self, chat_ctx: llm.ChatContext) -> float:
        return 0.95


class ScriptedAgent(Agent):
    """Counts LLM generations and emits a fixed reply, so a generated-but-unspoken
    turn shows up as `llm_calls` outrunning the replies actually played."""

    def __init__(self) -> None:
        super().__init__(instructions="reply with one short sentence")
        self.llm_calls = 0

    # NOTE: `stt_node` is deliberately NOT overridden — see `_FakeSTT`.

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncGenerator[str, None]:
        self.llm_calls += 1
        yield "Sure, I can help with that."

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncGenerator[rtc.AudioFrame, None]:
        async for _ in text:
            pass
        for _ in range(_REPLY_FRAMES):
            yield _silence_frame()


class CascadeHarness:
    """One scripted call. Use `async with`."""

    def __init__(self) -> None:
        self.vad = ScriptedVAD()
        self.stt = _FakeSTT()
        self.output = CapturingOutput()
        self.agent = ScriptedAgent()
        self.session: AgentSession[None] = AgentSession(
            stt=self.stt,
            llm=_FakeLLM(),
            tts=_FakeTTS(),
            vad=self.vad,
            **cascade_session_kwargs(turn_detector=_StubTurnDetector()),
        )
        self.session.input.audio = _SilenceInput()
        self.session.output.audio = self.output

    async def __aenter__(self) -> CascadeHarness:
        await self.session.start(self.agent, record=False)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.session.aclose()

    def _vad_event(self, kind: vad.VADEventType, *, speech: float, silence: float) -> vad.VADEvent:
        return vad.VADEvent(
            type=kind,
            samples_index=0,
            timestamp=asyncio.get_running_loop().time(),
            speech_duration=speech,
            silence_duration=silence,
            speaking=kind is vad.VADEventType.START_OF_SPEECH,
            raw_accumulated_speech=speech,
        )

    @property
    def input_started_at(self) -> float:
        """The recognizer's wall-clock anchor for STT stream timestamps. Reached through
        private state on purpose: this anchor is the value the bug re-seeds on handoff
        (audio_recognition.py:618) and then consumes at audio_recognition.py:869."""
        recognition = self.session._activity._audio_recognition  # type: ignore[union-attr]
        assert recognition._input_started_at is not None, "no audio pushed yet"
        return float(recognition._input_started_at)

    def vad_starts(self) -> None:
        self.vad.push(self._vad_event(vad.VADEventType.START_OF_SPEECH, speech=0.0, silence=0.0))

    def vad_ends(self, *, speech_duration: float = 0.2) -> None:
        self.vad.push(
            self._vad_event(vad.VADEventType.END_OF_SPEECH, speech=speech_duration, silence=0.4)
        )

    def vad_hears_speech(self, *, speech_duration: float = 0.2) -> None:
        """An INFERENCE_DONE carrying speech energy, which is what advances the
        recognizer's `_last_speaking_time` on a real call."""
        self.vad.push(
            self._vad_event(vad.VADEventType.INFERENCE_DONE, speech=speech_duration, silence=0.0)
        )

    def phantom_transcript(self, text: str, *, lands_at_offset: float | None = None) -> None:
        """A final transcript with NO VAD speech segment around it — audio Deepgram
        transcribed but Silero never scored as speech.

        By default the stream timestamps it honestly, the way Flux would. Pass
        `lands_at_offset` to force the mapped utterance end to `now + offset`, which is how
        the delay is measured independently of whether a handoff has drifted the clocks.
        """
        override = None
        if lands_at_offset is not None:
            override = (time.time() + lands_at_offset) - self.input_started_at
        self.stt.say(text, end_time=override)

    def mapped_speaking_time(self, *, end_time: float | None = None) -> float:
        """Where livekit will place the next transcript's utterance end on the wall clock —
        `end_time + _input_started_at` (audio_recognition.py:869). Ahead of `time.time()`
        means the end-of-turn task will sleep by the difference."""
        stream = self.stt.streams[-1]
        window_end = (
            stream.audio_seconds + stream.start_time_offset if end_time is None else end_time
        )
        return window_end + self.input_started_at

    async def handoff(self) -> ScriptedAgent:
        """Swap in a successor agent, as a task_complete handoff does. Neither agent
        overrides `stt_node`, so livekit reuses the live STT stream across the swap."""
        successor = ScriptedAgent()
        self.session.update_agent(successor)
        await self.wait_until_listening()
        # The new activity's audio anchor is only set once a frame arrives, so give the
        # 10ms input a few frames to land before anything reads it.
        await asyncio.sleep(_SETTLE)
        self.agent = successor
        return successor

    async def user_says(self, text: str, *, speech_duration: float = 1.0) -> None:
        """A healthy turn: VAD hears speech, Deepgram transcribes it, VAD hears it end."""
        self.vad_starts()
        self.vad_hears_speech()
        await asyncio.sleep(_EVENT_GAP)
        self.phantom_transcript(text)
        self.vad_hears_speech(speech_duration=speech_duration)
        await asyncio.sleep(_EVENT_GAP)
        self.vad_ends(speech_duration=speech_duration)

    async def wedge_turn(self, text: str = "Yeah.") -> None:
        """The stuck-turn sequence: a transcript, then a VAD segment that opens inside the
        endpointing window and never closes."""
        self.phantom_transcript(text)
        await asyncio.sleep(_EVENT_GAP)
        self.vad_starts()

    async def wait_for_reply(self, within: float = 3.0) -> bool:
        """True if the agent played any audio before `within` seconds elapse."""
        self.output.spoke.clear()
        try:
            async with asyncio.timeout(within):
                await self.output.spoke.wait()
        except TimeoutError:
            return False
        return True

    async def wait_until_listening(self, within: float = 5.0) -> None:
        """Block until the agent's audio has finished and the session is idle again."""
        idle = asyncio.Event()

        def _on_state(ev: Any) -> None:
            if ev.new_state == "listening":
                idle.set()

        if self.session.agent_state == "listening":
            return
        self.session.on("agent_state_changed", _on_state)
        try:
            async with asyncio.timeout(within):
                await idle.wait()
        finally:
            self.session.off("agent_state_changed", _on_state)

    async def exchange(self, text: str) -> None:
        """One complete healthy round trip, ending with the agent back to listening."""
        await self.user_says(text)
        assert await self.wait_for_reply(), "harness: agent never replied to a VAD-backed turn"
        await self.wait_until_listening()

    async def exchange_then_handoff(self, text: str, *, call_age: float = 2.0) -> None:
        """One healthy turn, a pause so the call has some age behind it, then a handoff.
        `call_age` is what a desynchronized clock would drift by, so it has to exceed the
        endpointing delay for a stalled turn to be distinguishable from a slow one."""
        await self.exchange(text)
        await asyncio.sleep(call_age)
        await self.handoff()

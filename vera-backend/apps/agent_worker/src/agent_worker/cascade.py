"""Deepgram(Flux) -> Gemini -> Cartesia cascade, latency-tuned per the POC.

The biggest wins: preemptive_generation fed by Deepgram Flux's eager EOT, and
Gemini thinking_budget=0. Keep EnglishModel turn detection — dropping it falls
back to dumb VAD-silence detection, which is worse.

Self-hosted LiveKit OSS only — never LiveKit Cloud. So no `livekit.agents.inference.*`
feature may be used: those run on LiveKit's hosted gateway (agent-gateway.livekit.cloud),
which we don't have. Left to auto-detect, interruption ("barge-in") picks the *adaptive*
ML detector, which 401s against that gateway and would stream call audio (PHI) off-box;
we pin `interruption.mode="vad"` (local Silero VAD) instead. Anything model-backed here
must stay a local/plugin model. The mode lives under `turn_handling`, which is mutually
exclusive with the deprecated flat AgentSession kwargs — so the WHOLE config (endpointing,
preemptive_generation, turn_detection, interruption) is expressed there, or the omitted
pieces are silently dropped.
"""

from typing import Any

from google.genai.types import ThinkingConfig
from livekit.agents import AgentSession
from livekit.plugins import cartesia, deepgram, google, silero
from livekit.plugins.turn_detector.english import EnglishModel

from agent_worker.intervention import TakeoverState

_VAD_SILENCE_DURATION = 0.4


def _build_vad() -> Any:
    return silero.VAD.load(min_silence_duration=_VAD_SILENCE_DURATION)


def cascade_session_kwargs(turn_detector: Any) -> dict[str, Any]:
    # All turn handling in one `turn_handling` block (mutually exclusive with the
    # deprecated flat kwargs). `interruption.mode="vad"` keeps barge-in on the local
    # Silero VAD — never the Cloud-only adaptive detector (see module docstring).
    return {
        "turn_handling": {
            "endpointing": {"min_delay": 0.3, "max_delay": 0.6},
            "preemptive_generation": {"enabled": True},
            "turn_detection": turn_detector,
            "interruption": {
                "mode": "vad",
                "min_duration": 0.5,
                "false_interruption_timeout": 2.0,
                "resume_false_interruption": True,
            },
        },
    }


def stt_kwargs(key_terms: list[str] | None) -> dict[str, Any]:
    """Optional Deepgram keyterm prompting — the CallPlan's session-wide
    `stt_key_terms`, fed verbatim. Empty/None → no kwarg at all."""
    return {"keyterm": key_terms} if key_terms else {}


def build_session(
    vad: Any | None = None, *, key_terms: list[str] | None = None
) -> AgentSession[TakeoverState]:
    # userdata carries the takeover latch: it must exist from session construction —
    # agents read it before speaking/hanging up, and LiveKit raises on unset userdata.
    return AgentSession(
        userdata=TakeoverState(),
        stt=deepgram.STTv2(
            model="flux-general-en", eager_eot_threshold=0.5, **stt_kwargs(key_terms)
        ),
        llm=google.LLM(
            model="gemini-2.5-flash",
            vertexai=True,
            thinking_config=ThinkingConfig(thinking_budget=0),
        ),
        tts=cartesia.TTS(model="sonic-3.5", emotion=["confident"]),
        vad=vad if vad is not None else _build_vad(),
        **cascade_session_kwargs(turn_detector=EnglishModel()),
    )

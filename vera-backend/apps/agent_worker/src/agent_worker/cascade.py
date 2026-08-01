"""Deepgram(Flux) -> Gemini -> Cartesia cascade, latency-tuned per the POC.

The biggest wins: preemptive_generation fed by Deepgram Flux's eager EOT, and a
minimal Gemini thinking config (`thinking_budget=0` on pre-3 models, the lowest
`thinking_level="low"` on Gemini 3, which has no zero-reasoning setting — see
`resolve_thinking_attrs`). Keep EnglishModel turn detection — dropping it falls
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

import logging
from typing import Any

from google.genai.types import ThinkingConfig
from livekit.agents import AgentSession
from livekit.plugins import cartesia, deepgram, google, silero
from livekit.plugins.turn_detector.english import EnglishModel

from agent_worker.intervention import TakeoverState
from vera_core.services.model_config import (
    ThinkingOverride,
    is_gemini_3_model,
    validate_extra_config,
)

logger = logging.getLogger("agent_worker")

# Pinned, never the floating "sonic-3.5" alias Cartesia moves without notice: the readback
# workaround and its removal test are verified against exactly this snapshot, so unpinning and
# deleting agent_worker.cartesia_workaround are one change.
_CARTESIA_TTS_MODEL = "sonic-3.5-2026-05-04"

_VAD_SILENCE_DURATION = 0.4


def _build_vad() -> Any:
    return silero.VAD.load(min_silence_duration=_VAD_SILENCE_DURATION)


def resolve_llm_model(llm_model: str | None, default_model: str) -> str:
    """The runtime override if set (non-empty), else the caller-supplied default
    (Settings.voice_llm_default_model — its own setting, not shared with any other
    model config in the system)."""
    return llm_model or default_model


def resolve_thinking_attrs(
    model: str, thinking_override: dict[str, Any] | None
) -> dict[str, int | str]:
    """The resolved (budget-or-level) value in plain-value form — always exactly the one
    key the model's family accepts, holding a value that family accepts. No override +
    Gemini 3 -> an explicit "low" (not an empty ThinkingConfig left for the plugin's own
    private auto-selection) so this is always accurate.

    Re-validated here through the SAME ThinkingOverride/validate_extra_config the save path
    uses, rather than trusting the stored dict: `extra_config` is unconstrained JSONB (no
    CHECK), so a hand-edited or future writer's row can hold the wrong key for the resolved
    model, an out-of-enum level, or a non-int budget. Each is worse than it looks —
    ThinkingConfig is extra="forbid", so a bad key raises here and drops the call at session
    setup; an out-of-enum thinking_level only warns locally before being sent to the API; and
    a pre-3 model paired with a thinking_level raises inside google.LLM's chat(), on the first
    live turn, crashing mid-call. Falling back to the family default keeps a bad row
    cosmetic, and reusing the save-path validator means the two can't drift apart."""
    default: dict[str, int | str] = (
        {"thinking_level": "low"} if is_gemini_3_model(model) else {"thinking_budget": 0}
    )
    if not thinking_override:
        return default
    try:
        override = ThinkingOverride(**thinking_override)
        validate_extra_config(model, override)
    except ValueError as exc:
        logger.warning(
            "thinking override %s is not usable for model %s (%s) — using default",
            sorted(thinking_override),
            model,
            type(exc).__name__,
        )
        return default
    # _exactly_one guarantees this is the single key the family accepts; the same
    # model_dump save_llm_model writes the column with.
    return override.model_dump(exclude_none=True)


def resolve_thinking_config(model: str, thinking_override: dict[str, Any] | None) -> ThinkingConfig:
    return ThinkingConfig(**resolve_thinking_attrs(model, thinking_override))


def llm_trace_attributes(model: str, thinking_attrs: dict[str, int | str]) -> dict[str, str | int]:
    return {"vera.llm.model": model, **{f"vera.llm.{k}": v for k, v in thinking_attrs.items()}}


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
    vad: Any | None = None,
    *,
    key_terms: list[str] | None = None,
    llm_model: str | None = None,
    thinking_override: dict[str, Any] | None = None,
    default_model: str,
) -> AgentSession[TakeoverState]:
    model = resolve_llm_model(llm_model, default_model)
    # The latch must exist from construction: agents read it before speaking or hanging up.
    return AgentSession(
        userdata=TakeoverState(),
        stt=deepgram.STTv2(
            model="flux-general-en", eager_eot_threshold=0.5, **stt_kwargs(key_terms)
        ),
        llm=google.LLM(
            model=model,
            vertexai=True,
            location="global",
            thinking_config=resolve_thinking_config(model, thinking_override),
        ),
        tts=cartesia.TTS(model=_CARTESIA_TTS_MODEL, emotion=["confident"]),
        vad=vad if vad is not None else _build_vad(),
        **cascade_session_kwargs(turn_detector=EnglishModel()),
    )

"""Agent persona for the Vera infertility-verification voice worker.

Chat-only slice: no tool machinery. The cascade agent imports these strings
and wires up the LLM pipeline.
"""

from __future__ import annotations

import json
import logging

from vera_core.callplan import BASE_PERSONA, CARTESIA_MARKUP_GUIDE, DEFAULT_GREETING
from vera_core.schemas import PersonaTweak

logger = logging.getLogger("agent_worker")

# Shared persona/greeting/TTS constants — single source in vera_core.callplan.persona.
GREETING = DEFAULT_GREETING


def build_instructions(tweak: PersonaTweak | None = None) -> str:
    """Static-fallback instructions: base persona (+ optional tenant extra
    instructions) + the Cartesia readback guide. Used only when no compiled call
    plan is available (Redis down / non-v2 schema); the schema-derived verification
    content comes from the CallPlan's `flat_instructions` on the normal path."""
    parts = [BASE_PERSONA]
    if tweak is not None and tweak.extra_instructions:
        parts.append(tweak.extra_instructions)
    parts.append(CARTESIA_MARKUP_GUIDE)
    return "\n\n".join(parts)


def resolve_greeting(tweak: PersonaTweak | None = None) -> str:
    """The outbound opener: the tenant override when set, else the base greeting."""
    if tweak is not None and tweak.greeting:
        return tweak.greeting
    return GREETING


def parse_persona_tweak(metadata: str | None) -> PersonaTweak:
    """Parse the tenant persona tweak out of LiveKit dispatch metadata.

    The tweak now rides under its own `persona_tweak` key so unrelated dispatch keys
    (enable_ivr_navigation, ivr_playbook, wait_for_speaker, …) never trip its extra="forbid"
    validation. Control plane and worker deploy as separate images, so for one release this
    also accepts the legacy flat shape (the whole dict IS the tweak) — logging a warning — so
    a rollout in either order doesn't silently drop the persona. Fail-safe: any missing, empty,
    or malformed metadata yields the no-op tweak so a bad config never kills a live call
    (mirrors the cascade's fail-safe posture, not the strict PHI seams)."""
    if not metadata:
        return PersonaTweak()
    try:
        payload = json.loads(metadata)
        if isinstance(payload, dict) and "persona_tweak" in payload:
            return PersonaTweak.model_validate(payload.get("persona_tweak") or {})
        # Legacy flat shape from a not-yet-updated control plane — accept for one release.
        tweak = PersonaTweak.model_validate(payload)
        if tweak != PersonaTweak():
            logger.warning("persona tweak arrived in legacy flat metadata shape; update producer")
        return tweak
    except (json.JSONDecodeError, ValueError):
        return PersonaTweak()

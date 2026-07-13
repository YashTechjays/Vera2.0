"""Worker-side prompt helpers.

Plan-only (2026-07-13): the monolithic SYSTEM_PROMPT / GREETING / build_instructions
were removed — the compiled CallPlan is the sole verification prompt source. What
remains is the Cartesia TTS markup guide (appended to every plan agent's instructions,
so CPT codes stay `<spell>`-wrapped) and the tenant persona-tweak parser.
"""

from __future__ import annotations

import json
import logging

from vera_core.schemas import PersonaTweak

logger = logging.getLogger("agent_worker")

CARTESIA_MARKUP_GUIDE = """SPOKEN MARKUP (Cartesia TTS only)
Cartesia Sonic 3.5 sounds natural from plain prose, so keep writing plain sentences — tone comes from your word choice, not markup. Tone and pacing are already set on the voice itself. Only two inline tags are supported, and they are the sole exception to the plain-sentences rule above:

- <spell>...</spell> reads the contents one character at a time, which is the most reliable way to voice a code. Wrap every CPT code in it using the bare digit string, e.g. <spell>58340</spell>, instead of writing the digits out as words. For an ICD-10 code, spell each side of the decimal and say the point in prose, e.g. <spell>Z31</spell> point <spell>89</spell>.
- <break time="200ms"/> inserts a short pause between two distinct thoughts. Use it rarely — at most once per response, and never chain two breaks.

Do not use any other tags (no emotion tags — they are not a Sonic 3.5 feature and will be read aloud). Never speak a tag name out loud. Never wrap a tool call in a tag."""


def parse_persona_tweak(metadata: str | None) -> PersonaTweak:
    """Parse the tenant persona tweak out of LiveKit dispatch metadata.

    The tweak now rides under its own `persona_tweak` key so unrelated dispatch keys
    (enable_ivr_navigation, ivr_playbook, wait_for_speaker, …) never trip its extra="forbid"
    validation. Control plane and worker deploy as separate images, so for one release this
    also accepts the legacy flat shape (the whole dict IS the tweak) — logging a warning — so
    a rollout in either order doesn't silently drop the persona. Fail-safe: any missing, empty,
    or malformed metadata yields the no-op tweak so a bad config never kills a live call
    (the cascade's fail-safe posture)."""
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

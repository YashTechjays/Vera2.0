"""Workaround for a Cartesia sonic-3.5 TTS defect. Delete this module once it is fixed upstream.

sonic-3.5 misreads the first character inside an utterance-initial `<spell>` tag — "Y" is voiced
as "I", so a member ID read to a payer IVR is wrong from its first character. The same tag placed
anywhere but the start of the utterance is read correctly, and `sonic-3` / `sonic-latest` are
unaffected, so this is specific to the pinned 3.5 snapshot. Leading letters seen to break: Y, O,
E, A, W — the fix is positional, so it also covers any we never hit.

A leading comma displaces the tag from that position. It is the only fix that adds nothing the
payer's ASR can transcribe: a spoken lead-in ("It's", "Sure,") also works but risks being captured
as a menu response or spliced into the ID itself. Whitespace, zero-width characters and `<break/>`
are all normalized away before synthesis and do not help.

Verify: uv run --no-project --with certifi python scripts/tts_probe.py --set comma
Remove when a pinned snapshot voices `<spell>YA123456789</spell>` correctly on its own — and unpin
the model in cascade.py at the same time.
"""

SPELL_LEAD_IN = ", "


def guard_utterance_initial_spell(text: str) -> str:
    return SPELL_LEAD_IN + text if text.lstrip().startswith("<spell>") else text

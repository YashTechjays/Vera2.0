"""Chunk-boundary handling for the LLM -> TTS stream.

LLM tokens arrive in arbitrary slices, so "[[NA" + "ME_1]]" is a real case. The
hydrator buffers each chunk and holds back any suffix that could still be the
start of a [[TYPE_N]] token, hydrating only the provably-safe prefix. A bracket
run that grows past any plausible token length is released as-is (ordinary
prose like "[see note 3]" must not stall the stream)."""

import re

from .boundary import PHIBoundary

# A buffer suffix that might still grow into a [[TYPE_N]] token: one or two "["
# then token-ish chars, optionally one closing "]" awaiting its twin.
_PARTIAL_TOKEN_RE = re.compile(r"\[{1,2}[A-Za-z0-9_ \-]{0,40}\]?$")

# Longest legitimate surface form, with slack for LLM-mangled spacing.
_MAX_HOLD = 48


class SpeechStreamHydrator:
    """One per call direction; NOT thread-safe (one stream, one consumer)."""

    def __init__(self, boundary: PHIBoundary, session_id: str) -> None:
        self._boundary = boundary
        self._session_id = session_id
        self._buffer = ""

    async def feed(self, chunk: str) -> str:
        """Add a chunk; return hydrated text that is safe to speak now."""
        self._buffer += chunk
        match = _PARTIAL_TOKEN_RE.search(self._buffer)
        if match is None or len(match.group(0)) > _MAX_HOLD:
            safe, self._buffer = self._buffer, ""
        else:
            safe, self._buffer = self._buffer[: match.start()], self._buffer[match.start() :]
        if not safe:
            return ""
        return await self._boundary.hydrate_for_speech(self._session_id, safe)

    async def flush(self) -> str:
        """End of stream: hydrate whatever is held, partial or not."""
        rest, self._buffer = self._buffer, ""
        if not rest:
            return ""
        return await self._boundary.hydrate_for_speech(self._session_id, rest)

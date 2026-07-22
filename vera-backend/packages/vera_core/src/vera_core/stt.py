"""Fault-tolerant one-shot speech-to-text for coaching's hold-to-whisper feature —
NOT the live voice cascade's continuous STT (that stays in the agent worker's
AgentSession/cascade config). Mirrors `vera_core.llm.ResilientLLM`'s shape: an
ordered provider chain, construction-time validation, lazy client build, PHI-safe
logging — wrapping livekit-agents' STT-side `FallbackAdapter` (try-primary-then-
fallback, with automatic recovery) instead of hand-rolling the chain.

PHI: whisper audio and its transcript are PHI. Nothing in this module logs audio
bytes or transcript text — only exception type names and provider/model labels.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from livekit import rtc

from vera_core.config.secrets import SecretProvider

if TYPE_CHECKING:
    import aiohttp
    from livekit.agents.stt import STT

logger = logging.getLogger(__name__)

DEEPGRAM_API_KEY_SECRET = "DEEPGRAM_API_KEY"
ASSEMBLYAI_API_KEY_SECRET = "ASSEMBLYAI_API_KEY"

# Matches the takeover transcriber's per-track STT sample rate and Deepgram
# STTv2's own default — no resampling needed between decode output and input.
_DECODE_SAMPLE_RATE = 16000

type ProviderFactory = Callable[
    ["STTSpec", SecretProvider | None, "aiohttp.ClientSession"], "STT[Any]"
]


class STTUnavailableError(Exception):
    """Every provider in the chain failed, or none could even be constructed.
    Carries no audio/transcript content."""


@dataclass(frozen=True)
class STTSpec:
    """One provider/model selector, e.g. STTSpec("deepgram", "flux-general-en")."""

    provider: str
    model: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, selector: str) -> STTSpec:
        """Parse a "provider:model" selector (the settings/env representation)."""
        provider, sep, model = selector.partition(":")
        if not sep or not provider.strip() or not model.strip():
            raise ValueError(f"invalid STT selector {selector!r}; expected 'provider:model'")
        return cls(provider=provider.strip(), model=model.strip())


def _build_deepgram(
    spec: STTSpec, secrets: SecretProvider | None, http_session: aiohttp.ClientSession
) -> STT[Any]:
    from livekit.plugins import deepgram

    if secrets is None:
        raise ValueError("deepgram provider requires a SecretProvider for DEEPGRAM_API_KEY")
    # An explicit session is required outside the agent worker's job context —
    # left unset, the plugin looks up a job-scoped session that only exists
    # inside a real LiveKit job, and this runs in the control plane instead.
    return deepgram.STTv2(
        model=spec.model,
        sample_rate=_DECODE_SAMPLE_RATE,
        api_key=secrets.get(DEEPGRAM_API_KEY_SECRET),
        http_session=http_session,
        **spec.extra,
    )


def _build_assemblyai(
    spec: STTSpec, secrets: SecretProvider | None, http_session: aiohttp.ClientSession
) -> STT[Any]:
    # AssemblyAI isn't provisioned yet (per the coaching proposal): neither the
    # livekit-plugins-assemblyai package nor ASSEMBLYAI_API_KEY exists. This factory
    # keeps the provider slot real — until both are added the import raises
    # ModuleNotFoundError (or the secret lookup fails), which ResilientSTT's
    # construction-time try/except drops from the chain with a warning, never
    # blocking a healthy Deepgram primary.
    from livekit.plugins import assemblyai  # type: ignore[attr-defined]

    if secrets is None:
        raise ValueError("assemblyai provider requires a SecretProvider for ASSEMBLYAI_API_KEY")
    stt: STT[Any] = assemblyai.STT(
        api_key=secrets.get(ASSEMBLYAI_API_KEY_SECRET), http_session=http_session, **spec.extra
    )
    return stt


PROVIDERS: Mapping[str, ProviderFactory] = {
    "deepgram": _build_deepgram,
    "assemblyai": _build_assemblyai,
}


async def _decode_to_frames(audio: bytes, mime_type: str) -> list[rtc.AudioFrame]:
    """Decode an encoded audio blob (webm/opus, wav, mp3, ... — whatever
    FFmpeg/PyAV recognizes) into 16kHz mono PCM frames."""
    from livekit.agents.utils.codecs import AudioStreamDecoder

    # Strip codec params ("audio/webm;codecs=opus" -> "audio/webm") — the
    # decoder's mime table only matches on the bare container type.
    container = mime_type.split(";", 1)[0].strip() or None
    decoder = AudioStreamDecoder(sample_rate=_DECODE_SAMPLE_RATE, num_channels=1, format=container)
    decoder.push(audio)
    decoder.end_input()
    frames: list[rtc.AudioFrame] = []
    try:
        async for frame in decoder:
            frames.append(frame)
    finally:
        await decoder.aclose()
    return frames


class ResilientSTT:
    """Fault-tolerant one-shot transcription over an ordered provider chain.

    Providers are validated at construction; the underlying plugin clients and
    the FallbackAdapter are built lazily on first transcribe() call (STT clients
    open network sessions that need a running event loop, same rule as
    ResilientLLM). Call aclose() at shutdown.
    """

    def __init__(
        self,
        primary: STTSpec,
        fallbacks: Sequence[STTSpec] = (),
        *,
        secrets: SecretProvider | None = None,
        registry: Mapping[str, ProviderFactory] | None = None,
    ) -> None:
        self._specs: list[STTSpec] = [primary, *fallbacks]
        self._secrets = secrets
        self._registry = PROVIDERS if registry is None else registry
        for spec in self._specs:
            if spec.provider not in self._registry:
                raise ValueError(f"unknown STT provider {spec.provider!r}")
        self._stts: list[STT[Any]] = []
        self._chain: Any = None
        self._http_session: aiohttp.ClientSession | None = None

    def _adapter(self) -> Any:
        if self._chain is None:
            import aiohttp
            from livekit.agents.stt import FallbackAdapter

            # Plugins need an explicit session here: left unset, they look up a
            # job-scoped session that only exists inside a real agent worker
            # job, and this class runs in the control plane instead. Owned by
            # this instance, closed in aclose().
            self._http_session = aiohttp.ClientSession()

            # A provider whose client can't even be constructed (missing plugin
            # package, absent secret) is dropped from the chain with a warning —
            # a misconfigured fallback must not take down a healthy primary.
            # Only an empty chain is fatal.
            stts: list[STT[Any]] = []
            for spec in self._specs:
                try:
                    stts.append(
                        self._registry[spec.provider](spec, self._secrets, self._http_session)
                    )
                except Exception as exc:
                    logger.warning(
                        "STT provider %s (%s) unavailable at construction: %s",
                        spec.provider,
                        spec.model,
                        type(exc).__name__,
                    )
            if not stts:
                raise STTUnavailableError
            self._stts = stts
            self._chain = FallbackAdapter(self._stts)
        return self._chain

    async def transcribe(self, audio: bytes, *, mime_type: str) -> str:
        """Decode + transcribe *audio* as one utterance. Raises
        STTUnavailableError when every provider fails or none can be built."""
        from livekit.agents.stt import SpeechEventType

        frames = await _decode_to_frames(audio, mime_type)
        if not frames:
            return ""
        stream = self._adapter().stream()
        try:
            for frame in frames:
                stream.push_frame(frame)
            stream.end_input()
            parts: list[str] = []
            async for ev in stream:
                if ev.type == SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives:
                    text = ev.alternatives[0].text
                    if text:
                        parts.append(text)
            return " ".join(parts).strip()
        except Exception as exc:  # provider errors may embed request payloads (PHI)
            logger.warning("all STT providers failed: %s", type(exc).__name__)
            raise STTUnavailableError from exc
        finally:
            await stream.aclose()

    async def aclose(self) -> None:
        chain, stts, session = self._chain, self._stts, self._http_session
        self._chain, self._stts, self._http_session = None, [], None
        if chain is not None:
            await chain.aclose()
        for s in stts:
            await s.aclose()
        if session is not None:
            await session.close()

"""PHICodec — the async facade the voice pipeline calls.

Two crossings of the tokenization wall per turn:
  * tokenize()        STT text -> tokens, before the LLM sees it (expensive: runs detection)
  * reidentify()      LLM text -> spoken raw, before TTS (cheap: vault lookup + TTS formatting)
  * reidentify_args() LLM tool args -> exact raw, before the payer connector (cheap: exact lookup)

Presidio is synchronous, so detection is offloaded to a thread and bounded by a
timeout; on timeout we fall back to regex-only detection (never to "no detection",
which would leak PHI). The per-session vault lock serializes the not-thread-safe mapping.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import re

from .config import DEFAULT_CONFIG, CodecConfig, EntityType, resolve_entity_type
from .detection.engine import Detection, DetectionEngine
from .detection.known_values import KnownValue, KnownValueIndex
from .detection.leak_canary import CanaryFinding, scan

# Canary kind -> best-guess type for the emergency (NER-down) redaction path.
_EMERGENCY_TYPE = {
    "ssn_like": EntityType.SSN,
    "phone_like": EntityType.PHONE,
    "long_digit_run": EntityType.BENEFICIARY_ID,
    "email_like": EntityType.EMAIL,
    "alnum_id_like": EntityType.BENEFICIARY_ID,
}
from .detection.normalizer import normalize
from .formatting.tts import format_for_tts
from .tokens.token import LENIENT_TOKEN_RE, TOKEN_RE, canonical_token
from .tokens.tokenizer import Replacement, apply_replacements, resolve_overlaps
from .vault.audit import AuditLog
from .vault.base import PHIVault
from .vault.memory_vault import InMemoryVault

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectedEntity:
    entity_type: str
    raw_text: str
    token: str
    start: int
    end: int
    score: float
    recognizer: str


@dataclass(frozen=True)
class TokenizeResult:
    text_tokenized: str
    normalized_text: str
    entities: list[DetectedEntity]
    leak_ok: bool
    leak_findings: list[CanaryFinding]
    degraded: bool  # True if detection fell back to regex-only
    latency_ms: float
    sanitized_input: bool = False  # input contained token-like delimiters that were neutralized
    detection_failed: bool = False  # NER crashed; emergency canary-only redaction was used


@dataclass(frozen=True)
class ReidentifyResult:
    text: str  # re-identified, TTS-formatted (spoken readback)
    unresolved: list[str] = field(default_factory=list)  # tokens with no vault entry
    latency_ms: float = 0.0
    repaired: list[str] = field(default_factory=list)  # mangled tokens repaired to canonical

    @property
    def ok(self) -> bool:
        return not self.unresolved


class PHICodec:
    def __init__(
        self,
        config: CodecConfig | None = None,
        *,
        engine: DetectionEngine | None = None,
        vault: PHIVault | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.config = config or DEFAULT_CONFIG
        self.engine = engine or DetectionEngine(self.config)
        self.vault = vault or InMemoryVault()
        self.audit = audit or AuditLog()
        self.known = KnownValueIndex(
            phonetic=self.config.use_phonetic_seed,
            phonetic_threshold=self.config.phonetic_threshold,
        )

    async def open_session(self, session_id: str) -> None:
        await self.vault.open_session(session_id)
        self.known.open(session_id)

    async def close_session(self, session_id: str) -> None:
        await self.vault.close_session(session_id)
        self.known.close(session_id)

    async def seed_session(
        self, session_id: str, known: dict[str, str | list[str]]
    ) -> list[dict]:
        """Pre-load known patient PHI and pre-mint its tokens before the call.

        ``known`` maps entity-type names to a value or list of values, e.g.
        ``{"NAME": "John Smith", "BENEFICIARY_ID": "XYZ987654321"}``. Returns a
        summary of the seeded (type, token) pairs. These values are then matched
        deterministically on every turn (tier-0), ahead of the recognizer tiers.
        """
        await self.open_session(session_id)
        seeded: list[dict] = []
        for type_name, raw in known.items():
            etype = resolve_entity_type(type_name)  # canonical, alias, or TYPE_N → EntityType
            values = [raw] if isinstance(raw, str) else list(raw)
            for value in values:
                value = value.strip()
                if not value:
                    continue
                token = await self.vault.get_or_create_token(
                    session_id, etype.value, value,
                    turn_id="seed", recognizer="seed", score=1.0,
                )
                self.known.add(
                    session_id,
                    KnownValue(
                        entity_type=etype,
                        normalized=KnownValueIndex.canonicalize(value),
                        token=token,
                        original=value,
                    ),
                )
                seeded.append({"entity_type": etype.value, "token": token})
        return seeded

    # ------------------------------------------------------------------ tokenize
    async def tokenize(self, session_id: str, text: str, *, turn_id: str = "t0") -> TokenizeResult:
        t0 = time.perf_counter()
        # Neutralize any token-like delimiters already in the input so external text can
        # never masquerade as a codec token (injection / collision).
        text, sanitized = _sanitize_input(text)
        normalized = normalize(text)

        detections, degraded, detection_failed = await self._detect_safe(normalized)

        # Tier 0: deterministic matches of seeded patient PHI (score 1.0 → win overlaps).
        known_dets = self.known.match(session_id, normalized)
        kept = resolve_overlaps(known_dets + detections)

        replacements: list[Replacement] = []
        entities: list[DetectedEntity] = []
        await self.vault.touch(session_id)
        for d in kept:
            if d.token is not None:
                # Known-value match: use the token pre-minted at seed time, so the
                # vault keeps the exact record value rather than the STT transcription.
                token = d.token
            else:
                token = await self.vault.get_or_create_token(
                    session_id,
                    d.entity_type.value,
                    d.text,
                    turn_id=turn_id,
                    recognizer=d.recognizer,
                    score=d.score,
                )
            replacements.append(Replacement(d.start, d.end, token))
            entities.append(
                DetectedEntity(
                    entity_type=d.entity_type.value,
                    raw_text=d.text,
                    token=token,
                    start=d.start,
                    end=d.end,
                    score=d.score,
                    recognizer=d.recognizer,
                )
            )
            self.audit.record(
                session_id=session_id,
                turn_id=turn_id,
                direction="tokenize",
                token=token,
                entity_type=d.entity_type.value,
                recognizer=d.recognizer,
                score=d.score,
                raw_value=d.text,
            )

        tokenized = apply_replacements(normalized, replacements)
        canary = scan(tokenized)
        if not canary.ok:
            logger.error("LEAK CANARY tripped on session %s: %s", session_id, canary.findings)

        return TokenizeResult(
            text_tokenized=tokenized,
            normalized_text=normalized,
            entities=entities,
            leak_ok=canary.ok,
            leak_findings=canary.findings,
            degraded=degraded,
            latency_ms=(time.perf_counter() - t0) * 1000,
            sanitized_input=sanitized,
            detection_failed=detection_failed,
        )

    async def _detect_safe(self, normalized: str) -> tuple[list[Detection], bool, bool]:
        """Run detection, never raising. Returns (detections, degraded, failed).

        Order of fallback, each strictly safer: full NER → regex-only (on timeout OR a
        detector exception) → emergency canary redaction (if regex also fails). The last
        resort still tokenizes structured PHI shapes so a detector bug can't leak IDs
        (free-text names can't be recovered without NER — logged via detection_failed).
        """
        try:
            dets = await asyncio.wait_for(
                asyncio.to_thread(self.engine.detect, normalized),
                timeout=self.config.ner_timeout_s if self.config.use_gliner else None,
            )
            return dets, False, False
        except asyncio.TimeoutError:
            logger.warning("detection exceeded %.0fms; regex-only fallback", self.config.ner_timeout_s * 1000)
        except Exception:
            logger.exception("detection engine error; regex-only fallback")
        try:
            return await asyncio.to_thread(self.engine.detect_regex_only, normalized), True, False
        except Exception:
            logger.exception("regex detection failed; emergency canary redaction")
            return self._emergency_detections(normalized), True, True

    @staticmethod
    def _emergency_detections(text: str) -> list[Detection]:
        """Last-resort structured-PHI redaction from leak-canary shapes (no NER)."""
        dets: list[Detection] = []
        for f in scan(text).findings:
            etype = _EMERGENCY_TYPE.get(f.kind, EntityType.UNIQUE_CODE)
            dets.append(Detection(etype, f.start, f.start + len(f.text), 0.5, "emergency", f.text))
        return dets

    # ----------------------------------------------------------------- reidentify
    async def reidentify(self, session_id: str, text: str, *, turn_id: str = "t0") -> ReidentifyResult:
        """TTS path: replace tokens with spoken-formatted raw values. Fail-closed on misses."""
        t0 = time.perf_counter()
        unresolved: list[str] = []

        text, repaired = await self._repair_tokens(session_id, text)

        async def repl(token_surface: str) -> str:
            entry = await self.vault.resolve(session_id, token_surface)
            if entry is None:
                unresolved.append(token_surface)
                return token_surface  # left intact; caller must NOT speak this (see ok)
            self.audit.record(
                session_id=session_id, turn_id=turn_id, direction="reidentify",
                token=token_surface, entity_type=entry.entity_type,
                recognizer=entry.recognizer, score=entry.score,
            )
            return format_for_tts(EntityType(entry.entity_type), entry.raw_value)

        out = await self._async_token_sub(text, repl)
        return ReidentifyResult(
            text=out, unresolved=unresolved, repaired=repaired,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    async def reidentify_args(self, session_id: str, args: dict) -> dict:
        """Tool-call path: replace tokens with the EXACT raw value (no TTS formatting)."""
        async def resolve_value(v):
            if isinstance(v, str):
                v, _ = await self._repair_tokens(session_id, v)
                async def repl(tok: str) -> str:
                    entry = await self.vault.resolve(session_id, tok)
                    return entry.raw_value if entry else tok
                return await self._async_token_sub(v, repl)
            if isinstance(v, dict):
                return {k: await resolve_value(val) for k, val in v.items()}
            if isinstance(v, list):
                return [await resolve_value(item) for item in v]
            return v

        return await resolve_value(args)

    async def _repair_tokens(self, session_id: str, text: str) -> tuple[str, list[str]]:
        """Rewrite LLM-mangled token forms back to canonical, when they resolve in the vault.

        A mangled surface ("[[ name 1 ]]", "[NAME-1]") is canonicalized and only rewritten
        if that canonical token actually exists in the session vault — so this never invents
        a mapping, it only recovers a real token the model garbled. Well-formed tokens are
        left untouched (the strict pass resolves them, fail-closing on unknown ones). An
        unresolvable mangle is left as literal text — we don't flag it, because the lenient
        pattern would otherwise false-positive on ordinary bracketed prose ("[see note 3]").
        """
        repaired: list[str] = []
        out: list[str] = []
        last = 0
        for m in LENIENT_TOKEN_RE.finditer(text):
            out.append(text[last : m.start()])
            surface = m.group(0)
            canon = canonical_token(m.group(1), m.group(2))
            if canon != surface and await self.vault.resolve(session_id, canon) is not None:
                repaired.append(canon)
                logger.warning("repaired mangled token %r -> %s", surface, canon)
                out.append(canon)
            else:
                out.append(surface)
            last = m.end()
        out.append(text[last:])
        return "".join(out), repaired

    @staticmethod
    async def _async_token_sub(text: str, repl) -> str:
        """Replace every [[TOKEN]] using an async replacement fn, preserving order."""
        out: list[str] = []
        last = 0
        for m in TOKEN_RE.finditer(text):
            out.append(text[last : m.start()])
            out.append(await repl(m.group(0)))
            last = m.end()
        out.append(text[last:])
        return "".join(out)


def _sanitize_input(text: str) -> tuple[str, bool]:
    """Neutralize token-like delimiters in raw input so external text can't masquerade
    as a codec token. Collapses runs of 2+ brackets to one, breaking the ``[[...]]`` form."""
    clean = re.sub(r"\[{2,}", "[", text)
    clean = re.sub(r"\]{2,}", "]", clean)
    return clean, clean != text

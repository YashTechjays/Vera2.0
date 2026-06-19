"""Vera's three crossings of the PHI tokenization wall, on top of phi_codec.

  redact              STT text -> tokenized text, before the LLM sees it
  hydrate_for_speech  LLM text -> spoken raw, before TTS.  FAIL-SAFE: an
                      unknown token becomes a neutral phrase and the event is
                      logged + audited — the call goes on, nothing leaks.
  hydrate_raw         LLM tool args -> exact raw, before a payer connector.
                      STRICT: an unresolved token raises and is audited — wrong
                      identifiers must never reach a payer API.

The vault is session-scoped; close_session() wipes it at call end. Audit
records carry tokens/counts only, never raw PHI.
"""

import logging
from typing import Any, cast
from uuid import UUID

from phi_codec.codec import PHICodec
from phi_codec.tokens.token import TOKEN_RE
from vera_core.audit import AuditRecord, AuditSink
from vera_core.models.audit_log import ActorType, AuditEvent

logger = logging.getLogger("vera.phi")

NEUTRAL_PHRASE = "that information"


class UnresolvedPHITokenError(RuntimeError):
    def __init__(self, tokens: list[str]) -> None:
        super().__init__(f"unresolved PHI tokens in tool args: {tokens}")
        self.tokens = tokens


def _find_tokens_in_args(value: Any) -> list[str]:
    if isinstance(value, str):
        return [m.group(0) for m in TOKEN_RE.finditer(value)]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _find_tokens_in_args(v)]
    if isinstance(value, list):
        return [t for v in value for t in _find_tokens_in_args(v)]
    return []


class PHIBoundary:
    """One per process; sessions are keyed by session_id (one per call)."""

    def __init__(self, codec: PHICodec, audit: AuditSink, tenant_id: UUID) -> None:
        self._codec = codec
        self._audit = audit
        self._tenant_id = tenant_id

    async def open_session(
        self, session_id: str, known: dict[str, str | list[str]] | None = None
    ) -> None:
        """Open the per-call vault; optionally pre-seed known patient PHI so its
        tokens are minted before the first turn."""
        if known:
            await self._codec.seed_session(session_id, known)
        else:
            await self._codec.open_session(session_id)

    async def close_session(self, session_id: str) -> None:
        """Wipe the vault at call end — raw PHI does not outlive the call."""
        await self._codec.close_session(session_id)

    async def redact(self, session_id: str, text: str) -> str:
        result = await self._codec.tokenize(session_id, text)
        if result.degraded or result.detection_failed:
            logger.warning(
                "phi detection degraded (session=%s degraded=%s failed=%s)",
                session_id,
                result.degraded,
                result.detection_failed,
            )
        await self._emit(
            AuditEvent.PHI_ACCESS,
            session_id,
            detail={
                "direction": "redact",
                "entities": len(result.entities),
                "degraded": result.degraded,
                "detection_failed": result.detection_failed,
                "leak_ok": result.leak_ok,
            },
        )
        return cast(str, result.text_tokenized)

    async def hydrate_for_speech(self, session_id: str, text: str) -> str:
        """LLM -> TTS. Unknown tokens are replaced with a neutral phrase: the
        agent says \"that information\" instead of leaking a token or dying."""
        result = await self._codec.reidentify(session_id, text)
        out = cast(str, result.text)
        if result.unresolved:
            for token in result.unresolved:
                out = out.replace(token, NEUTRAL_PHRASE)
            logger.warning(
                "fail-safe hydration: %d unresolved token(s) neutralized (session=%s): %s",
                len(result.unresolved),
                session_id,
                result.unresolved,
            )
            await self._emit(
                AuditEvent.PHI_HYDRATE_FAILSAFE,
                session_id,
                detail={"unresolved": result.unresolved, "repaired": result.repaired},
            )
        await self._emit(
            AuditEvent.PHI_ACCESS,
            session_id,
            detail={"direction": "speech", "unresolved": len(result.unresolved)},
        )
        return out

    async def hydrate_raw(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """LLM tool args -> payer connector. Strict: every token must resolve."""
        resolved = cast(dict[str, Any], await self._codec.reidentify_args(session_id, args))
        leftover = _find_tokens_in_args(resolved)
        if leftover:
            await self._emit(
                AuditEvent.PHI_DETOKENIZE,
                session_id,
                decision="deny",
                detail={"unresolved": leftover},
                reason="unresolved tokens in tool args",
            )
            raise UnresolvedPHITokenError(leftover)
        await self._emit(
            AuditEvent.PHI_DETOKENIZE,
            session_id,
            decision="allow",
            detail={"direction": "tool_args", "keys": sorted(args)},
        )
        return resolved

    async def _emit(
        self,
        event: AuditEvent,
        session_id: str,
        *,
        decision: str | None = None,
        detail: dict[str, Any] | None = None,
        reason: str = "",
    ) -> None:
        await self._audit.emit(
            AuditRecord(
                tenant_id=self._tenant_id,
                actor_type=ActorType.SERVICE,
                actor_label="agent_worker",
                event_type=event.value,
                resource_type="phi_session",
                resource_id=session_id,
                decision=decision,
                detail=detail or {},
                reason=reason,
            )
        )

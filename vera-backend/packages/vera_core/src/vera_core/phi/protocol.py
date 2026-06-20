"""Swappable PHI-boundary seam.

The cascade depends only on `PHIBoundaryProtocol`. Today the factory returns the
no-op `PassthroughPHIBoundary` (tokenization is not yet wired). When the codec
lands, the real `vera_core.phi.boundary.PHIBoundary` — which already has this
exact method shape — is returned instead, with zero cascade changes.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PHIBoundaryProtocol(Protocol):
    async def open_session(
        self, session_id: str, known: dict[str, str | list[str]] | None = None
    ) -> None: ...
    async def close_session(self, session_id: str) -> None: ...
    async def redact(self, session_id: str, text: str) -> str: ...
    async def hydrate_for_speech(self, session_id: str, text: str) -> str: ...
    async def hydrate_raw(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]: ...


class PassthroughPHIBoundary:
    """No-op boundary: text flows through unchanged. Synthetic data only."""

    async def open_session(
        self, session_id: str, known: dict[str, str | list[str]] | None = None
    ) -> None:
        pass

    async def close_session(self, session_id: str) -> None:
        pass

    async def redact(self, session_id: str, text: str) -> str:
        return text

    async def hydrate_for_speech(self, session_id: str, text: str) -> str:
        return text

    async def hydrate_raw(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return args

"""Thin wrapper over the LiveKit server SDK: create call rooms, dispatch the
agent worker, place outbound SIP participants, and mint browser join tokens.
Mirrors the build_kms factory shape.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from livekit import api
from livekit.api import TwirpError

from vera_core.config import SecretProvider
from vera_core.config.settings import Settings
from vera_core.observability.correlation import SIP_CALLEE_IDENTITY

AGENT_NAME = "vera-agent"


class LiveKitGateway:
    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
        sip_trunk_id: str | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
        # May be None when outbound SIP is not configured — outbound calls then
        # fail closed at the router before ever reaching create_sip_participant.
        self._sip_trunk_id = sip_trunk_id

    @property
    def url(self) -> str:
        return self._url

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[api.LiveKitAPI]:
        # LiveKitAPI wraps an aiohttp ClientSession, which requires a running
        # event loop — construct it inside the coroutine, not in __init__.
        lk = api.LiveKitAPI(self.url, self._api_key, self._api_secret)
        try:
            yield lk
        finally:
            await lk.aclose()  # type: ignore[no-untyped-call]  # livekit-api missing return annotation

    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        async with self._client() as lk:
            await lk.room.create_room(api.CreateRoomRequest(name=room_name))
            # metadata rides on the dispatch as a JSON string the worker parses
            # (e.g. {"wait_for_speaker": true}); None → "" → existing callers unchanged.
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=AGENT_NAME,
                    room=room_name,
                    metadata=json.dumps(metadata) if metadata else "",
                )
            )

    async def create_sip_participant(self, room_name: str, phone_number: str) -> None:
        """Dial an outbound phone number into the room via the configured SIP trunk.

        The callee's audio joins the room as the SIP-callee participant; the agent and
        any listening monitor hear them once they answer. Requires a trunk id — the
        router enforces that precondition (fail-closed) before calling this.
        """
        if self._sip_trunk_id is None:
            raise ValueError("outbound SIP trunk is not configured")
        async with self._client() as lk:
            await lk.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=self._sip_trunk_id,
                    sip_call_to=phone_number,
                    room_name=room_name,
                    participant_identity=SIP_CALLEE_IDENTITY,
                    participant_name="Outbound callee",
                    wait_until_answered=False,
                )
            )

    async def delete_room(self, room_name: str) -> None:
        """Tear the room down server-side: removes every participant — the agent
        worker (→ its session shuts down) and any SIP callee (→ the outbound call is
        hung up) — and closes the room. Idempotent: deleting an already-gone room
        (e.g. agent's delete_room_on_close already removed it) is a no-op.
        """
        async with self._client() as lk:
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
            except TwirpError as exc:
                if exc.code == "not_found":
                    return  # room already gone — agent's close path deleted it first
                raise

    def mint_join_token(self, room_name: str, identity: str) -> str:
        grants = api.VideoGrants(room_join=True, room=room_name)
        return (
            api.AccessToken(self._api_key, self._api_secret)
            .with_identity(identity)
            .with_grants(grants)
            .to_jwt()
        )


def build_livekit_gateway(settings: Settings, secrets: SecretProvider) -> LiveKitGateway:
    if settings.livekit_url is None:
        raise ValueError("VERA_LIVEKIT_URL must be set to use the LiveKit gateway")
    return LiveKitGateway(
        url=settings.livekit_url,
        api_key=secrets.get("LIVEKIT_API_KEY"),
        api_secret=secrets.get("LIVEKIT_API_SECRET"),
        sip_trunk_id=settings.livekit_sip_trunk_id,
    )

"""LiveKit server SDK wrapper: create call rooms, dispatch the agent worker,
place outbound SIP participants, and mint browser join tokens.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import aiohttp
from livekit import api
from livekit.api.twirp_client import TwirpError

from vera_core.config import SecretProvider
from vera_core.config.settings import Settings
from vera_core.observability.correlation import SIP_CALLEE_IDENTITY
from vera_core.telephony import LiveKitUnavailable, OutboundDialError

__all__ = ["LiveKitGateway", "LiveKitUnavailable", "OutboundDialError", "build_livekit_gateway"]

AGENT_NAME = "vera-agent"

# Backstop room lifetimes (the pipeline sweeper is the primary net). A watching
# supervisor counts as a participant, so neither timeout fires for observer-held
# rooms — the sweeper's observer-only probe handles those.
_ROOM_EMPTY_TIMEOUT_S = 300
_ROOM_DEPARTURE_TIMEOUT_S = 120

# Transport-level SDK failures, re-raised as domain errors so SDK exception types
# never leak to the routers.
_LIVEKIT_TRANSPORT_ERRORS = (TwirpError, aiohttp.ClientError)


class LiveKitGateway:
    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
        agent_name: str = AGENT_NAME,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
        self._agent_name = agent_name

    @property
    def url(self) -> str:
        return self._url

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[api.LiveKitAPI]:
        # LiveKitAPI wraps an aiohttp ClientSession that needs a running event
        # loop — construct it inside the coroutine, not in __init__.
        lk = api.LiveKitAPI(self.url, self._api_key, self._api_secret)
        try:
            yield lk
        finally:
            await lk.aclose()  # type: ignore[no-untyped-call]  # livekit-api missing return annotation

    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        async with self._client() as lk:
            await lk.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=_ROOM_EMPTY_TIMEOUT_S,
                    departure_timeout=_ROOM_DEPARTURE_TIMEOUT_S,
                )
            )
            # metadata rides on the dispatch as a JSON string the worker parses.
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=self._agent_name,
                    room=room_name,
                    metadata=json.dumps(metadata) if metadata else "",
                )
            )

    async def outbound_trunk_exists(self, trunk_id: str) -> bool:
        """True if an outbound SIP trunk with this id exists on the LiveKit SIP service.

        Checks LiveKit's own trunk config only — it does not contact the telephony
        provider. Raises LiveKitUnavailable on lookup failure so the caller can fail closed.
        """
        try:
            async with self._client() as lk:
                resp = await lk.sip.list_outbound_trunk(
                    api.ListSIPOutboundTrunkRequest(trunk_ids=[trunk_id])
                )
        except _LIVEKIT_TRANSPORT_ERRORS as e:
            raise LiveKitUnavailable(str(e)) from e
        return len(resp.items) > 0

    async def create_sip_participant(
        self, room_name: str, phone_number: str, trunk_id: str
    ) -> None:
        """Dial an outbound phone number into the room via the tenant's SIP trunk.

        Raises OutboundDialError if LiveKit/the provider rejects the dial, so the
        router returns a clean upstream error instead of an uncaught 500.
        """
        if not trunk_id:
            # Unreachable from the router (it checks the trunk first); keep the
            # OutboundDialError contract rather than a raw ValueError → 500.
            raise OutboundDialError("outbound SIP trunk is not configured")
        try:
            async with self._client() as lk:
                await lk.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        sip_trunk_id=trunk_id,
                        sip_call_to=phone_number,
                        room_name=room_name,
                        participant_identity=SIP_CALLEE_IDENTITY,
                        participant_name="Outbound callee",
                        wait_until_answered=False,
                    )
                )
        except _LIVEKIT_TRANSPORT_ERRORS as e:
            raise OutboundDialError(str(e)) from e

    async def existing_rooms(self, room_names: list[str]) -> set[str]:
        """The subset of *room_names* that currently exist on the LiveKit server.
        The sweeper uses "room gone but call non-terminal" as the worker-died signal."""
        if not room_names:
            return set()
        async with self._client() as lk:
            resp = await lk.room.list_rooms(api.ListRoomsRequest(names=room_names))
        return {room.name for room in resp.rooms}

    async def room_participant_identities(self, room_name: str) -> list[str] | None:
        """Identities currently in the room, or None when the room doesn't exist.
        The sweeper uses this to spot dead-but-open rooms holding only browser
        observers, which keep the room's departure timeout from ever firing."""
        async with self._client() as lk:
            try:
                resp = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
            except TwirpError as exc:
                if exc.code == "not_found":
                    return None
                raise
        return [p.identity for p in resp.participants]

    async def delete_room(self, room_name: str) -> None:
        """Tear the room down server-side, hanging up the SIP callee and shutting
        down the agent session. Idempotent: deleting an already-gone room is a no-op.
        """
        async with self._client() as lk:
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
            except TwirpError as exc:
                if exc.code == "not_found":
                    return
                raise

    async def set_room_metadata(self, room_name: str, metadata: dict[str, object]) -> None:
        """Set room-level metadata (JSON-encoded). LiveKit pushes it to every
        participant as a RoomMetadataChanged event, so the browser can read session
        status before the room is torn down. Idempotent: teardown paths may race and
        re-enter this after the room is gone, so a missing room is a no-op.
        """
        async with self._client() as lk:
            try:
                await lk.room.update_room_metadata(
                    api.UpdateRoomMetadataRequest(room=room_name, metadata=json.dumps(metadata))
                )
            except TwirpError as exc:
                if exc.code == "not_found":
                    return
                raise

    def mint_join_token(
        self,
        room_name: str,
        identity: str,
        *,
        can_publish: bool = True,
        name: str | None = None,
        attributes: dict[str, str] | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> str:
        # Short TTL vs the SDK default (~6h) replay window; the caller caps intervene
        # tokens at the connect-grace. can_publish=False is a server-side mute the
        # client can't override. name/attributes are workforce identifiers, never PHI.
        grants = api.VideoGrants(room_join=True, room=room_name, can_publish=can_publish)
        token = api.AccessToken(self._api_key, self._api_secret).with_identity(identity)
        if name is not None:
            token = token.with_name(name)
        if attributes:
            token = token.with_attributes(attributes)
        return token.with_grants(grants).with_ttl(ttl).to_jwt()


def build_livekit_gateway(settings: Settings, secrets: SecretProvider) -> LiveKitGateway:
    if settings.livekit_url is None:
        raise ValueError("VERA_LIVEKIT_URL must be set to use the LiveKit gateway")
    return LiveKitGateway(
        url=settings.livekit_url,
        api_key=secrets.get("LIVEKIT_API_KEY"),
        api_secret=secrets.get("LIVEKIT_API_SECRET"),
        agent_name=settings.livekit_agent_name,
    )

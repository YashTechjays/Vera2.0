"""Thin wrapper over the LiveKit server SDK: create call rooms, dispatch the
agent worker, place outbound SIP participants, and mint browser join tokens.
Mirrors the build_kms factory shape.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp
from livekit import api
from livekit.api.twirp_client import TwirpError

from vera_core.config import SecretProvider
from vera_core.config.settings import Settings
from vera_core.observability.correlation import SIP_CALLEE_IDENTITY

AGENT_NAME = "vera-agent"

# Transport-level failures the LiveKit SDK raises: a Twirp API error or an aiohttp
# connection failure. Caught at this gateway boundary and re-raised as domain errors so
# SDK exception types never leak to the routers.
_LIVEKIT_TRANSPORT_ERRORS = (TwirpError, aiohttp.ClientError)


class LiveKitUnavailable(Exception):
    """The LiveKit SIP service could not be reached (or errored) while we probed it —
    e.g. verifying a trunk id exists before storing the credential. Distinct from
    "trunk not found": this means we could not get an answer, so we fail closed."""


class OutboundDialError(Exception):
    """Placing an outbound SIP call failed at the LiveKit / telephony seam — a
    bad/deleted trunk, the provider rejecting the call, or LiveKit being unreachable.
    The router translates this into a clean upstream-error response, never a raw 500."""


class LiveKitGateway:
    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret

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

    async def outbound_trunk_exists(self, trunk_id: str) -> bool:
        """True if an outbound SIP trunk with this id exists on the LiveKit SIP service.

        Existence check against LiveKit's own trunk config only — it does NOT contact
        the telephony provider or verify the trunk's upstream address/credentials
        (LiveKit exercises those solely when a call is placed). Catches the common
        misconfiguration: a wrong/typo'd/deleted trunk id. Raises LiveKitUnavailable if
        LiveKit can't be reached or errors on the lookup, so the caller can fail closed.
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

        The callee's audio joins the room as the SIP-callee participant; the agent and
        any listening monitor hear them once they answer. `trunk_id` is resolved per
        tenant from the integrations table by the caller (fail-closed before this).

        Raises OutboundDialError if LiveKit/the provider rejects the dial (e.g. the
        trunk was deleted after it was stored, or the carrier refuses the call) so the
        router returns a clean upstream error instead of an uncaught 500.
        """
        if not trunk_id:
            # Unreachable from the router (it resolves + checks the trunk first), but if
            # a future caller slips through, keep the OutboundDialError contract rather
            # than surfacing a raw ValueError → 500.
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

    async def delete_room(self, room_name: str) -> None:
        """Tear the room down server-side: removes every participant — the agent
        worker (→ its session shuts down) and any SIP callee (→ the outbound call is
        hung up) — and closes the room. Idempotent: deleting an absent room is a no-op.
        """
        async with self._client() as lk:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))

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
    )

"""Thin wrapper over the LiveKit server SDK: create call rooms, dispatch the
agent worker, place outbound SIP participants, and mint browser join tokens.
Mirrors the build_kms factory shape.
"""

import json
import re
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
    bad/deleted trunk, the callee being busy/declining/not answering, or LiveKit being
    unreachable. Carries the SIP response code (e.g. 486) when the failure was a SIP
    response, and `timed_out=True` when our own client deadline elapsed first, so the
    caller can classify busy/declined/no-answer."""

    def __init__(
        self, message: str, *, sip_status: int | None = None, timed_out: bool = False
    ) -> None:
        super().__init__(message)
        self.sip_status = sip_status
        self.timed_out = timed_out


# A SIP status line embeds a 3-digit 4xx/5xx/6xx response code; pull the first one out of
# the Twirp error text/metadata (best-effort — used only to refine the failure reason).
_SIP_CODE_RE = re.compile(r"\b([4-6]\d{2})\b")


def _extract_sip_status(err: TwirpError) -> int | None:
    haystack = err.message + " " + " ".join(err.metadata.values())
    match = _SIP_CODE_RE.search(haystack)
    return int(match.group(1)) if match else None


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
                    agent_name=self._agent_name,
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
        self,
        room_name: str,
        phone_number: str,
        trunk_id: str,
        *,
        wait_until_answered: bool = False,
        dial_timeout: float | None = None,
    ) -> None:
        """Dial an outbound phone number into the room via the tenant's SIP trunk.

        The callee's audio joins the room as the SIP-callee participant; the agent and
        any listening monitor hear them once they answer. `trunk_id` is resolved per
        tenant from the integrations table by the caller (fail-closed before this).

        With `wait_until_answered=True` this blocks until the call is answered or reaches
        a terminal failure (busy/declined/no-answer), and `timeout` bounds that wait — use
        it in a background task to detect the outcome the dialer would otherwise never see.

        Raises OutboundDialError if the dial fails — a bad/deleted trunk, the carrier
        refusing the call, the callee busy/declining/not answering, or LiveKit being
        unreachable. The error carries `sip_status` / `timed_out` for classification.
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
                        wait_until_answered=wait_until_answered,
                    ),
                    timeout=dial_timeout,
                )
        except TwirpError as e:
            raise OutboundDialError(str(e), sip_status=_extract_sip_status(e)) from e
        except TimeoutError as e:
            # Our client deadline elapsed before LiveKit resolved the ring — treat as no-answer.
            raise OutboundDialError(str(e) or "outbound dial timed out", timed_out=True) from e
        except aiohttp.ClientError as e:
            raise OutboundDialError(str(e)) from e

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

    async def set_room_metadata(self, room_name: str, metadata: dict[str, object]) -> None:
        """Set room-level metadata (JSON-encoded). LiveKit pushes it to every
        participant as a RoomMetadataChanged event, so the browser can read
        session status (e.g. a failed outbound call) before the room is torn down.
        """
        async with self._client() as lk:
            await lk.room.update_room_metadata(
                api.UpdateRoomMetadataRequest(room=room_name, metadata=json.dumps(metadata))
            )

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
        agent_name=settings.livekit_agent_name,
    )

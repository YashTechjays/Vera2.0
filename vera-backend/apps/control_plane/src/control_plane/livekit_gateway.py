"""LiveKit server SDK wrapper: create call rooms, dispatch the agent worker,
place outbound SIP participants, and mint browser join tokens.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import NamedTuple

import aiohttp
from livekit import api
from livekit.api.twirp_client import TwirpError

from vera_core.config import SecretProvider
from vera_core.config.settings import Settings
from vera_core.observability.correlation import SIP_CALLEE_IDENTITY
from vera_core.telephony import LiveKitUnavailable, OutboundDialError, TelephonyError

__all__ = ["LiveKitGateway", "LiveKitUnavailable", "OutboundDialError", "build_livekit_gateway"]

AGENT_NAME = "vera-agent"

# Backstop room lifetimes (the pipeline sweeper is the primary net). A watching
# supervisor counts as a participant, so neither timeout fires for observer-held
# rooms — the sweeper's observer-only probe handles those.
_ROOM_EMPTY_TIMEOUT_S = 300
_ROOM_DEPARTURE_TIMEOUT_S = 120

# Transport-level SDK failures, re-raised as domain errors so SDK exception types
# never leak to the routers. TwirpError is a deprecated alias of the SDK's
# ServerError, so this covers both application-level rejections and transport loss.
_LIVEKIT_TRANSPORT_ERRORS = (TwirpError, aiohttp.ClientError)


def _seam_error[E: TelephonyError](cls: type[E], op: str, exc: Exception) -> E:
    """Build *cls* for a failed LiveKit call, carrying only PHI-safe diagnostics.

    A ServerError's code/status are a fixed server-authored vocabulary; its message
    and metadata can echo the request body (agent_context, the dialed number) and are
    dropped. A transport error never reached LiveKit, so its type name stands in.
    Raise with `from None` — the SDK error's message would otherwise ride the chain.
    """
    if isinstance(exc, TwirpError):
        return cls(op, code=exc.code, status=exc.status)
    return cls(op, code=type(exc).__name__)


# Terminal-failure egress statuses used by get_egress_status. Members are plain
# ints at runtime (protobuf enum wrapper); frozenset gives O(1) membership tests.
# LIMIT_REACHED is deliberately NOT a failure: LiveKit ends the egress at its
# duration/size cap but still uploads the output — the recording exists and must
# enter the retention lifecycle, not be stranded as FAILED.
_FAILED_EGRESS_STATUSES: frozenset[int] = frozenset(
    {
        api.EgressStatus.EGRESS_FAILED,
        api.EgressStatus.EGRESS_ABORTED,
    }
)


class EgressStartError(Exception):
    """Starting the room-composite recording egress failed (LiveKit unreachable or
    the egress service rejected the request). Callers fail OPEN: the call proceeds
    unrecorded and the failure is audited (spec decision 2)."""


class EgressState(NamedTuple):
    """Reconciled egress status for the recording verifier."""

    complete: bool
    failed: bool
    duration_ms: int | None
    size_bytes: int | None


class ActiveEgress(NamedTuple):
    """A currently-running egress, used by the verifier to detect orphans — an
    egress still uploading audio for which no Recording row exists (its row was
    rolled back with the caller transaction)."""

    egress_id: str
    room_name: str
    started_at_ms: int | None


class RoomParticipant(NamedTuple):
    """A participant currently in a room. `can_publish` is the grant LiveKit itself
    enforces, so it — not the mirrored vera.mode attribute — is what says "live mic"."""

    identity: str
    can_publish: bool = False


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
            await lk.aclose()

    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        """Create the room and dispatch the agent worker into it.

        Raises LiveKitUnavailable carrying LiveKit's own rejection code — the
        message is dropped because `metadata` holds agent_context (raw PHI) and a
        rejection can quote the request body back.
        """
        try:
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
        except _LIVEKIT_TRANSPORT_ERRORS as e:
            raise _seam_error(LiveKitUnavailable, "create_call_room failed", e) from None

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
            raise _seam_error(LiveKitUnavailable, "list_outbound_trunk failed", e) from None
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
            raise _seam_error(OutboundDialError, "create_sip_participant failed", e) from None

    async def existing_rooms(self, room_names: list[str]) -> set[str]:
        """The subset of *room_names* that currently exist on the LiveKit server.
        The sweeper uses "room gone but call non-terminal" as the worker-died signal."""
        if not room_names:
            return set()
        async with self._client() as lk:
            resp = await lk.room.list_rooms(api.ListRoomsRequest(names=room_names))
        return {room.name for room in resp.rooms}

    async def room_participants(self, room_name: str) -> list[RoomParticipant] | None:
        """Participants currently in the room, or None when the room doesn't exist.
        The sweeper reads this to spot dead-but-open rooms holding only browser
        observers, which keep the room's departure timeout from ever firing; the
        intervene path reads it to find the window already holding the mic."""
        async with self._client() as lk:
            try:
                resp = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
            except TwirpError as exc:
                if exc.code == "not_found":
                    return None
                raise
        return [RoomParticipant(p.identity, p.permission.can_publish) for p in resp.participants]

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

    async def start_room_audio_egress(
        self, room_name: str, *, bucket: str, object_path: str
    ) -> str:
        """Start an audio-only room-composite egress uploading straight to GCS.

        Empty GCPUpload credentials → the egress service signs with its own
        service account (Workload Identity; devops-todo). Returns the egress id
        the verifier later reconciles with ListEgress.

        SDK note: the installed livekit-api exposes GCPUpload / field ``gcp`` on
        EncodedFileOutput, not GCSUpload / ``gcs`` as named in the spec brief.
        """
        try:
            async with self._client() as lk:
                info = await lk.egress.start_room_composite_egress(
                    api.RoomCompositeEgressRequest(
                        room_name=room_name,
                        audio_only=True,
                        file_outputs=[
                            api.EncodedFileOutput(
                                file_type=api.EncodedFileType.OGG,
                                filepath=object_path,
                                gcp=api.GCPUpload(bucket=bucket),
                            )
                        ],
                    )
                )
        except _LIVEKIT_TRANSPORT_ERRORS as e:
            raise EgressStartError(str(e)) from e
        return str(info.egress_id)

    async def get_egress_status(self, egress_id: str) -> EgressState | None:
        """Current state of one egress; None when LiveKit no longer lists the id
        (server restarted / id expired) — the verifier marks such rows FAILED.
        """
        async with self._client() as lk:
            resp = await lk.egress.list_egress(api.ListEgressRequest(egress_id=egress_id))
        if not resp.items:
            return None
        item = resp.items[0]
        file_result = item.file_results[0] if item.file_results else None
        return EgressState(
            complete=item.status
            in (api.EgressStatus.EGRESS_COMPLETE, api.EgressStatus.EGRESS_LIMIT_REACHED),
            failed=item.status in _FAILED_EGRESS_STATUSES,
            # proto duration / size are 0 when not yet reported; treat 0 as absent.
            duration_ms=(
                (file_result.duration // 1_000_000)
                if file_result and file_result.duration
                else None
            ),
            size_bytes=(file_result.size or None) if file_result else None,
        )

    async def list_active_egresses(self) -> list[ActiveEgress]:
        """Every egress LiveKit is currently running (active=True filter). The
        verifier cross-references these against Recording rows to find orphans —
        egresses still uploading with no row to give them a retention lifecycle.
        """
        async with self._client() as lk:
            resp = await lk.egress.list_egress(api.ListEgressRequest(active=True))
        return [
            ActiveEgress(
                egress_id=str(item.egress_id),
                room_name=item.room_name,
                # proto started_at is unix nanoseconds, 0 when not yet reported.
                started_at_ms=(item.started_at // 1_000_000) if item.started_at else None,
            )
            for item in resp.items
        ]

    async def stop_egress(self, egress_id: str) -> None:
        """Stop a running egress (used to reap orphans). Idempotent: an egress
        that already ended / no longer exists is a no-op, mirroring delete_room.
        """
        async with self._client() as lk:
            try:
                await lk.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
            except TwirpError as exc:
                if exc.code == "not_found":
                    return  # already stopped / gone — nothing to reap
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

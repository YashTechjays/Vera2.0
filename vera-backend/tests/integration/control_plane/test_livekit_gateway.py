"""Tests for LiveKitGateway — token minting is deterministic; room creation
requires a live server so it is not tested here (covered by import + mypy)."""

import jwt
import pytest

from control_plane.livekit_gateway import LiveKitGateway, build_livekit_gateway
from vera_core.config import SecretNotFoundError
from vera_core.config.settings import Settings


class _StubSecrets:
    """Minimal SecretProvider stub for factory tests."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, name: str) -> str:
        try:
            return self._data[name]
        except KeyError:
            raise SecretNotFoundError(name) from None


def test_mint_join_token_grants_room_join() -> None:
    gw = LiveKitGateway(url="ws://localhost:7880", api_key="devkey", api_secret="secret")
    token = gw.mint_join_token(room_name="call--t--c", identity="supervisor-1")

    claims = jwt.decode(token, "secret", algorithms=["HS256"])
    assert claims["sub"] == "supervisor-1"
    assert claims["video"]["room"] == "call--t--c"
    assert claims["video"]["roomJoin"] is True


def test_mint_join_token_carries_name_and_attributes() -> None:
    gw = LiveKitGateway(url="ws://localhost:7880", api_key="devkey", api_secret="secret")
    token = gw.mint_join_token(
        room_name="call--t--c",
        identity="supervisor-1",
        can_publish=False,
        name="supervisor@test.example",
        attributes={"vera.mode": "listener"},
    )

    claims = jwt.decode(token, "secret", algorithms=["HS256"])
    assert claims["name"] == "supervisor@test.example"
    assert claims["attributes"] == {"vera.mode": "listener"}
    assert claims["video"]["canPublish"] is False


def test_mint_join_token_omits_name_and_attributes_by_default() -> None:
    # Existing call sites (Voice Lab) pass neither — their tokens must not change.
    gw = LiveKitGateway(url="ws://localhost:7880", api_key="devkey", api_secret="secret")
    token = gw.mint_join_token(room_name="call--t--c", identity="caller-1")

    claims = jwt.decode(token, "secret", algorithms=["HS256"])
    assert "name" not in claims
    assert "attributes" not in claims


def test_list_participants_returns_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    from livekit import api

    class _FakeRoomService:
        async def list_participants(
            self, req: api.ListParticipantsRequest
        ) -> api.ListParticipantsResponse:
            assert req.room == "call--t--c"
            return api.ListParticipantsResponse(
                participants=[
                    api.ParticipantInfo(identity="supervisor-1"),
                    api.ParticipantInfo(identity="phone-callee"),
                ]
            )

    class _FakeLkApi:
        room = _FakeRoomService()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(api, "LiveKitAPI", lambda *a, **k: _FakeLkApi())
    gw = LiveKitGateway(url="ws://x", api_key="k", api_secret="s")

    import asyncio

    assert asyncio.run(gw.list_participants("call--t--c")) == ["supervisor-1", "phone-callee"]


def test_list_participants_not_found_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vanished room means every participant is gone — [] rather than an error."""
    from livekit import api
    from livekit.api.twirp_client import TwirpError

    class _FakeRoomService:
        async def list_participants(
            self, req: api.ListParticipantsRequest
        ) -> api.ListParticipantsResponse:
            raise TwirpError(code="not_found", msg="room not found", status=404)

    class _FakeLkApi:
        room = _FakeRoomService()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(api, "LiveKitAPI", lambda *a, **k: _FakeLkApi())
    gw = LiveKitGateway(url="ws://x", api_key="k", api_secret="s")

    import asyncio

    assert asyncio.run(gw.list_participants("call--t--c")) == []


def test_build_livekit_gateway_raises_when_url_missing() -> None:
    settings = Settings(livekit_url=None)
    secrets = _StubSecrets({"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s"})
    with pytest.raises(ValueError, match="VERA_LIVEKIT_URL"):
        build_livekit_gateway(settings, secrets)


def test_build_livekit_gateway_constructs_gateway() -> None:
    settings = Settings(livekit_url="ws://localhost:7880")
    secrets = _StubSecrets({"LIVEKIT_API_KEY": "devkey", "LIVEKIT_API_SECRET": "devsecret"})
    gw = build_livekit_gateway(settings, secrets)
    assert gw.url == "ws://localhost:7880"


def test_set_room_metadata_serializes_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_room_metadata JSON-encodes the dict into an UpdateRoomMetadataRequest."""
    import json

    from livekit import api

    from control_plane.livekit_gateway import LiveKitGateway

    captured: dict[str, object] = {}

    class _FakeRoomService:
        async def update_room_metadata(self, req: api.UpdateRoomMetadataRequest) -> None:
            captured["room"] = req.room
            captured["metadata"] = req.metadata

    class _FakeLkApi:
        room = _FakeRoomService()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(api, "LiveKitAPI", lambda *a, **k: _FakeLkApi())
    gw = LiveKitGateway(url="ws://x", api_key="k", api_secret="s")

    import asyncio

    asyncio.run(
        gw.set_room_metadata("call--t--c", {"status": "call_failed", "reason": "no_answer"})
    )
    assert captured["room"] == "call--t--c"
    assert json.loads(str(captured["metadata"])) == {"status": "call_failed", "reason": "no_answer"}


def test_configured_agent_name_flows_to_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """VERA_LIVEKIT_AGENT_NAME threads through build_livekit_gateway → create_dispatch,
    so a laptop sharing a LiveKit project can isolate its dispatch pool from a deployed worker."""
    from livekit import api

    captured: dict[str, object] = {}

    class _FakeRoomService:
        async def create_room(self, req: api.CreateRoomRequest) -> None:
            return None

    class _FakeDispatchService:
        async def create_dispatch(self, req: api.CreateAgentDispatchRequest) -> None:
            captured["agent_name"] = req.agent_name

    class _FakeLkApi:
        room = _FakeRoomService()
        agent_dispatch = _FakeDispatchService()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(api, "LiveKitAPI", lambda *a, **k: _FakeLkApi())
    settings = Settings(livekit_url="ws://x", livekit_agent_name="vera-agent-local")
    secrets = _StubSecrets({"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s"})
    gw = build_livekit_gateway(settings, secrets)

    import asyncio

    asyncio.run(gw.create_call_room("call--t--c"))
    assert captured["agent_name"] == "vera-agent-local"


def test_default_agent_name_stays_vera_agent() -> None:
    """Unset → "vera-agent", so dev/prod (and the deployed worker) are unaffected."""
    settings = Settings(livekit_url="ws://x")
    secrets = _StubSecrets({"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s"})
    assert build_livekit_gateway(settings, secrets)._agent_name == "vera-agent"

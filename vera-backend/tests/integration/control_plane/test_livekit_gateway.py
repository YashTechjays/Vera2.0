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

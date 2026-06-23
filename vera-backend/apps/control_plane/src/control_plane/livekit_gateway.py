"""Thin wrapper over the LiveKit server SDK: create call rooms, dispatch the
agent worker, and mint browser join tokens. Mirrors the build_kms factory shape.
"""

from livekit import api

from vera_core.config import SecretProvider
from vera_core.config.settings import Settings

AGENT_NAME = "vera-agent"


class LiveKitGateway:
    def __init__(self, url: str, api_key: str, api_secret: str) -> None:
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret

    @property
    def url(self) -> str:
        return self._url

    async def create_call_room(self, room_name: str, metadata: str = "") -> None:
        # LiveKitAPI wraps an aiohttp ClientSession, which requires a running
        # event loop — construct it inside the coroutine, not in __init__.
        lk = api.LiveKitAPI(self.url, self._api_key, self._api_secret)
        try:
            await lk.room.create_room(api.CreateRoomRequest(name=room_name))
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=AGENT_NAME, room=room_name, metadata=metadata
                )
            )
        finally:
            await lk.aclose()  # type: ignore[no-untyped-call]  # livekit-api missing return annotation

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

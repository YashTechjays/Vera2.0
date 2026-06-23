"""CORS middleware behaviour (DB/Redis-free: lifespan is not started here)."""

import httpx
import pytest

from control_plane.main import create_app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_preflight_allows_configured_origin(client: httpx.AsyncClient) -> None:
    res = await client.options(
        "/healthz",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert res.headers.get("access-control-allow-credentials") == "true"


async def test_actual_request_gets_cors_header(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz", headers={"Origin": "http://localhost:5173"})
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"


async def test_disallowed_origin_gets_no_cors_header(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in res.headers

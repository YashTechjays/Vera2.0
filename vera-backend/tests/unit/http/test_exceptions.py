"""The error table, the OpenAPI helper, and the global handlers (API Contract §7.1).

These run without a database: a tiny FastAPI app exercises the handlers end to end.
"""

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    ExceptionCode,
    register_exception_handlers,
)
from control_plane.request_context import RequestIdMiddleware


def test_error_codes_map_to_fixed_http_status() -> None:
    assert DefaultExceptionCode.BAD_REQUEST.http_status == 400
    assert DefaultExceptionCode.UNAUTHORIZED.http_status == 401
    assert DefaultExceptionCode.FORBIDDEN.http_status == 403
    assert DefaultExceptionCode.NOT_FOUND.http_status == 404
    assert DefaultExceptionCode.VALIDATION_ERROR.http_status == 422
    assert DefaultExceptionCode.RATE_LIMIT_EXCEEDED.http_status == 429
    assert DefaultExceptionCode.INTERNAL_SERVER_ERROR.http_status == 500
    assert ExceptionCode.USER_ALREADY_EXISTS.http_status == 409
    assert ExceptionCode.INVALID_TOKEN.http_status == 401
    # The machine code is the member name.
    assert ExceptionCode.INVALID_TOKEN.code == "INVALID_TOKEN"


def test_custom_response_groups_codes_by_status() -> None:
    docs = CustomAPIResponse.custom(
        ExceptionCode.INVALID_TOKEN,
        ExceptionCode.TOKEN_EXPIRED,
        DefaultExceptionCode.VALIDATION_ERROR,
    )
    # 401 (both token codes) + 422.
    assert set(docs) == {401, 422}
    assert "INVALID_TOKEN" in docs[401]["description"]
    assert "TOKEN_EXPIRED" in docs[401]["description"]


class _Body(BaseModel):
    n: int


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.post("/boom")
    async def boom() -> None:
        raise CustomAPIException(
            ExceptionCode.USER_ALREADY_EXISTS, description="dup", data={"field": "email"}
        )

    @app.post("/validate")
    async def validate(body: _Body) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("PHI-ish secret detail")

    register_exception_handlers(app)
    return app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_build_app(), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_custom_exception_serializes_to_fail_envelope(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.post("/boom")
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "FAIL"
    assert body["error_code"] == "USER_ALREADY_EXISTS"
    assert body["description"] == "dup"
    assert body["data"] == {"field": "email"}
    assert resp.headers["X-Request-Id"]


async def test_validation_error_envelope_omits_input_values(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.post("/validate", json={"n": "not-an-int-PHI"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "FAIL"
    assert body["error_code"] == "VALIDATION_ERROR"
    # Field paths are reported, but the offending value is never echoed (PHI safety).
    assert body["data"]["fields"][0]["loc"] == ["body", "n"]
    assert "not-an-int-PHI" not in resp.text


async def test_unhandled_exception_is_generic_500(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.get("/crash")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_code"] == "INTERNAL_SERVER_ERROR"
    # The raw exception text never reaches the client.
    assert "secret detail" not in resp.text
    assert resp.headers["X-Request-Id"]


async def test_request_id_generated_when_absent_and_echoed_when_present(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        generated = await client.post("/boom")
        supplied = await client.post("/boom", headers={"X-Request-Id": "rid-123"})
    assert generated.headers["X-Request-Id"]
    assert supplied.headers["X-Request-Id"] == "rid-123"

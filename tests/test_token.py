import json

import httpx
import pytest

from cws_agent_sdk.config import CwsConfig
from cws_agent_sdk.errors import CwsAuthError
from cws_agent_sdk.token import TokenManager


def _cfg():
    return CwsConfig(
        bff_url="https://bff.test",
        ws_url="wss://comm.test",
        api_key="cwsk_secret",
        org_id="org-1",
    )


def _d8(data, status=200):
    return httpx.Response(
        status, json={"data": data, "request_id": "r", "server_time": "t"}
    )


@pytest.mark.asyncio
async def test_exchange_and_cache():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("authorization")))
        assert request.url.path == "/auth/agent/token"
        assert request.headers["authorization"] == "Bearer cwsk_secret"
        assert json.loads(request.content)["org_id"] == "org-1"
        return _d8(
            {
                "access_token": "jwt-1",
                "access_token_expires_at": "2999-01-01T00:00:00Z",
                "refresh_token": "rt-1",
                "refresh_token_expires_at": "2999-01-08T00:00:00Z",
            }
        )

    tm = TokenManager(
        _cfg(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await tm.get_access_token() == "jwt-1"
    assert await tm.get_access_token() == "jwt-1"  # cached, no 2nd call
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_refresh_path_used_after_invalidate():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/token":
            return _d8(
                {
                    "access_token": "jwt-1",
                    "refresh_token": "rt-1",
                }
            )
        if request.url.path == "/auth/refresh":
            body = json.loads(request.content)
            assert body["refresh_token"] == "rt-1"
            return _d8({"access_token": "jwt-2", "refresh_token": "rt-2"})
        raise AssertionError(request.url.path)

    tm = TokenManager(
        _cfg(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await tm.get_access_token() == "jwt-1"
    tm.invalidate()
    assert await tm.get_access_token() == "jwt-2"


@pytest.mark.asyncio
async def test_auth_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "status": 401,
                    "code": "INVALID_API_KEY",
                    "detail": "bad key",
                },
                "request_id": "r",
            },
        )

    tm = TokenManager(
        _cfg(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(CwsAuthError, match="INVALID_API_KEY"):
        await tm.get_access_token()


@pytest.mark.asyncio
async def test_ws_ticket():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/token":
            return _d8({"access_token": "jwt-1", "refresh_token": "rt-1"})
        if request.url.path == "/auth/ws-ticket":
            assert request.headers["authorization"] == "Bearer jwt-1"
            return _d8({"ticket": "tick-123", "expires_at": "soon"})
        raise AssertionError(request.url.path)

    tm = TokenManager(
        _cfg(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await tm.get_ws_ticket() == "tick-123"

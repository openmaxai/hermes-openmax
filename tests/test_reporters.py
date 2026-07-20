"""Reporters, billing gate, and agent.config.* hot updates."""

import httpx
import pytest

from cws_agent_sdk.bridge import CwsBridge
from cws_agent_sdk.codec import FRAME_SYSTEM, Frame
from cws_agent_sdk.config import CwsConfig
from cws_agent_sdk.http import CwsHttpClient
from cws_agent_sdk.providers import FileStorage
from cws_agent_sdk.reporters import (
    BillingGate,
    MetricsReporter,
    OnlineReporter,
)
from cws_agent_sdk.token import TokenManager


def _cfg():
    return CwsConfig(
        bff_url="https://bff.test",
        ws_url="wss://comm.test",
        api_key="cwsk_x",
        org_id="org-1",
        member_id="me-1",
    )


def _http_for(handler):
    cfg = _cfg()
    httpx.MockTransport(handler)

    def h(request):
        if request.url.path == "/auth/agent/token":
            return httpx.Response(
                200, json={"data": {"access_token": "jwt", "refresh_token": "rt"}}
            )
        return handler(request)

    tm = TokenManager(cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    return CwsHttpClient(
        cfg, tm, http=httpx.AsyncClient(transport=httpx.MockTransport(h))
    )


@pytest.mark.asyncio
async def test_online_report():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200, json={"data": {"triggered": False, "reason": "already_onboarded"}}
        )

    rep = OnlineReporter(_http_for(handler))
    data = await rep.report("me-1")
    assert data["reason"] == "already_onboarded"
    assert ("POST", "/api/v1/agents/me-1/online-report") in calls


@pytest.mark.asyncio
async def test_metrics_report_version_only():
    bodies = []

    def handler(request):
        if request.url.path.endswith("/runtime-metrics"):
            import json

            bodies.append((request.method, json.loads(request.content)))
            return httpx.Response(
                200, json={"data": {"request_id": "r", "server_time": "t"}}
            )
        raise AssertionError(request.url.path)

    rep = MetricsReporter(_http_for(handler), lambda: "me-1", version="0.1.0")
    assert await rep.report_once() is True
    method, body = bodies[0]
    assert method == "PUT"
    assert body == {"version": "0.1.0"}


@pytest.mark.asyncio
async def test_bridge_does_not_report_openmax_as_im_channel(tmp_path, monkeypatch):
    requested_paths = []

    async def record_request(method, path, **kwargs):
        requested_paths.append(path)
        return {}

    async def stop_after_tick(_delay):
        bridge._running = False

    async def on_message(_message):
        pass

    bridge = CwsBridge(
        _cfg(),
        storage=FileStorage(tmp_path),
        on_message=on_message,
        billing_gate_enabled=False,
    )
    monkeypatch.setattr(bridge._http, "request", record_request)
    monkeypatch.setattr("cws_agent_sdk.bridge.asyncio.sleep", stop_after_tick)

    bridge._running = True
    await bridge._metrics_loop()
    await bridge.stop()

    assert not any(path.endswith("/channel-liveness") for path in requested_paths)


@pytest.mark.asyncio
async def test_billing_gate_suspended_and_cache():
    hits = []

    def handler(request):
        hits.append(request.url.path)
        return httpx.Response(
            200, json={"data": {"usage_snapshot": {"enforcement_suspended": True}}}
        )

    gate = BillingGate(_http_for(handler), cache_ttl_s=60)
    assert await gate.is_suspended() is True
    assert await gate.is_suspended() is True  # cached
    assert len(hits) == 1
    assert gate.should_send_overdue_notice("c-1") is True
    assert gate.should_send_overdue_notice("c-1") is False  # throttled


@pytest.mark.asyncio
async def test_billing_gate_fails_open():
    def handler(request):
        return httpx.Response(500, json={"error": {"status": 500, "detail": "boom"}})

    gate = BillingGate(_http_for(handler))
    assert await gate.is_suspended() is False


@pytest.mark.asyncio
async def test_config_hot_update_frames(tmp_path):
    async def on_message(m):
        pass

    events = []

    async def on_cfg(event, data):
        events.append(event)

    b = CwsBridge(
        _cfg(),
        storage=FileStorage(tmp_path),
        on_message=on_message,
        billing_gate_enabled=False,
        on_config_event=on_cfg,
    )

    def sysframe(event, data):
        return Frame(type=FRAME_SYSTEM, payload={"event": event, "data": data})

    await b._handle_frame(
        sysframe(
            "agent.config.dm_allowlist_changed",
            {"action": "add", "member_ids": ["u-1", "u-2"]},
        )
    )
    assert b._policy.dm_allowlist == ["u-1", "u-2"]
    await b._handle_frame(
        sysframe(
            "agent.config.dm_allowlist_changed",
            {"action": "remove", "member_ids": ["u-1"]},
        )
    )
    assert b._policy.dm_allowlist == ["u-2"]

    await b._handle_frame(
        sysframe(
            "agent.config.group_mode_changed",
            {"conversation_id": "c-9", "mode": "open"},
        )
    )
    assert b._effective_policy("c-9").group_require_mention is False
    assert b._effective_policy("c-other").group_require_mention is True

    await b._handle_frame(
        sysframe("agent.config.owner_changed", {"new_owner_member_id": "owner-2"})
    )
    assert b.owner_member_id == "owner-2"
    assert len(events) == 4


@pytest.mark.asyncio
async def test_reported_policy_matches_runtime_state(tmp_path):
    async def on_message(_):
        pass

    b = CwsBridge(
        _cfg(),
        storage=FileStorage(tmp_path),
        on_message=on_message,
        billing_gate_enabled=False,
    )
    calls = []

    async def request(method, path, *, json=None, **_):
        calls.append((method, path, json))
        return {}

    b._http.request = request
    b._policy.dm_policy = "allowlist"
    b._policy.dm_allowlist = ["u-1"]
    b._policy.group_policy = "allowlist"
    b._policy.group_configs = {
        "c-1": {"mode": "smart", "allow_from": ["u-1"]},
    }

    await b._report_policy()

    assert calls == [
        (
            "PUT",
            "/api/v1/agents/me-1/reported-policy",
            {
                "dm_policy": "allowlist",
                "dm_allowlist": ["u-1"],
                "group_scope": "allowlist",
                "group_allowlist": ["c-1"],
                "groups": [
                    {"conversation_id": "c-1", "mode": "smart", "allow_from": ["u-1"]}
                ],
            },
        )
    ]

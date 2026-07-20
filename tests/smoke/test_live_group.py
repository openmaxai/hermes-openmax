"""Live OpenMax smoke tests.

These tests are opt-in and require a running Hermes gateway connected to the
same OpenMax agent. They send a uniquely tagged human message through cws-core
and verify the agent replies in the same conversation.
"""

import os
import time
import uuid

import httpx
import pytest


pytestmark = pytest.mark.live


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for live smoke tests")
    return value


def _headers() -> dict[str, str]:
    token = _required("OPENMAX_SMOKE_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    cf_id = os.getenv("CF_ACCESS_CLIENT_ID", "").strip()
    cf_secret = os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret
    return headers


def _unwrap_list(response: httpx.Response) -> list[dict]:
    response.raise_for_status()
    body = response.json()
    if isinstance(body, list):
        return body
    data = body.get("data", body) if isinstance(body, dict) else []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "messages", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def test_live_group_mention_round_trip():
    base_url = _required("CWS_BFF_URL").rstrip("/")
    conversation_id = _required("OPENMAX_SMOKE_GROUP_ID")
    agent_member_id = _required("OPENMAX_SMOKE_AGENT_MEMBER_ID")
    timeout_s = float(os.getenv("OPENMAX_SMOKE_TIMEOUT_S", "120"))
    trace = f"hermes-smoke-{uuid.uuid4().hex[:12]}"
    prompt = f"@hermes_1 Reply with exactly: {trace}"

    with httpx.Client(base_url=base_url, headers=_headers(), timeout=30) as client:
        sent = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "client_msg_id": f"smoke-{uuid.uuid4()}",
                "type": "TEXT",
                "content": {
                    "content_type": "text",
                    "body": {"text": prompt},
                    "attachments": [],
                },
                "mentions": [
                    {"type": "member", "member_id": agent_member_id}
                ],
            },
        )
        sent.raise_for_status()

        deadline = time.monotonic() + timeout_s
        observed = []
        while time.monotonic() < deadline:
            response = client.get(
                f"/api/v1/conversations/{conversation_id}/messages",
                params={"limit": 50},
            )
            observed = _unwrap_list(response)
            for message in observed:
                text = str(message.get("content") or message.get("text") or "")
                if trace in text and str(message.get("sender_id") or "") == agent_member_id:
                    return
            time.sleep(2)

    pytest.fail(
        f"No agent reply containing {trace!r} in conversation {conversation_id}; "
        f"observed {len(observed)} messages"
    )

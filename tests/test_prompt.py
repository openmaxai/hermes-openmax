"""Tests for the copyable Buy Agent onboarding prompt."""

from hermes_openmax.prompt import build_prompt


def test_prompt_drives_complete_profile_safe_onboarding():
    prompt = build_prompt(
        bff_url="https://bff.example",
        ws_url="wss://comm.example/ws",
        org_id="org-1",
        invitation_id="invite-1",
        invitation_token="invite-secret-for-target-agent",
        organization_name="Example Org",
        display_name="Hermes One",
        owner_name="Leslie",
        owner_member_id="owner-1",
        expires_at="2026-08-01T00:00:00Z",
    )

    for expected in (
        "hermes plugins install openmaxai/hermes-openmax --enable",
        "hermes plugins list --plain --no-bundled",
        "hermes plugins update hermes-openmax",
        "hermes config env-path",
        "register_agent",
        "accept_invitation",
        "data.access_token",
        "SDK 返回的 `result` 已经是解包后的 D8 `data`",
        "hermes gateway install --start-now",
        "hermes gateway restart",
        "cws connected",
        "online-report",
        "不得调用 `/channel-liveness`",
        "inbound message: platform=cws",
        "response ready: platform=cws",
        "/reset",
        "[SKIP]",
        "Message E2E: passed/failed/not-tested",
    ):
        assert expected in prompt

    assert "https://bff.example" in prompt
    assert "wss://comm.example/ws" in prompt
    assert "org-1" in prompt
    assert "invite-1" in prompt
    assert "invite-secret-for-target-agent" in prompt
    assert "Example Org" in prompt
    assert "Hermes One" in prompt
    assert "Leslie" in prompt
    assert "owner-1" in prompt
    assert "2026-08-01T00:00:00Z" in prompt
    assert "zylos-openmax" in prompt


def test_prompt_uses_safe_placeholders_without_invitation_values():
    prompt = build_prompt()
    for placeholder in (
        "<CWS_BFF_URL>",
        "<CWS_WS_URL>",
        "<ORG_ID>",
        "<INVITATION_ID>",
        "<INVITATION_TOKEN>",
        "<ORGANIZATION_NAME>",
        "<DISPLAY_NAME>",
        "<OWNER_NAME>",
        "<OWNER_MEMBER_ID>",
        "<EXPIRES_AT>",
    ):
        assert placeholder in prompt

    assert "Bearer ***" not in prompt
    assert "<GENER...KEY>" not in prompt
    assert "storage.googleapis.com" not in prompt


def test_prompt_does_not_treat_openmax_transport_as_im_channel():
    prompt = build_prompt()
    assert "CWS WebSocket 是传输连接" in prompt
    assert "不得调用 `/channel-liveness`" in prompt
    assert "channel_type: openmax" in prompt
    assert "online_status=online" in prompt

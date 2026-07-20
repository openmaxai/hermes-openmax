"""Tests for the copyable onboarding prompt."""

from hermes_openmax.prompt import build_prompt


def test_prompt_includes_setup_and_policy_without_secrets():
    prompt = build_prompt(
        bff_url="https://bff.example",
        ws_url="wss://comm.example/ws",
        org_id="org-1",
        member_id="member-1",
    )
    assert "https://bff.example" in prompt
    assert "wss://comm.example/ws" in prompt
    assert "org-1" in prompt
    assert "member-1" in prompt
    assert "CWS_API_KEY=<your OpenMax agent API key>" in prompt
    assert "group conversations use separate Hermes sessions" in prompt
    assert "group scope" in prompt
    assert "[SKIP]" in prompt
    assert "secret-value" not in prompt


def test_prompt_uses_placeholders_when_values_are_absent():
    prompt = build_prompt()
    assert "<CWS_BFF_URL>" in prompt
    assert "<CWS_WS_URL>" in prompt
    assert "<CWS_ORG_ID>" in prompt
    assert "<CWS_MEMBER_ID>" in prompt

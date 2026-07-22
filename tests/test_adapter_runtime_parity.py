"""Adapter runtime parity for system conversations and inbound metadata."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform

from cws_agent_sdk.types import InboundMessage
from hermes_openmax.adapter import CwsAdapter, _policy_from_env


def test_policy_env_defaults_are_safe_and_agent_controls_are_explicit(monkeypatch):
    for name in (
        "CWS_DM_POLICY",
        "CWS_GROUP_POLICY",
        "CWS_ALLOW_ALL_USERS",
        "CWS_ALLOWED_USERS",
        "CWS_ALLOW_AGENT_SENDERS",
        "CWS_ALLOW_SIBLING_DM",
        "CWS_ALLOWED_AGENT_SENDERS",
        "CWS_SELF_ALIASES",
    ):
        monkeypatch.delenv(name, raising=False)

    policy = _policy_from_env()

    assert policy.dm_policy == "owner"
    assert policy.group_policy == "allowlist"
    assert policy.allow_agent_senders is False
    assert policy.allow_sibling_dm is False
    assert policy.agent_allowlist == []


def test_policy_env_loads_agent_allowlist_aliases_and_budgets(monkeypatch):
    monkeypatch.setenv("CWS_ALLOWED_AGENT_SENDERS", "agent-1, agent-2")
    monkeypatch.setenv("CWS_SELF_ALIASES", "COCO, helper.bot")
    monkeypatch.setenv("CWS_MAX_AGENT_HOPS", "3")
    monkeypatch.setenv("CWS_AGENT_TURN_BUDGET", "2")

    policy = _policy_from_env()

    assert policy.agent_allowlist == ["agent-1", "agent-2"]
    assert policy.self_aliases == ["COCO", "helper.bot"]
    assert policy.max_agent_hops == 3
    assert policy.agent_turn_budget == 2


class _Bridge:
    def __init__(self):
        self._cfg = SimpleNamespace(member_id="agent-1")
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id="out-1")


def _adapter():
    adapter = object.__new__(CwsAdapter)
    adapter._bridge = _Bridge()
    adapter._orientation = ""
    adapter.platform = next(iter(Platform))
    adapter.events = []

    async def handle_message(event):
        adapter.events.append(event)

    adapter.handle_message = handle_message
    return adapter


@pytest.mark.asyncio
async def test_system_member_inbound_marks_only_that_message_read_only_for_replies():
    adapter = _adapter()
    message = InboundMessage(
        message_id="m-1",
        conversation_id="system-dm-1",
        org_id="org-1",
        text="Run task",
        sender_id="system-member",
        sender_name="System Member",
        sender_type="system",
        conversation_type="dm",
    )

    await adapter._on_inbound(message)
    result = await adapter.send("system-dm-1", "I did it", reply_to="m-1")

    assert result.success
    assert result.message_id == ""
    assert adapter._bridge.sent == []

    # A later human message in the same conversation remains writable.
    human = InboundMessage(
        message_id="m-2",
        conversation_id="system-dm-1",
        org_id="org-1",
        text="Hello",
        sender_id="human-1",
        sender_type="human",
        conversation_type="dm",
    )
    await adapter._on_inbound(human)
    result = await adapter.send("system-dm-1", "Hello back", reply_to="m-2")
    assert result.success and result.message_id == "out-1"


@pytest.mark.asyncio
async def test_agent_inbound_causation_is_forwarded_to_outbound_reply():
    adapter = _adapter()
    first = InboundMessage(
        message_id="agent-msg-1",
        conversation_id="agent-dm-1",
        org_id="org-1",
        text="Please continue",
        sender_id="agent-2",
        sender_type="agent",
        conversation_type="dm",
        metadata={
            "agent_hop_count": 2,
            "agent_origin_member_id": "agent-1",
            "agent_trace_id": "trace-1",
        },
    )
    second = InboundMessage(
        message_id="agent-msg-2",
        conversation_id="agent-dm-1",
        org_id="org-1",
        text="Another request",
        sender_id="agent-2",
        sender_type="agent",
        conversation_type="dm",
        metadata={"agent_hop_count": 7, "agent_trace_id": "trace-2"},
    )
    human = InboundMessage(
        message_id="human-msg-1",
        conversation_id="agent-dm-1",
        org_id="org-1",
        text="Human interjection",
        sender_id="human-1",
        sender_type="human",
        conversation_type="dm",
    )

    await adapter._on_inbound(first)
    await adapter._on_inbound(second)
    await adapter._on_inbound(human)
    await adapter.send("agent-dm-1", "Done", reply_to="agent-msg-1")
    assert adapter._bridge.sent[-1]["metadata"] == {
        "agent_hop_count": 2,
        "agent_origin_member_id": "agent-1",
        "agent_trace_id": "trace-1",
    }

    await adapter.send("agent-dm-1", "Second", reply_to="agent-msg-2")
    assert adapter._bridge.sent[-1]["metadata"] == {
        "agent_hop_count": 7,
        "agent_trace_id": "trace-2",
    }

    await adapter.send("agent-dm-1", "Proactive")
    assert adapter._bridge.sent[-1]["metadata"] == {}


@pytest.mark.asyncio
async def test_system_member_conversation_suppresses_media_and_edits():
    adapter = _adapter()
    adapter._readonly_message_ids = {"m-1"}

    image = await adapter.send_image_file(
        "system-dm-1", "/tmp/not-read.png", reply_to="m-1"
    )
    edit = await adapter.edit_message("system-dm-1", "m-1", "progress")

    assert image.success and image.message_id == ""
    assert edit.success and edit.message_id == ""
    assert adapter._bridge.sent == []


@pytest.mark.asyncio
async def test_non_system_conversation_remains_writable():
    adapter = _adapter()
    message = InboundMessage(
        message_id="m-1",
        conversation_id="human-dm-1",
        org_id="org-1",
        text="Hello",
        sender_id="human-1",
        sender_type="human",
        conversation_type="dm",
    )

    await adapter._on_inbound(message)
    result = await adapter.send("human-dm-1", "Hello back", reply_to="m-1")

    assert result.success
    assert result.message_id == "out-1"
    assert len(adapter._bridge.sent) == 1


def test_cws_adapter_delegates_authorization_to_openmax():
    adapter = _adapter()
    assert adapter.authorization_is_upstream is True


@pytest.mark.asyncio
async def test_authoritative_owner_change_rebuilds_orientation():
    adapter = _adapter()
    rebuilds = []

    async def rebuild_orientation():
        rebuilds.append(adapter._bridge._cfg.member_id)

    adapter._build_orientation = rebuild_orientation

    await adapter._on_config_event(
        "agent.config.owner_changed",
        {"new_owner_member_id": "owner-from-core", "source": "core"},
    )

    assert rebuilds == ["agent-1"]


@pytest.mark.asyncio
async def test_group_source_is_role_authorized_without_member_user_id():
    adapter = _adapter()
    message = InboundMessage(
        message_id="group-m-1",
        conversation_id="group-1",
        org_id="org-1",
        text="hello",
        sender_id="human-1",
        sender_type="human",
        conversation_type="group",
    )

    await adapter._on_inbound(message)

    assert adapter.events[-1].source.chat_type == "group"
    assert adapter.events[-1].source.user_id is None
    assert adapter.events[-1].source.role_authorized is True


@pytest.mark.asyncio
async def test_readonly_system_message_ids_are_bounded():
    adapter = _adapter()
    for index in range(1100):
        await adapter._on_inbound(
            InboundMessage(
                message_id=f"m-{index}",
                conversation_id="system-dm-1",
                org_id="org-1",
                text="Run task",
                sender_id="system-member",
                sender_type="system",
                conversation_type="dm",
            )
        )

    assert len(adapter._readonly_message_ids) == 1024
    assert "m-0" not in adapter._readonly_message_ids
    assert "m-1099" in adapter._readonly_message_ids


@pytest.mark.asyncio
async def test_cws_priority_stays_in_metadata_when_message_event_has_no_priority_field():
    adapter = _adapter()
    message = InboundMessage(
        message_id="m-1",
        conversation_id="human-dm-1",
        org_id="org-1",
        text="Urgent",
        sender_id="human-1",
        sender_type="human",
        conversation_type="dm",
        metadata={"cws_priority": "high"},
    )

    await adapter._on_inbound(message)

    event = adapter.events[0]
    assert not hasattr(event, "priority")
    assert event.metadata["cws_priority"] == "high"


def test_source_uses_conversation_scoped_chat_type_for_session_routing():
    adapter = _adapter()
    source = adapter.build_source(
        chat_id="conv-group-1",
        chat_type="group",
        user_id="user-1",
    )

    assert source.chat_id == "conv-group-1"
    assert source.chat_type == "group"


@pytest.mark.asyncio
async def test_group_members_share_one_conversation_scoped_source():
    adapter = _adapter()
    seen = []

    async def capture(event):
        seen.append(event)

    adapter.handle_message = capture
    for sender in ("user-1", "user-2"):
        await adapter._on_inbound(
            InboundMessage(
                message_id=f"msg-{sender}",
                conversation_id="group-1",
                conversation_type="group",
                org_id="org-1",
                sender_id=sender,
                sender_name=sender,
                text="hello",
            )
        )
    assert [
        (event.source.chat_id, event.source.chat_type, event.source.user_id)
        for event in seen
    ] == [
        ("group-1", "group", None),
        ("group-1", "group", None),
    ]

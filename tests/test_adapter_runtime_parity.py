"""Adapter runtime parity for system conversations and inbound metadata."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform

from cws_agent_sdk.types import InboundMessage
from hermes_openmax.adapter import CwsAdapter


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

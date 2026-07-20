"""Credential-free integration tests for the OpenMax group delivery pipeline."""

from types import SimpleNamespace

import pytest

from cws_agent_sdk.access_policy import AccessPolicyConfig
from cws_agent_sdk.bridge import CwsBridge
from cws_agent_sdk.codec import FRAME_MESSAGE, Frame
from cws_agent_sdk.config import CwsConfig
from cws_agent_sdk.providers import FileStorage
from cws_agent_sdk.types import InboundMessage
from hermes_openmax.adapter import CwsAdapter


class IntegrationComm:
    def __init__(self, *, conversation_id: str, sender_id: str, member_id: str):
        self.conversation_id = conversation_id
        self.sender_id = sender_id
        self.member_id = member_id
        self.sync_acks = []
        self.read_marks = []
        self.messages = {}

    async def get_message(self, conversation_id, message_id):
        return self.messages[(conversation_id, str(message_id))]

    async def get_conversation(self, conversation_id):
        return {"id": conversation_id, "type": "group", "name": "Integration Group"}

    async def mark_read(self, conversation_id, seq):
        self.read_marks.append((conversation_id, seq))

    async def sync_ack(self, _device_id, seq, *_args):
        self.sync_acks.append(seq)

    async def add_reaction(self, *_args):
        return None

    async def remove_reaction(self, *_args):
        return None


class IntegrationCore:
    def __init__(self, sender_id):
        self.sender_id = sender_id

    async def get_member(self, member_id):
        assert member_id == self.sender_id
        return {"id": member_id, "display_name": "Test User"}


class NoopArtifacts:
    async def resolve_uris(self, _uris):
        return {"resolved": {}}


@pytest.mark.asyncio
async def test_ws_group_frame_reaches_conversation_scoped_gateway_source(tmp_path):
    conversation_id = "group-e2e-1"
    sender_id = "user-1"
    member_id = "agent-1"
    captured = []

    adapter = object.__new__(CwsAdapter)
    adapter.platform = "cws"
    adapter._bridge = SimpleNamespace(_cfg=SimpleNamespace(member_id=member_id))
    adapter._orientation = ""
    adapter._readonly_message_ids = set()
    adapter._silent_groups = set()

    async def capture_event(event):
        captured.append(event)

    adapter.handle_message = capture_event

    cfg = CwsConfig(
        bff_url="https://bff.test",
        ws_url="wss://comm.test",
        api_key="cwsk_test",
        org_id="org-1",
        member_id=member_id,
    )
    bridge = CwsBridge(
        cfg,
        storage=FileStorage(tmp_path),
        on_message=adapter._on_inbound,
        policy=AccessPolicyConfig(group_policy="open"),
        billing_gate_enabled=False,
        ack_reaction="",
    )
    comm = IntegrationComm(
        conversation_id=conversation_id, sender_id=sender_id, member_id=member_id
    )
    comm.messages[(conversation_id, "1001")] = {
        "message": {
            "id": "1001",
            "conversation_id": conversation_id,
            "seq": 7,
            "inbox_seq": 501,
            "sender_id": sender_id,
            "sender_type": "HUMAN",
            "mentions": [{"type": "member", "member_id": member_id}],
        },
        "content": {"content_type": "text", "body": {"text": "@agent hello"}},
    }
    bridge.comm = comm
    bridge.core = IntegrationCore(sender_id)
    bridge.artifacts = NoopArtifacts()

    await bridge._handle_frame(
        Frame(
            type=FRAME_MESSAGE,
            org_id="org-1",
            payload={
                "id": "1001",
                "conversation_id": conversation_id,
                "seq": 501,
                "sender_id": sender_id,
            },
        )
    )

    assert len(captured) == 1
    event = captured[0]
    assert event.source.chat_id == conversation_id
    assert event.source.chat_type == "group"
    assert event.source.user_id is None
    assert event.text == "@agent hello"
    assert comm.read_marks == [(conversation_id, 7)]
    assert comm.sync_acks == [501]


@pytest.mark.asyncio
async def test_sync_group_frame_preserves_inbox_seq_separately(tmp_path):
    conversation_id = "group-sync-1"
    sender_id = "user-1"
    member_id = "agent-1"
    captured = []
    adapter = object.__new__(CwsAdapter)
    adapter.platform = "cws"
    adapter._bridge = SimpleNamespace(_cfg=SimpleNamespace(member_id=member_id))
    adapter._orientation = ""
    adapter._readonly_message_ids = set()
    adapter._silent_groups = set()
    async def handle_event(event):
        captured.append(event)

    adapter.handle_message = handle_event
    cfg = CwsConfig(bff_url="https://bff.test", ws_url="wss://comm.test", api_key="x", org_id="org-1", member_id=member_id)
    bridge = CwsBridge(cfg, storage=FileStorage(tmp_path), on_message=adapter._on_inbound, policy=AccessPolicyConfig(group_policy="open"), billing_gate_enabled=False, ack_reaction="")
    comm = IntegrationComm(conversation_id=conversation_id, sender_id=sender_id, member_id=member_id)
    comm.messages[(conversation_id, "1002")] = {"message": {"id": "1002", "conversation_id": conversation_id, "seq": 8, "sender_id": sender_id, "sender_type": "HUMAN", "mentions": [{"member_id": member_id}]}, "content": {"body": {"text": "@agent sync"}}}
    bridge.comm = comm
    bridge.core = IntegrationCore(sender_id)
    bridge.artifacts = NoopArtifacts()
    await bridge._handle_frame(Frame(type="sync", payload={"events": [{"conversation_id": conversation_id, "message_id": "1002", "seq": 777}]}))
    assert captured[0].metadata["cws_inbox_seq"] == 777
    assert comm.read_marks == [(conversation_id, 8)]
    assert comm.sync_acks == [777]

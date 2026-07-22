"""Bridge behavior: normalization, echo suppression, delivery invariant."""

import pytest

from cws_agent_sdk.bridge import CwsBridge
from cws_agent_sdk.codec import FRAME_MESSAGE, Frame
from cws_agent_sdk.config import CwsConfig
from cws_agent_sdk.providers import FileStorage


class FakeComm:
    def __init__(self):
        self.messages = {}
        self.read_marks = []
        self.sync_acks = []
        self.sent_messages = []

    async def get_message(self, conv_id, msg_id):
        return self.messages[f"{conv_id}:{msg_id}"]

    async def mark_read(self, conv_id, seq):
        self.read_marks.append((conv_id, seq))
        return seq

    async def sync_ack(self, device_id, seq):
        self.sync_acks.append(seq)

    async def sync(self, since_seq, device_id, limit=100):
        return {"events": [], "has_more": False}

    async def get_conversation(self, conv_id):
        return {"id": conv_id, "type": getattr(self, "conv_type", "dm")}

    async def add_reaction(self, message_id, code):
        self.reactions_added = getattr(self, "reactions_added", [])
        self.reactions_added.append((str(message_id), code))

    async def remove_reaction(self, message_id, code):
        self.reactions_removed = getattr(self, "reactions_removed", [])
        self.reactions_removed.append((str(message_id), code))

    async def send_message(self, conv_id, text, **kw):
        from cws_agent_sdk.types import SendReceipt

        self.sent_messages.append((conv_id, text, kw))
        return SendReceipt(message_id="out-1", conversation_id=conv_id)


def make_bridge(tmp_path, on_message, member_id="me-1"):
    cfg = CwsConfig(
        bff_url="https://bff.test",
        ws_url="wss://comm.test",
        api_key="cwsk_x",
        org_id="org-1",
        member_id=member_id,
    )
    bridge = CwsBridge(
        cfg,
        storage=FileStorage(tmp_path),
        on_message=on_message,
        billing_gate_enabled=False,
    )
    bridge.comm = FakeComm()
    return bridge


def msg_frame(msg_id=1, conv="conv-1", seq=10, sender="user-7"):
    return Frame(
        type=FRAME_MESSAGE,
        org_id="org-1",
        payload={
            "id": msg_id,
            "conversation_id": conv,
            "seq": seq,
            "sender_id": sender,
        },
    )


def detail(
    msg_id=1,
    conv="conv-1",
    seq=10,
    sender="user-7",
    text="hello",
    sender_type="HUMAN",
    mentions=None,
    metadata=None,
    include_inbox_seq=True,
):
    message = {
        "id": msg_id,
        "conversation_id": conv,
        "seq": seq,
        "sender_id": sender,
        "sender_type": sender_type,
        "client_msg_id": f"cm-{msg_id}",
        "mentions": mentions or [],
        "metadata": metadata or {},
    }
    if include_inbox_seq:
        message["inbox_seq"] = seq
    return {
        "message": message,
        "content": {"content_type": "text", "body": {"text": text}},
    }


def sync_detail(
    msg_id=1,
    conv="conv-1",
    *,
    conversation_seq=1,
    inbox_seq=10,
    sender="user-7",
    text="hello",
):
    value = detail(
        msg_id=msg_id,
        conv=conv,
        seq=conversation_seq,
        sender=sender,
        text=text,
    )
    value["message"]["inbox_seq"] = inbox_seq
    return value


@pytest.mark.asyncio
async def test_inbound_delivery_and_watermark(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    b = make_bridge(tmp_path, on_message)
    b.comm.messages["conv-1:1"] = detail()
    await b._handle_frame(msg_frame())

    assert len(got) == 1
    assert got[0].text == "hello"
    assert got[0].sender_type == "human"
    assert b.comm.read_marks == [("conv-1", 10)]
    assert b.comm.sync_acks == [10]


@pytest.mark.asyncio
async def test_realtime_without_inbox_seq_does_not_advance_global_cursor(tmp_path):
    got = []

    async def on_message(message):
        got.append(message)

    b = make_bridge(tmp_path, on_message)
    b.comm.messages["conv-1:1"] = detail(seq=7, include_inbox_seq=False)

    await b._handle_frame(msg_frame(seq=7))

    assert len(got) == 1
    assert b.comm.read_marks == [("conv-1", 7)]
    assert b.comm.sync_acks == []
    assert b._sync_seq == 0


@pytest.mark.asyncio
async def test_sync_replay_acks_org_inbox_seq_not_conversation_seq(tmp_path):
    """The global /sync cursor must never be advanced with a per-chat seq."""
    got = []

    async def on_message(message):
        got.append(message)

    b = make_bridge(tmp_path, on_message)
    b.comm.messages["group-1:1"] = sync_detail(
        conv="group-1", conversation_seq=1, inbox_seq=201
    )

    await b._deliver_by_id("group-1", 1, 201)

    assert len(got) == 1
    assert b.comm.read_marks == [("group-1", 1)]
    assert b.comm.sync_acks == [201]
    assert b._sync_seq == 201


@pytest.mark.asyncio
async def test_delivery_failure_keeps_watermark(tmp_path):
    async def on_message(m):
        raise RuntimeError("gateway rejected")

    b = make_bridge(tmp_path, on_message)
    b.comm.messages["conv-1:1"] = detail()
    with pytest.raises(RuntimeError):
        await b._handle_frame(msg_frame())

    # Invariant: no read-mark, no sync-ack, not marked seen — /sync will replay.
    assert b.comm.read_marks == []
    assert b.comm.sync_acks == []
    assert "conv-1:1" not in b._seen


@pytest.mark.asyncio
async def test_own_echo_suppressed_by_sender(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    b = make_bridge(tmp_path, on_message, member_id="me-1")
    await b._handle_frame(msg_frame(sender="me-1"))
    assert got == []


@pytest.mark.asyncio
async def test_dedupe(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    b = make_bridge(tmp_path, on_message)
    b.comm.messages["conv-1:1"] = detail()
    await b._handle_frame(msg_frame())
    await b._handle_frame(msg_frame())
    assert len(got) == 1


@pytest.mark.asyncio
async def test_fallback_text_extraction(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    b = make_bridge(tmp_path, on_message)
    d = detail()
    d["content"] = {}
    d["message"]["fallback_text"] = "fallback!"
    b.comm.messages["conv-1:1"] = d
    await b._handle_frame(msg_frame())
    assert got[0].text == "fallback!"


@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_suppressed(tmp_path):
    """WS frame and /sync replay racing on the same message deliver once."""
    import asyncio

    got = []

    async def slow_on_message(m):
        await asyncio.sleep(0.05)  # widen the race window
        got.append(m)

    b = make_bridge(tmp_path, slow_on_message)
    b.comm.messages["conv-1:1"] = detail()
    await asyncio.gather(
        b._handle_frame(msg_frame()),
        b._handle_frame(msg_frame()),
    )
    assert len(got) == 1
    assert b.comm.sync_acks == [10]


@pytest.mark.asyncio
async def test_ack_reaction_added_and_cleared_on_reply(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    b = make_bridge(tmp_path, on_message)
    b.comm.messages["conv-1:1"] = detail()
    await b._handle_frame(msg_frame())
    assert b.comm.reactions_added == [("1", "eyes")]
    assert b._pending_acks["conv-1"] == "1"

    await b.send("conv-1", "reply!")
    assert b.comm.reactions_removed == [("1", "eyes")]
    assert "conv-1" not in b._pending_acks


@pytest.mark.asyncio
async def test_group_smart_mode_and_history_context(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    b = make_bridge(tmp_path, on_message)
    b.comm.conv_type = "group"
    b._group_mode_overrides["conv-1"] = "smart"
    b._policy.group_configs["conv-1"] = {
        "mode": "smart",
        "allow_from": ["*"],
    }
    b.comm.messages["conv-1:1"] = detail(msg_id=1, seq=10, text="earlier chatter")
    b.comm.messages["conv-1:2"] = detail(msg_id=2, seq=11, text="hello smart")
    await b._handle_frame(msg_frame(msg_id=1, seq=10))
    await b._handle_frame(msg_frame(msg_id=2, seq=11))
    # smart mode: no mention required, both delivered, hint attached
    assert len(got) == 2
    assert "smart-mode" in got[1].metadata.get("smart_mode_hint", "")
    assert "earlier chatter" in got[1].metadata.get("group_context", "")


@pytest.mark.asyncio
async def test_group_silent_mode_caches_without_model_delivery(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    b = make_bridge(tmp_path, on_message)
    b.comm.conv_type = "group"
    b._group_mode_overrides["conv-1"] = "silent"
    b._policy.group_configs["conv-1"] = {
        "mode": "silent",
        "allow_from": ["*"],
    }

    async def unexpected_member_lookup(_member_id):
        raise AssertionError("silent must not resolve sender identity")

    b.core.get_member = unexpected_member_lookup
    b.comm.messages["conv-1:1"] = detail()
    await b._handle_frame(msg_frame())
    assert got == []
    assert b._group_history["conv-1"] == ["user-7: hello"]
    assert getattr(b.comm, "reactions_added", []) == []
    assert b.comm.sync_acks == [10]  # consumed silently


@pytest.mark.asyncio
async def test_agent_duplicate_and_turn_budget_breakers_consume_without_delivery(tmp_path):
    got = []

    async def on_message(message):
        got.append(message)

    b = make_bridge(tmp_path, on_message)
    b.comm.conv_type = "group"
    b._policy.group_policy = "open"
    b._policy.allow_agent_senders = True
    b._policy.agent_allowlist = ["agent-1"]
    b._policy.agent_turn_budget = 2
    mention = [{"type": "member", "member_id": "me-1"}]
    b.comm.messages["conv-1:1"] = detail(
        msg_id=1,
        sender="agent-1",
        sender_type="AGENT",
        mentions=mention,
        text="same task",
    )
    b.comm.messages["conv-1:2"] = detail(
        msg_id=2,
        seq=11,
        sender="agent-1",
        sender_type="AGENT",
        mentions=mention,
        text="same task",
    )
    b.comm.messages["conv-1:3"] = detail(
        msg_id=3,
        seq=12,
        sender="agent-1",
        sender_type="AGENT",
        mentions=mention,
        text="different task",
    )
    b.comm.messages["conv-1:4"] = detail(
        msg_id=4,
        seq=13,
        sender="agent-1",
        sender_type="AGENT",
        mentions=mention,
        text="third task",
    )

    await b._handle_frame(msg_frame(msg_id=1, sender="agent-1"))
    await b._handle_frame(msg_frame(msg_id=2, seq=11, sender="agent-1"))
    await b._handle_frame(msg_frame(msg_id=3, seq=12, sender="agent-1"))
    await b._handle_frame(msg_frame(msg_id=4, seq=13, sender="agent-1"))

    assert [message.text for message in got] == ["same task", "different task"]
    assert b.comm.sync_acks == [10, 11, 12, 13]


@pytest.mark.asyncio
async def test_agent_hop_limit_is_fail_closed(tmp_path):
    got = []

    async def on_message(message):
        got.append(message)

    b = make_bridge(tmp_path, on_message)
    b.comm.conv_type = "group"
    b._policy.group_policy = "open"
    b._policy.allow_agent_senders = True
    b._policy.agent_allowlist = ["agent-1"]
    b._policy.max_agent_hops = 2
    b.comm.messages["conv-1:1"] = detail(
        sender="agent-1",
        sender_type="AGENT",
        mentions=[{"type": "member", "member_id": "me-1"}],
        metadata={"agent_hop_count": 3},
    )

    await b._handle_frame(msg_frame(sender="agent-1"))

    assert got == []
    assert b.comm.sync_acks == [10]


@pytest.mark.asyncio
async def test_agent_delivery_failure_remains_retryable(tmp_path):
    attempts = []

    async def flaky_delivery(message):
        attempts.append(message.message_id)
        if len(attempts) == 1:
            raise RuntimeError("gateway unavailable")

    b = make_bridge(tmp_path, flaky_delivery)
    b.comm.conv_type = "group"
    b._policy.group_policy = "open"
    b._policy.allow_agent_senders = True
    b._policy.agent_allowlist = ["agent-1"]
    b.comm.messages["conv-1:1"] = detail(
        sender="agent-1",
        sender_type="AGENT",
        mentions=[{"type": "member", "member_id": "me-1"}],
    )

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        await b._handle_frame(msg_frame(sender="agent-1"))
    await b._handle_frame(msg_frame(sender="agent-1"))

    assert attempts == ["1", "1"]
    assert b.comm.sync_acks == [10]


@pytest.mark.asyncio
async def test_outbound_messages_propagate_agent_loop_metadata(tmp_path):
    b = make_bridge(tmp_path, lambda _message: None)

    await b.send("conv-1", "reply", metadata={"agent_hop_count": 2})

    metadata = b.comm.sent_messages[-1][2]["metadata"]
    assert metadata["agent_hop_count"] == 3
    assert metadata["agent_origin_member_id"] == "me-1"
    assert metadata["agent_trace_id"]


@pytest.mark.asyncio
async def test_dm_reject_notice_throttled(tmp_path):
    async def on_message(m):
        pass

    b = make_bridge(tmp_path, on_message)
    b._policy.dm_policy = "owner"
    b.owner_member_id = "boss-1"
    b.comm.sent = []

    async def send_message(conv_id, text, **kw):
        b.comm.sent.append(text)
        from cws_agent_sdk.types import SendReceipt

        return SendReceipt(message_id="n-1", conversation_id=conv_id)

    b.comm.send_message = send_message
    b.comm.messages["conv-1:1"] = detail(sender="stranger-1")
    b.comm.messages["conv-1:2"] = detail(msg_id=2, seq=11, sender="stranger-1")
    await b._handle_frame(msg_frame(sender="stranger-1"))
    await b._handle_frame(msg_frame(msg_id=2, seq=11, sender="stranger-1"))
    assert len(b.comm.sent) == 1  # one notice, throttled


@pytest.mark.asyncio
async def test_group_reject_notice_requires_live_human_mention(tmp_path):
    b = make_bridge(tmp_path, lambda _message: None)
    b.comm.conv_type = "group"
    b._policy.group_policy = "disabled"
    b._group_mode_overrides["conv-1"] = "silent"
    b._policy.group_configs["conv-1"] = {
        "mode": "silent",
        "allow_from": ["*"],
    }
    mention = [{"type": "member", "member_id": "me-1"}]
    b.comm.messages["conv-1:1"] = detail(mentions=mention)
    b.comm.messages["conv-1:2"] = detail(msg_id=2, seq=11, text="background")
    b.comm.messages["conv-1:3"] = detail(
        msg_id=3, seq=12, mentions=mention, text="replayed mention"
    )

    await b._handle_frame(msg_frame())
    await b._handle_frame(msg_frame(msg_id=2, seq=11))
    await b._deliver_by_id("conv-1", 3, 12)

    assert len(b.comm.sent_messages) == 1
    assert "disabled" in b.comm.sent_messages[0][1]
    assert b.comm.sync_acks == [10, 11, 12]


@pytest.mark.asyncio
async def test_dedup_persistence_across_restart(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    b = make_bridge(tmp_path, on_message)
    b.comm.messages["conv-1:1"] = detail()
    await b._handle_frame(msg_frame())
    b._storage.write_json("dedup.json", list(b._seen.keys()))

    b2 = make_bridge(tmp_path, on_message)
    b2.comm.messages["conv-1:1"] = detail()
    await b2._handle_frame(msg_frame())
    assert len(got) == 1  # second bridge skips via persisted dedup

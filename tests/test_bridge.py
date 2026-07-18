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


def detail(msg_id=1, conv="conv-1", seq=10, sender="user-7", text="hello"):
    return {
        "message": {
            "id": msg_id,
            "conversation_id": conv,
            "seq": seq,
            "sender_id": sender,
            "sender_type": "HUMAN",
            "client_msg_id": f"cm-{msg_id}",
        },
        "content": {"content_type": "text", "body": {"text": text}},
    }


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
    b.comm.messages["conv-1:1"] = detail(msg_id=1, seq=10, text="earlier chatter")
    b.comm.messages["conv-1:2"] = detail(msg_id=2, seq=11, text="hello smart")
    await b._handle_frame(msg_frame(msg_id=1, seq=10))
    await b._handle_frame(msg_frame(msg_id=2, seq=11))
    # smart mode: no mention required, both delivered, hint attached
    assert len(got) == 2
    assert "smart-mode" in got[1].metadata.get("smart_mode_hint", "")
    assert "earlier chatter" in got[1].metadata.get("group_context", "")


@pytest.mark.asyncio
async def test_group_silent_mode_observes_without_reply(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    b = make_bridge(tmp_path, on_message)
    b.comm.conv_type = "group"
    b._group_mode_overrides["conv-1"] = "silent"
    b.comm.messages["conv-1:1"] = detail()
    await b._handle_frame(msg_frame())
    assert len(got) == 1
    assert got[0].metadata["group_silent"] is True
    assert b.comm.sync_acks == [10]  # consumed silently


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

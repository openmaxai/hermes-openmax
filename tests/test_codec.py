import json

from cws_agent_sdk.codec import (
    FRAME_MESSAGE,
    decode_frame,
    encode_read_receipt,
    encode_typing,
    new_client_msg_id,
)


def test_decode_message_frame():
    raw = json.dumps(
        {
            "type": "message",
            "id": "abc",
            "timestamp": 1784279444000,
            "org_id": "org-1",
            "payload": {
                "id": 12345,
                "conversation_id": "conv-1",
                "sender_id": "member-9",
                "sender_type": "human",
                "seq": 77,
            },
        }
    )
    f = decode_frame(raw)
    assert f is not None
    assert f.type == FRAME_MESSAGE
    assert f.org_id == "org-1"
    assert f.payload["id"] == 12345
    assert f.payload["seq"] == 77


def test_decode_frame_malformed():
    assert decode_frame("not json") is None
    assert decode_frame(json.dumps({"payload": {}})) is None  # missing type
    assert decode_frame(json.dumps([1, 2])) is None


def test_encode_typing_roundtrip():
    raw = encode_typing("conv-1", "member-1", "start")
    obj = json.loads(raw)
    assert obj["type"] == "typing"
    assert obj["payload"] == {
        "conversation_id": "conv-1",
        "user_id": "member-1",
        "action": "start",
    }


def test_encode_read_receipt_roundtrip():
    obj = json.loads(encode_read_receipt("conv-2", 42))
    assert obj["type"] == "read_receipt"
    assert obj["payload"]["read_until_seq"] == 42


def test_client_msg_id_shape():
    a, b = new_client_msg_id(), new_client_msg_id()
    assert a != b
    assert len(a) <= 64

"""Frame codec for the cws-comm WebSocket.

Frame envelope (cws-comm internal/transport/ws/frame.go):
  { "type": "...", "id": "...", "timestamp": <unix-ms>, "org_id": "...", "payload": {...} }

The `message` frame is THIN: it carries id/seq/sender but normally no content —
clients refetch the body over REST.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# Server->client frame types
FRAME_PING = "ping"
FRAME_PONG = "pong"
FRAME_MESSAGE = "message"
FRAME_MESSAGE_ACK = "message_ack"
FRAME_TYPING = "typing"
FRAME_READ_RECEIPT = "read_receipt"
FRAME_READ_STATE = "read_state_update"
FRAME_DELIVERY_STATE = "delivery_state_update"
FRAME_PRESENCE = "presence"
FRAME_SYSTEM = "system"
FRAME_ERROR = "error"
FRAME_SYNC = "sync"

# Fatal close codes (conn.go): do not reconnect blindly.
CLOSE_HEARTBEAT_TIMEOUT = 4001
CLOSE_AUTH_FAILURE = 4002
CLOSE_SESSION_EXPIRED = 4003
CLOSE_RATE_LIMITED = 4004
CLOSE_ORG_SUSPENDED = 4005
CLOSE_DUPLICATE_DEVICE = 4006


@dataclass
class Frame:
    type: str
    payload: dict = field(default_factory=dict)
    id: str = ""
    org_id: str = ""
    timestamp: int = 0
    raw: Any = None


def decode_frame(raw: str | bytes) -> Optional[Frame]:
    """Parse one WS text frame. Returns None on malformed input."""
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or not obj.get("type"):
        return None
    return Frame(
        type=str(obj["type"]),
        payload=obj.get("payload") or {},
        id=str(obj.get("id") or ""),
        org_id=str(obj.get("org_id") or ""),
        timestamp=int(obj.get("timestamp") or 0),
        raw=obj,
    )


def encode_pong() -> str:
    """Application-level JSON pong (zylos parity: some deployments front the
    WS with proxies that drop protocol-level ping/pong)."""
    return json.dumps({"type": FRAME_PONG, "timestamp": int(time.time() * 1000)})


def encode_ping() -> str:
    return json.dumps({"type": FRAME_PING, "timestamp": int(time.time() * 1000)})


def encode_typing(conversation_id: str, user_id: str, action: str = "start") -> str:
    return json.dumps(
        {
            "type": FRAME_TYPING,
            "timestamp": int(time.time() * 1000),
            "payload": {
                "conversation_id": conversation_id,
                "user_id": user_id,
                "action": action,
            },
        }
    )


def encode_read_receipt(conversation_id: str, read_until_seq: int) -> str:
    return json.dumps(
        {
            "type": FRAME_READ_RECEIPT,
            "timestamp": int(time.time() * 1000),
            "payload": {
                "conversation_id": conversation_id,
                "read_until_seq": int(read_until_seq),
            },
        }
    )


# -- contract v1 classification (openmax-agent-sdk schemas/v1) ---------------

_FRAME_KIND = {
    FRAME_MESSAGE: "message",
    FRAME_MESSAGE_ACK: "message_ack",
    FRAME_SYSTEM: "system",
    FRAME_ERROR: "error",
    FRAME_PING: "heartbeat",
    FRAME_PONG: "heartbeat",
    FRAME_TYPING: "presence",
    FRAME_READ_STATE: "presence",
    FRAME_READ_RECEIPT: "presence",
    FRAME_DELIVERY_STATE: "presence",
    FRAME_PRESENCE: "presence",
}


def classify_frame(frame: dict) -> str:
    """Contract FrameKind: message/message_ack/system/error/heartbeat/presence/unknown."""
    return _FRAME_KIND.get(str((frame or {}).get("type", "")), "unknown")


def classify_system_event(event: str) -> Optional[str]:
    """Contract system-event classes: recall/edit/config_update/connection/channel/None."""
    e = str(event or "")
    if e in ("message.recalled", "message.deleted"):
        return "recall"
    if e == "message.updated":
        return "edit"
    if e.startswith("agent.config."):
        return "config_update"
    if e.startswith("connection."):
        return "connection"
    if e.startswith("channel."):
        return "channel"
    return None


def new_client_msg_id() -> str:
    """Idempotency key for outbound sends (<=64 chars)."""
    return f"hermes-{uuid.uuid4().hex}"


def looks_like_markdown(text: str) -> bool:
    """Port of zylos-openmax's outbound markdown heuristic — the FE renders
    content_type 'markdown' differently from plain 'text'."""
    import re

    if not text:
        return False
    patterns = (
        r"(^|\n)#{1,6}\s",          # headers
        r"\*\*[^*\n]+\*\*",          # bold
        r"(^|\n)\s*[-*+]\s+\S",      # bullet list
        r"(^|\n)\s*\d+\.\s+\S",      # ordered list
        r"```",                       # code fence
        r"\[[^\]\n]+\]\([^)\n]+\)",  # link
        r"(^|\n)>\s+\S",             # blockquote
        r"`[^`\n]+`",                # inline code
    )
    return any(re.search(p, text) for p in patterns)

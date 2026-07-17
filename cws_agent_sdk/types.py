"""Normalized domain types exchanged between SDK and adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class InboundMessage:
    """A CWS message normalized for runtime delivery."""

    message_id: str
    conversation_id: str
    org_id: str
    text: str
    sender_id: str = ""
    sender_name: str = ""
    sender_type: str = ""  # human | agent | system
    conversation_type: str = "dm"  # dm | group
    seq: Optional[int] = None
    reply_to_message_id: Optional[str] = None
    created_at: Optional[str] = None
    media: list[dict] = field(default_factory=list)
    mentions: list[dict] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


@dataclass
class SendReceipt:
    """Result of a successful outbound send."""

    message_id: str
    conversation_id: str
    raw: Any = None

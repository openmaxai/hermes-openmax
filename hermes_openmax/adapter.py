"""CWS platform adapter — the thin translation layer.

Everything CWS-protocol-shaped lives in ``cws_agent_sdk``; this class only
maps between the SDK's normalized types and Hermes gateway types:

  inbound:  sdk InboundMessage  -> MessageEvent -> self.handle_message()
  outbound: gateway calls send() -> sdk bridge.send()

Delivery invariant: the SDK only advances its ack watermark after the
``on_message`` callback returns without raising, and ``_on_inbound`` awaits
``handle_message`` — so a message is only acked once the gateway has truly
accepted it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from cws_agent_sdk import CwsBridge, CwsConfig, InboundMessage
from cws_agent_sdk.providers import FileStorage

logger = logging.getLogger(__name__)

_STATE_DIR = "~/.hermes/platforms/cws"


class CwsAdapter(BasePlatformAdapter):
    """OpenMax Workspace (CWS) adapter."""

    _last_instance: Optional["CwsAdapter"] = None

    def __init__(self, config):
        super().__init__(config)
        self._bridge: Optional[CwsBridge] = None
        CwsAdapter._last_instance = self

    # -- lifecycle -----------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        cfg = CwsConfig.from_env()
        missing = cfg.validate()
        if missing:
            logger.error("[cws] missing config: %s", ", ".join(missing))
            return False
        self._bridge = CwsBridge(
            cfg,
            storage=FileStorage(_STATE_DIR),
            logger=logger,
            on_message=self._on_inbound,
        )
        await self._bridge.start()
        logger.info("[cws] bridge started (org=%s)", cfg.org_id or "<from-token>")
        return True

    async def disconnect(self) -> None:
        if self._bridge:
            await self._bridge.stop()
            self._bridge = None

    @classmethod
    def last_instance_connected(cls) -> bool:
        inst = cls._last_instance
        return bool(inst and inst._bridge and inst._bridge.is_running())

    # -- outbound: gateway -> CWS ---------------------------------------

    async def send(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        if not self._bridge:
            return SendResult(success=False, error="cws bridge not connected")
        try:
            receipt = await self._bridge.send(
                conversation_id=chat_id,
                content=text,
                reply_to=reply_to,
            )
            return SendResult(success=True, message_id=receipt.message_id)
        except Exception as exc:  # noqa: BLE001 — surface any send failure to gateway
            logger.warning("[cws] send failed conv=%s: %s", chat_id, exc)
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str) -> None:
        if self._bridge:
            try:
                await self._bridge.send_typing(chat_id)
            except Exception:  # noqa: BLE001 — typing is best-effort
                pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        if not self._bridge:
            return {"chat_id": chat_id}
        return await self._bridge.get_conversation_info(chat_id)

    # -- inbound: CWS -> gateway ----------------------------------------

    async def _on_inbound(self, msg: InboundMessage) -> None:
        """SDK delivery callback. Raising here prevents the ack watermark
        from advancing, so the message is replayed via /sync later."""
        source = self.build_source(
            chat_id=msg.conversation_id,
            chat_type="dm" if msg.conversation_type == "dm" else "group",
            user_id=msg.sender_id or None,
            user_name=msg.sender_name or None,
            is_bot=(msg.sender_type == "agent"),
            message_id=msg.message_id,
        )
        event = MessageEvent(
            text=msg.text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg.message_id,
            reply_to_message_id=msg.reply_to_message_id,
            media_urls=[m["path"] for m in msg.media if m.get("path")],
            media_types=[m.get("type", "") for m in msg.media],
            metadata={
                "cws_org_id": msg.org_id,
                "cws_seq": msg.seq,
                **msg.metadata,
            },
            raw_message=msg.raw,
        )
        await self.handle_message(event)

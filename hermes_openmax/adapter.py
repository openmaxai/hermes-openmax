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
from cws_agent_sdk.access_policy import AccessPolicyConfig
from cws_agent_sdk.providers import FileStorage


def _policy_from_env() -> AccessPolicyConfig:
    import os

    def flag(name: str, default: bool) -> bool:
        raw = os.getenv(name, "").strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "on")

    allow = [s.strip() for s in os.getenv("CWS_ALLOWED_USERS", "").split(",") if s.strip()]
    return AccessPolicyConfig(
        group_require_mention=flag("CWS_GROUP_REQUIRE_MENTION", True),
        allow_agent_senders=flag("CWS_ALLOW_AGENT_SENDERS", False),
        allow_sibling_dm=flag("CWS_ALLOW_SIBLING_DM", False),
        dm_allowlist=[] if flag("CWS_ALLOW_ALL_USERS", True) else allow,
    )

logger = logging.getLogger(__name__)

_STATE_DIR = "~/.hermes/platforms/cws"


class _SdkLogger:
    """Adapt stdlib logging to the SDK's Logger protocol (.log/.warn *args)."""

    def log(self, *args) -> None:
        logger.info("[cws] %s", " ".join(str(a) for a in args))

    def warn(self, *args) -> None:
        logger.warning("[cws] %s", " ".join(str(a) for a in args))


class CwsAdapter(BasePlatformAdapter):
    """OpenMax Workspace (CWS) adapter."""

    _last_instance: Optional["CwsAdapter"] = None

    def __init__(self, config, **kwargs):
        from gateway.config import Platform

        super().__init__(config=config, platform=Platform("cws"))
        self._bridge: Optional[CwsBridge] = None
        self._orientation: str = ""
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
            logger=_SdkLogger(),
            on_message=self._on_inbound,
            policy=_policy_from_env(),
            version=cfg.client_version,
            on_config_event=self._on_config_event,
        )
        await self._bridge.start()
        logger.info("[cws] bridge started (org=%s)", cfg.org_id or "<from-token>")
        await self._build_orientation()
        return True

    async def _build_orientation(self) -> None:
        """Workspace orientation injected per-turn (zylos-openmax parity:
        the agent should know who it is in this org, who its owner is, and
        what workspace capabilities it has)."""
        try:
            me = await self._bridge.core.me()
            owner_name = ""
            owner_id = self._bridge.owner_member_id
            if owner_id:
                owner = await self._bridge.core.get_member(owner_id)
                owner_name = str(owner.get("display_name") or "")
            self._orientation = (
                "# OpenMax Workspace context\n"
                f"You are workspace member '{me.get('display_name')}' (agent, member_id "
                f"{me.get('member_id')}) in org '{me.get('org_name')}' ({me.get('org_slug')}). "
                f"Your responsible human owner is '{owner_name or 'unknown'}'"
                f"{f' (member_id {owner_id})' if owner_id else ''}.\n"
                "You have native workspace tools: workspace_tasks (projects/issues/tasks), "
                "workspace_kb (knowledge base), workspace_members (directory, DMs). Use them "
                "for any request about workspace work items or knowledge instead of guessing.\n"
                "Etiquette: reply in the conversation's language; in group conversations you "
                "only see messages that mention you; keep replies concise and actionable."
            )
            logger.info("[cws] orientation built (%d chars)", len(self._orientation))
        except Exception as exc:  # noqa: BLE001 — orientation is an enhancement
            logger.warning("[cws] orientation build failed: %s", exc)

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
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._bridge:
            return SendResult(success=False, error="cws bridge not connected")
        try:
            receipt = await self._bridge.send(
                conversation_id=chat_id,
                content=content,
                reply_to=reply_to,
                metadata=metadata,
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

    async def _on_config_event(self, event: str, data: Dict[str, Any]) -> None:
        """agent.config.* events the SDK doesn't fully interpret land here."""
        logger.info("[cws] config event %s: %s", event, {k: data.get(k) for k in list(data)[:6]})

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
            channel_prompt=self._orientation or None,
            raw_message=msg.raw,
        )
        await self.handle_message(event)

    # -- outbound media -------------------------------------------------------

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local image via the presigned two-phase flow, then send a
        message referencing it. Falls back to a caption+URL text message."""
        import os

        import httpx

        if not self._bridge:
            return SendResult(success=False, error="cws bridge not connected")
        try:
            if os.path.isfile(image_url):
                size = os.path.getsize(image_url)
                fname = os.path.basename(image_url)
                ctype = "image/png" if fname.lower().endswith(".png") else "image/jpeg"
                prep = await self._bridge.artifacts.prepare_conversation_upload(
                    chat_id, fname, ctype, size
                )
                if not prep.get("instant_upload"):
                    with open(image_url, "rb") as fh:
                        async with httpx.AsyncClient(timeout=120) as up:
                            resp = await up.put(
                                prep["upload_url"], content=fh.read(),
                                headers=prep.get("headers") or {},
                            )
                            resp.raise_for_status()
                node = await self._bridge.artifacts.finalize_conversation_upload(
                    prep["upload_token"]
                )
                text = caption or f"[image] {fname}"
                receipt = await self._bridge.send(chat_id, text, reply_to=reply_to,
                                                  metadata={"attachment": node})
                return SendResult(success=True, message_id=receipt.message_id)
            # Remote URL: no re-hosting — send as markdown image link.
            text = f"![{caption or 'image'}]({image_url})"
            receipt = await self._bridge.send(chat_id, text, reply_to=reply_to)
            return SendResult(success=True, message_id=receipt.message_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cws] send_image failed, falling back to text: %s", exc)
            try:
                receipt = await self._bridge.send(
                    chat_id, f"{caption or ''} {image_url}".strip(), reply_to=reply_to
                )
                return SendResult(success=True, message_id=receipt.message_id)
            except Exception as exc2:  # noqa: BLE001
                return SendResult(success=False, error=str(exc2))

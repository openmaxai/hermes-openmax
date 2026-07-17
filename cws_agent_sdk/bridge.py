"""CwsBridge — the orchestrator an adapter talks to.

Wires TokenManager + CwsWsClient + CommService into one lifecycle:

  start() -> exchange token -> open WS (one-shot ticket per connect)
  message frame (thin) -> dedupe -> REST-fetch body -> normalize
      -> await on_message(InboundMessage)          # the delivery point
      -> advance watermarks (mark_read + sync/ack) # ONLY after success
  reconnect -> POST /sync since watermark -> replay missed -> ack

Delivery invariant: watermarks move only after on_message returns without
raising. A failed delivery keeps the seq un-acked so /sync replays it.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

from .codec import FRAME_MESSAGE, FRAME_SYNC, Frame, encode_typing
from .config import CwsConfig
from .errors import CwsApiError
from .http import CwsHttpClient
from .providers import FileStorage, Logger, StdLogger
from .services import CommService
from .token import TokenManager
from .types import InboundMessage, SendReceipt
from .ws import CwsWsClient

_SYNC_SEQ_KEY = "sync_seq.json"
_DEDUP_MAX = 2048


class CwsBridge:
    def __init__(
        self,
        cfg: CwsConfig,
        *,
        storage: FileStorage,
        on_message: Callable[[InboundMessage], Awaitable[None]],
        logger: Optional[Logger] = None,
    ):
        self._cfg = cfg
        self._storage = storage
        self._on_message = on_message
        self._log = logger or StdLogger("[cws-bridge]")
        self._tokens = TokenManager(cfg, storage=storage, logger=self._log)
        self._http = CwsHttpClient(cfg, self._tokens, logger=self._log)
        self.comm = CommService(self._http)
        self._ws = CwsWsClient(
            cfg,
            ticket_provider=self._tokens.get_ws_ticket,
            on_frame=self._handle_frame,
            on_reconnected=self._sync_missed,
            on_auth_reset=self._tokens.invalidate,
            logger=self._log,
        )
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._inflight: set[str] = set()
        self._own_client_msg_ids: "OrderedDict[str, None]" = OrderedDict()
        self._sync_seq: int = int((storage.read_json(_SYNC_SEQ_KEY) or {}).get("seq", 0))
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        # Fail fast on bad credentials before going async.
        await self._tokens.get_access_token()
        self._running = True
        self._ws.start()
        # Catch up anything missed while offline.
        await self._sync_missed()

    async def stop(self) -> None:
        self._running = False
        await self._ws.stop()
        await self._http.aclose()
        await self._tokens.aclose()

    def is_running(self) -> bool:
        return self._running and self._ws.is_open()

    # -- outbound ------------------------------------------------------------

    async def send(
        self,
        conversation_id: str,
        content: str,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SendReceipt:
        from .codec import new_client_msg_id

        cmid = new_client_msg_id()
        self._remember_own(cmid)
        return await self.comm.send_message(
            conversation_id,
            content,
            reply_to=reply_to,
            metadata=metadata,
            client_msg_id=cmid,
        )

    async def send_typing(self, conversation_id: str) -> None:
        if self._ws.is_open():
            await self._ws.send_text(
                encode_typing(conversation_id, self._cfg.member_id or self._cfg.identity_id)
            )

    async def get_conversation_info(self, conversation_id: str) -> dict:
        try:
            return await self.comm.get_conversation(conversation_id)
        except CwsApiError:
            return {"id": conversation_id}

    # -- inbound ---------------------------------------------------------------

    async def _handle_frame(self, frame: Frame) -> None:
        if frame.type == FRAME_MESSAGE:
            await self._handle_message_frame(frame)
        elif frame.type == FRAME_SYNC:
            for ev in frame.payload.get("events") or []:
                await self._deliver_by_id(
                    str(ev.get("conversation_id", "")),
                    ev.get("message_id"),
                    int(ev.get("seq") or 0),
                )
        # typing / read_state / presence / acks: no runtime delivery needed.

    async def _handle_message_frame(self, frame: Frame) -> None:
        p = frame.payload
        message_id = p.get("id")
        conversation_id = str(p.get("conversation_id", ""))
        seq = int(p.get("seq") or 0)
        sender_id = str(p.get("sender_id", ""))
        if message_id is None or not conversation_id:
            return
        if self._cfg.member_id and sender_id == self._cfg.member_id:
            return  # own echo
        await self._deliver_by_id(conversation_id, message_id, seq, frame=frame)

    async def _deliver_by_id(
        self, conversation_id: str, message_id, seq: int, frame: Optional[Frame] = None
    ) -> None:
        key = f"{conversation_id}:{message_id}"
        # The in-flight guard closes the WS-frame vs /sync-replay race: both
        # can observe the same undelivered message concurrently.
        if key in self._seen or key in self._inflight:
            return
        self._inflight.add(key)
        try:
            detail = await self.comm.get_message(conversation_id, message_id)
            msg = self._normalize(detail, conversation_id, seq)
            if msg is None:
                self._mark_seen(key)
                await self._advance(conversation_id, seq)
                return
            # Delivery point — exceptions propagate, watermark stays put, /sync replays.
            await self._on_message(msg)
            self._mark_seen(key)
            await self._advance(conversation_id, msg.seq or seq)
        finally:
            self._inflight.discard(key)

    def _normalize(
        self, detail: dict, conversation_id: str, seq: int
    ) -> Optional[InboundMessage]:
        m = detail.get("message") or detail
        content = detail.get("content") or m.get("content") or {}
        client_msg_id = str(m.get("client_msg_id") or "")
        if client_msg_id and client_msg_id in self._own_client_msg_ids:
            return None  # our own outbound echoed back
        sender_type = str(m.get("sender_type", "")).lower()
        if self._cfg.member_id and str(m.get("sender_id", "")) == self._cfg.member_id:
            return None
        text = self._extract_text(m, content)
        return InboundMessage(
            message_id=str(m.get("id", "")),
            conversation_id=str(m.get("conversation_id", conversation_id)),
            org_id=str(m.get("org_id", self._cfg.org_id)),
            text=text,
            sender_id=str(m.get("sender_id", "")),
            sender_type=sender_type or "human",
            seq=int(m.get("seq") or seq or 0),
            reply_to_message_id=str(m["parent_id"]) if m.get("parent_id") else None,
            created_at=m.get("created_at") or m.get("timestamp"),
            metadata=m.get("metadata") or {},
            raw=detail,
        )

    @staticmethod
    def _extract_text(m: dict, content: dict) -> str:
        """Best-effort text extraction across content shapes."""
        body = content.get("body") if isinstance(content, dict) else None
        if isinstance(body, dict):
            for key in ("text", "content", "markdown"):
                if isinstance(body.get(key), str) and body[key]:
                    return body[key]
        if isinstance(content, str) and content:
            return content
        fallback = m.get("fallback_text")
        if isinstance(fallback, str) and fallback:
            return fallback
        if isinstance(m.get("content"), str):
            return m["content"]
        return ""

    # -- watermarks ---------------------------------------------------------

    async def _advance(self, conversation_id: str, seq: int) -> None:
        if seq <= 0:
            return
        try:
            await self.comm.mark_read(conversation_id, seq)
        except CwsApiError as exc:
            self._log.warn("mark_read failed:", exc)
        if seq > self._sync_seq:
            self._sync_seq = seq
            self._storage.write_json(_SYNC_SEQ_KEY, {"seq": seq})
            try:
                await self.comm.sync_ack(self._cfg.device_id, seq)
            except CwsApiError as exc:
                self._log.warn("sync_ack failed:", exc)

    async def _sync_missed(self) -> None:
        """Replay events missed while offline (at-least-once, ordered by seq)."""
        cursor = self._sync_seq
        while True:
            try:
                res = await self.comm.sync(cursor, self._cfg.device_id)
            except CwsApiError as exc:
                self._log.warn("/sync failed:", exc)
                return
            events = res.get("events") or []
            for ev in events:
                await self._deliver_by_id(
                    str(ev.get("conversation_id", "")),
                    ev.get("message_id"),
                    int(ev.get("seq") or 0),
                )
            if not res.get("has_more"):
                return
            try:
                cursor = int(res.get("next_cursor") or 0)
            except (TypeError, ValueError):
                return
            if cursor <= 0:
                return

    # -- small state helpers ---------------------------------------------------

    def _mark_seen(self, key: str) -> None:
        self._seen[key] = None
        while len(self._seen) > _DEDUP_MAX:
            self._seen.popitem(last=False)

    def _remember_own(self, client_msg_id: str) -> None:
        self._own_client_msg_ids[client_msg_id] = None
        while len(self._own_client_msg_ids) > _DEDUP_MAX:
            self._own_client_msg_ids.popitem(last=False)

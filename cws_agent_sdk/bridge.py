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

from .access_policy import AccessPolicyConfig, decide_inbound
from .codec import FRAME_MESSAGE, FRAME_SYNC, FRAME_SYSTEM, Frame, encode_typing
from .config import CwsConfig
from .errors import CwsApiError
from .http import CwsHttpClient
from .providers import FileStorage, Logger, StdLogger
from .reporters import (
    OVERDUE_NOTICE,
    BillingGate,
    MetricsReporter,
    OnlineReporter,
    RuntimeStateProvider,
)
from .services import AsService, CommService, ConnService, CoreService, KbService, TmService
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
        policy: Optional[AccessPolicyConfig] = None,
        version: str = "",
        runtime_state: Optional[RuntimeStateProvider] = None,
        billing_gate_enabled: bool = True,
        metrics_interval_s: float = 300.0,
        on_config_event: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        ack_reaction: str = "eyes",  # "" disables the received-ack reaction
        ack_reaction_ttl_s: float = 600.0,
    ):
        self._cfg = cfg
        self._storage = storage
        self._on_message = on_message
        self._log = logger or StdLogger("[cws-bridge]")
        self._tokens = TokenManager(cfg, storage=storage, logger=self._log)
        self._http = CwsHttpClient(cfg, self._tokens, logger=self._log)
        self.comm = CommService(self._http)
        self.core = CoreService(self._http)
        self.tm = TmService(self._http)
        self.kb = KbService(self._http)
        self.artifacts = AsService(self._http)
        self.conn = ConnService(self._http)
        self._online = OnlineReporter(self._http, logger=self._log)
        self._metrics = MetricsReporter(
            self._http,
            lambda: self._cfg.member_id,
            version=version or cfg.client_version,
            runtime_state=runtime_state,
            logger=self._log,
        )
        self._billing: Optional[BillingGate] = (
            BillingGate(self._http, logger=self._log) if billing_gate_enabled else None
        )
        self._metrics_interval_s = metrics_interval_s
        self._metrics_task: Optional[asyncio.Task] = None
        self._on_config_event = on_config_event
        self.owner_member_id: str = ""
        self._group_mode_overrides: dict[str, str] = {}  # conv_id -> raw mode
        self._ack_reaction = ack_reaction
        self._ack_ttl_s = ack_reaction_ttl_s
        self._pending_acks: dict[str, str] = {}  # conv_id -> message_id with our ack on it
        self._ws = CwsWsClient(
            cfg,
            ticket_provider=self._tokens.get_ws_ticket,
            on_frame=self._handle_frame,
            on_reconnected=self._sync_missed,
            on_auth_reset=self._tokens.invalidate,
            logger=self._log,
        )
        self._policy = policy or AccessPolicyConfig()
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._inflight: set[str] = set()
        self._own_client_msg_ids: "OrderedDict[str, None]" = OrderedDict()
        self._conv_types: "OrderedDict[str, str]" = OrderedDict()  # conv_id -> dm|group|...
        self._member_names: "OrderedDict[str, str]" = OrderedDict()  # member_id -> display_name
        self._sync_seq: int = int((storage.read_json(_SYNC_SEQ_KEY) or {}).get("seq", 0))
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        # Fail fast on bad credentials before going async.
        await self._tokens.get_access_token()
        await self._resolve_identity()
        self._running = True
        if self._cfg.member_id:
            await self._online.report(self._cfg.member_id)
            self._metrics_task = asyncio.create_task(self._metrics_loop(), name="cws-metrics")
        self._ws.start()
        # Catch up anything missed while offline.
        await self._sync_missed()

    async def stop(self) -> None:
        self._running = False
        if self._metrics_task:
            self._metrics_task.cancel()
            self._metrics_task = None
        await self._ws.stop()
        await self._http.aclose()
        await self._tokens.aclose()

    async def _resolve_identity(self) -> None:
        """Fill member_id from /me when unset; pull authoritative owner."""
        try:
            if not self._cfg.member_id:
                me = await self.core.me()
                self._cfg.member_id = str(me.get("member_id") or "")
                if not self._cfg.org_id:
                    self._cfg.org_id = str(me.get("org_id") or "")
            if self._cfg.member_id:
                member = await self.core.get_member(self._cfg.member_id)
                self.owner_member_id = str(member.get("owner_member_id") or "")
        except CwsApiError as exc:
            self._log.warn("identity/owner resolve failed (non-fatal):", exc)

    async def _metrics_loop(self) -> None:
        while self._running:
            try:
                await self._metrics.report_once()
            except Exception as exc:  # noqa: BLE001 — reporting never breaks the loop
                self._log.warn("metrics tick failed:", exc)
            await asyncio.sleep(self._metrics_interval_s)

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
        receipt = await self.comm.send_message(
            conversation_id,
            content,
            reply_to=reply_to,
            metadata=metadata,
            client_msg_id=cmid,
        )
        # Replying resolves the pending received-ack (zylos parity) and any
        # lingering typing bubble.
        await self._clear_ack(conversation_id)
        try:
            await self.send_typing(conversation_id, "stop")
        except Exception:  # noqa: BLE001 — cosmetic
            pass
        return receipt

    async def send_image_file(
        self,
        conversation_id: str,
        image_path: str,
        *,
        caption: str = "",
        reply_to: Optional[str] = None,
    ) -> SendReceipt:
        """Upload a local image (presigned two-phase) and send it as a native
        IMAGE message with a proper attachment."""
        import mimetypes
        import os

        import httpx as _httpx

        size = os.path.getsize(image_path)
        fname = os.path.basename(image_path)
        ctype = mimetypes.guess_type(fname)[0] or "image/png"
        prep = await self.artifacts.prepare_conversation_upload(
            conversation_id, fname, ctype, size
        )
        if not prep.get("instant_upload"):
            with open(image_path, "rb") as fh:
                async with _httpx.AsyncClient(timeout=300) as up:
                    resp = await up.put(
                        prep["upload_url"], content=fh.read(),
                        headers=prep.get("headers") or {},
                    )
                    resp.raise_for_status()
        fin = await self.artifacts.finalize_conversation_upload(prep["upload_token"])
        receipt = await self.comm.send_image_message(
            conversation_id,
            artifact_id=str(fin.get("artifact_id", "")),
            file_name=fname,
            content_type=ctype,
            size_bytes=size,
            caption=caption,
            reply_to=reply_to,
        )
        await self._clear_ack(conversation_id)
        return receipt

    async def send_typing(self, conversation_id: str, action: str = "start") -> None:
        if self._ws.is_open():
            await self._ws.send_text(
                encode_typing(
                    conversation_id,
                    self._cfg.member_id or self._cfg.identity_id,
                    action,
                )
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
        elif frame.type == FRAME_SYSTEM:
            await self._handle_system_frame(frame)
        # typing / read_state / presence / acks: no runtime delivery needed.

    async def _handle_system_frame(self, frame: Frame) -> None:
        """agent.config.* hot updates: {event, conversation_id, data}."""
        event = str(frame.payload.get("event", ""))
        data = frame.payload.get("data") or {}
        if not event.startswith("agent.config."):
            return
        self._log.log("config event:", event)
        if event == "agent.config.dm_allowlist_changed":
            action = str(data.get("action", "")).lower()
            ids = [str(i) for i in data.get("member_ids") or []]
            if action == "add":
                for i in ids:
                    if i not in self._policy.dm_allowlist:
                        self._policy.dm_allowlist.append(i)
            elif action == "remove":
                self._policy.dm_allowlist = [
                    i for i in self._policy.dm_allowlist if i not in ids
                ]
        elif event == "agent.config.group_mode_changed":
            conv = str(data.get("conversation_id", ""))
            if conv:
                self._group_mode_overrides[conv] = str(data.get("mode", ""))
        elif event == "agent.config.owner_changed":
            self.owner_member_id = str(data.get("new_owner_member_id", ""))
        # Other events (dm_policy / group_scope / group_allowlist / allowfrom)
        # are forwarded to the adapter callback; interpretation is host policy.
        if self._on_config_event:
            try:
                await self._on_config_event(event, data)
            except Exception as exc:  # noqa: BLE001 — host callback must not kill WS
                self._log.warn("on_config_event error:", exc)

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
            conv_info = await self._conversation_info(conversation_id)
            msg.conversation_type = conv_info["type"]
            if conv_info.get("name"):
                msg.metadata["conversation_name"] = conv_info["name"]
            if not msg.sender_name and msg.sender_id:
                msg.sender_name = await self._member_name(msg.sender_id)
            await self._hydrate_media(msg)
            await self._expand_work_references(msg)
            await self._hydrate_reply_context(msg)
            decision = decide_inbound(
                msg,
                self_member_id=self._cfg.member_id,
                cfg=self._effective_policy(conversation_id),
            )
            if decision.handle and self._billing is not None and await self._billing.is_suspended():
                self._log.warn("billing suspended — skipping delivery", conversation_id)
                if self._billing.should_send_overdue_notice(conversation_id):
                    try:
                        await self.comm.send_message(conversation_id, OVERDUE_NOTICE)
                    except CwsApiError as exc:
                        self._log.warn("overdue notice failed:", exc)
                self._mark_seen(key)
                await self._advance(conversation_id, msg.seq or seq)
                return
            if not decision.handle:
                self._log.log(f"policy skip [{decision.reason}] conv={conversation_id} msg={message_id}")
                self._mark_seen(key)
                await self._advance(conversation_id, msg.seq or seq)
                return
            await self._ack_received(conversation_id, str(message_id))
            # Delivery point — exceptions propagate, watermark stays put, /sync replays.
            await self._on_message(msg)
            self._mark_seen(key)
            await self._advance(conversation_id, msg.seq or seq)
        finally:
            self._inflight.discard(key)

    # -- received-ack reaction (zylos parity: 👀 on receipt, cleared on reply) --

    async def _ack_received(self, conversation_id: str, message_id: str) -> None:
        if not self._ack_reaction:
            return
        try:
            await self.comm.add_reaction(message_id, self._ack_reaction)
        except Exception as exc:  # noqa: BLE001 — ack is cosmetic
            self._log.warn("ack reaction failed:", exc)
            return
        self._pending_acks[conversation_id] = message_id
        asyncio.create_task(self._expire_ack(conversation_id, message_id))

    async def _expire_ack(self, conversation_id: str, message_id: str) -> None:
        await asyncio.sleep(self._ack_ttl_s)
        if self._pending_acks.get(conversation_id) == message_id:
            await self._clear_ack(conversation_id)

    async def _clear_ack(self, conversation_id: str) -> None:
        message_id = self._pending_acks.pop(conversation_id, "")
        if not message_id or not self._ack_reaction:
            return
        try:
            await self.comm.remove_reaction(message_id, self._ack_reaction)
        except Exception as exc:  # noqa: BLE001
            self._log.warn("ack reaction clear failed:", exc)

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
        mentions = m.get("mentions") or []
        extra_meta = dict(m.get("metadata") or {})
        priority = m.get("priority")
        if priority is not None:
            extra_meta["cws_priority"] = int(priority)  # 1=urgent 2=high 3=normal
        return InboundMessage(
            mentions=[mm for mm in mentions if isinstance(mm, dict)],
            message_id=str(m.get("id", "")),
            conversation_id=str(m.get("conversation_id", conversation_id)),
            org_id=str(m.get("org_id", self._cfg.org_id)),
            text=text,
            sender_id=str(m.get("sender_id", "")),
            sender_type=sender_type or "human",
            seq=int(m.get("seq") or seq or 0),
            reply_to_message_id=str(m["parent_id"]) if m.get("parent_id") else None,
            created_at=m.get("created_at") or m.get("timestamp"),
            metadata=extra_meta,
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

    async def _member_name(self, member_id: str) -> str:
        cached = self._member_names.get(member_id)
        if cached is not None:
            return cached
        name = ""
        try:
            member = await self.core.get_member(member_id)
            name = str(member.get("display_name") or "")
        except Exception:  # noqa: BLE001 — name lookup must never break delivery
            pass
        self._member_names[member_id] = name
        while len(self._member_names) > 1024:
            self._member_names.popitem(last=False)
        return name

    async def _hydrate_media(self, msg: InboundMessage) -> None:
        """Best-effort: resolve message attachments to local files for vision.

        Attachment shapes vary; we look for artifact URI strings, resolve them
        to presigned URLs, and download into the storage dir. Failures leave
        msg.media entries without a local path — never break delivery."""
        m = (msg.raw or {}).get("message") if isinstance(msg.raw, dict) else None
        attachments = (m or {}).get("attachments") or []
        if not attachments:
            return
        uris = []
        for att in attachments:
            if isinstance(att, str):
                uris.append(att)
            elif isinstance(att, dict):
                uri = att.get("uri") or att.get("artifact_uri") or att.get("url")
                if isinstance(uri, str) and uri:
                    uris.append(uri)
        if not uris:
            return
        try:
            resolved = await self.artifacts.resolve_uris(uris[:10])
        except Exception as exc:  # noqa: BLE001 — media is best-effort
            self._log.warn("attachment resolve failed:", exc)
            return
        import httpx as _httpx

        for uri, info in (resolved.get("resolved") or {}).items():
            url = info.get("download_url")
            if not url:
                continue
            entry = {"uri": uri, "type": info.get("content_type", ""), "name": info.get("name", "")}
            try:
                fname = f"media-{abs(hash(uri)) % 10**10}-{(info.get('name') or 'file')[-60:]}"
                async with _httpx.AsyncClient(timeout=60) as dl:
                    resp = await dl.get(url)
                    resp.raise_for_status()
                    self._storage.write(f"media/{fname}", resp.content)
                    if hasattr(self._storage, "path_for"):
                        entry["path"] = self._storage.path_for(f"media/{fname}")
            except Exception as exc:  # noqa: BLE001 — download is best-effort
                self._log.warn("attachment download failed:", exc)
            msg.media.append(entry)

    async def _expand_work_references(self, msg: InboundMessage) -> None:
        """Expand proj://<id> / issue://<id> URIs in the message into context.

        Result lands in msg.metadata['work_reference_context'] (plain text);
        the adapter forwards it as out-of-band channel context. Best-effort."""
        import re

        refs = re.findall(r"\b(proj|issue)://([0-9a-fA-F-]{36})", msg.text or "")
        if not refs:
            return
        blocks: list[str] = []
        for kind, ref_id in refs[:3]:
            try:
                if kind == "issue":
                    issue = await self.tm.get_issue(ref_id)
                    blocks.append(
                        f"[issue://{ref_id}] title={issue.get('title')!r} "
                        f"status={issue.get('status')} owner={issue.get('owner_member_id')}"
                    )
                else:
                    rows = await self.tm.work_references(project_id=ref_id, limit=10)
                    labels = [f"{r.get('kind')}:{r.get('label')}({r.get('status','')})" for r in rows]
                    blocks.append(f"[proj://{ref_id}] issues: {', '.join(labels) or '(none)'}")
            except Exception as exc:  # noqa: BLE001 — reference expansion is best-effort
                self._log.warn("work-reference expand failed:", exc)
        if blocks:
            msg.metadata["work_reference_context"] = (
                "Referenced workspace items:\n" + "\n".join(blocks)
            )

    async def _hydrate_reply_context(self, msg: InboundMessage) -> None:
        """Quoted-reply context (formatInboundForC4 parity): fetch the parent
        message's text + author so the runtime sees what is being replied to."""
        if not msg.reply_to_message_id:
            return
        try:
            detail = await self.comm.get_message(msg.conversation_id, msg.reply_to_message_id)
            parent = detail.get("message") or detail
            content = detail.get("content") or parent.get("content") or {}
            text = self._extract_text(parent, content)
            author_id = str(parent.get("sender_id", ""))
            msg.metadata["reply_to_text"] = text[:2000]
            msg.metadata["reply_to_author_id"] = author_id
            if author_id:
                msg.metadata["reply_to_author_name"] = await self._member_name(author_id)
        except Exception as exc:  # noqa: BLE001 — quote context is best-effort
            self._log.warn("reply-context hydrate failed:", exc)

    def _effective_policy(self, conversation_id: str) -> AccessPolicyConfig:
        """Apply a per-conversation group-mode override on top of the base policy."""
        mode = self._group_mode_overrides.get(conversation_id, "")
        if not mode:
            return self._policy
        from dataclasses import replace

        low = mode.lower()
        if "mention" in low:
            return replace(self._policy, group_require_mention=True)
        if low in ("open", "all") or "open" in low:
            return replace(self._policy, group_require_mention=False)
        return self._policy

    async def _conversation_info(self, conversation_id: str) -> dict:
        cached = self._conv_types.get(conversation_id)
        if cached:
            return cached
        info_out = {"type": "dm", "name": ""}
        try:
            info = await self.comm.get_conversation(conversation_id)
            info_out["type"] = str(info.get("type", "dm")).lower() or "dm"
            info_out["name"] = str(info.get("name") or "")
        except Exception as exc:  # noqa: BLE001 — assume dm on failure
            self._log.warn("get_conversation failed, assuming dm:", exc)
        self._conv_types[conversation_id] = info_out
        while len(self._conv_types) > 512:
            self._conv_types.popitem(last=False)
        return info_out

    async def _conversation_type(self, conversation_id: str) -> str:
        return (await self._conversation_info(conversation_id))["type"]

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

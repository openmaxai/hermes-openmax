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
    ChannelLivenessReporter,
    MetricsReporter,
    OnlineReporter,
    RuntimeStateProvider,
)
from .services import (
    AsService,
    CommService,
    ConnService,
    CoreService,
    KbService,
    TmService,
)
from .token import TokenManager
from .types import InboundMessage, SendReceipt
from .ws import CwsWsClient

_SYNC_SEQ_KEY = "sync_seq.json"
_DEDUP_KEY = "dedup.json"
_POLICY_KEY = "policy.json"
_DEDUP_MAX = 2048
_DEDUP_PERSIST_EVERY = 25
_GROUP_HISTORY_LEN = 10
_GROUP_CONTEXT_N = 5

DM_REJECT_NOTICE = (
    "你好,我暂时无法处理这条私信(访问策略限制)。请联系我的 owner 开通权限。"
)

SMART_MODE_HINT = (
    "<smart-mode>You were not mentioned. Decide whether to respond. Do NOT "
    "reply if: the message is unrelated to you, just casual chat, or doesn't "
    "need your input. Only reply when: 1) someone asks a question you can "
    "help with, 2) discussing technical topics you know well, 3) someone "
    "clearly needs assistance. When uncertain, prefer NOT to reply. Reply "
    "with exactly [SKIP] to stay silent.</smart-mode>"
)


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
        self._liveness = ChannelLivenessReporter(
            self._http,
            lambda: self._cfg.member_id,
            self._channel_health,
            logger=self._log,
        )
        self._billing: Optional[BillingGate] = (
            BillingGate(self._http, logger=self._log) if billing_gate_enabled else None
        )
        self._metrics_interval_s = metrics_interval_s
        self._metrics_task: Optional[asyncio.Task] = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._on_config_event = on_config_event
        self._ack_reaction = ack_reaction
        self._ack_ttl_s = ack_reaction_ttl_s
        self._pending_acks: dict[
            str, str
        ] = {}  # conv_id -> message_id with our ack on it
        self._ws = CwsWsClient(
            cfg,
            ticket_provider=self._tokens.get_ws_ticket,
            on_frame=self._handle_frame,
            on_reconnected=self._sync_missed,
            on_auth_reset=self._tokens.invalidate,
            logger=self._log,
        )
        self._policy = policy or AccessPolicyConfig()
        self._group_mode_overrides: dict[str, str] = {}  # conv_id -> raw mode
        self.owner_member_id: str = ""
        self._load_policy_state()
        self._seen: "OrderedDict[str, None]" = OrderedDict(
            (k, None) for k in (storage.read_json(_DEDUP_KEY) or [])[-_DEDUP_MAX:]
        )
        self._marks_since_persist = 0
        self._inflight: set[str] = set()
        self._own_client_msg_ids: "OrderedDict[str, None]" = OrderedDict()
        self._group_history: dict[str, list[str]] = {}  # conv_id -> recent "name: text"
        self._last_reject_notice: dict[str, float] = {}
        self._conv_types: "OrderedDict[str, str]" = (
            OrderedDict()
        )  # conv_id -> dm|group|...
        self._member_names: "OrderedDict[str, str]" = (
            OrderedDict()
        )  # member_id -> display_name
        self._participants: "OrderedDict[str, set]" = (
            OrderedDict()
        )  # conv_id -> display names seen
        self._sync_seq: int = int(
            (storage.read_json(_SYNC_SEQ_KEY) or {}).get("seq", 0)
        )
        self._sync_lock = asyncio.Lock()
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        # Fail fast on bad credentials before going async.
        await self._tokens.get_access_token()
        await self._resolve_identity()
        self._running = True
        if self._cfg.member_id:
            await self._online.report(self._cfg.member_id)
            self._metrics_task = asyncio.create_task(
                self._metrics_loop(), name="cws-metrics"
            )
        self._ws.start()
        # First install seeks to the inbox end; later starts replay from the
        # persisted cursor. Both run off the connect path.
        self._spawn_bg(self._initialize_or_sync(), "cws-initial-sync")

    def _spawn_bg(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def stop(self) -> None:
        self._running = False
        self._storage.write_json(_DEDUP_KEY, list(self._seen.keys()))
        if self._metrics_task:
            self._metrics_task.cancel()
            self._metrics_task = None
        for task in list(self._bg_tasks):
            task.cancel()
        await self._ws.stop()
        await self._http.aclose()
        await self._tokens.aclose()

    async def _resolve_identity(self) -> None:
        """Fill member_id from /me when unset; pull authoritative owner."""
        try:
            me = await self.core.me()
            if not self._cfg.member_id:
                self._cfg.member_id = str(me.get("member_id") or "")
                if not self._cfg.org_id:
                    self._cfg.org_id = str(me.get("org_id") or "")
            self._policy.self_display_name = str(me.get("display_name") or "")
            if self._cfg.member_id:
                member = await self.core.get_member(self._cfg.member_id)
                platform_owner = str(member.get("owner_member_id") or "")
                if platform_owner:
                    # Platform data is authoritative; keep a first-DM-bound
                    # owner only while the platform has none.
                    self.owner_member_id = platform_owner
            await self._report_policy()
        except CwsApiError as exc:
            self._log.warn("identity/owner resolve failed (non-fatal):", exc)

    async def _report_policy(self) -> None:
        if not self._cfg.member_id:
            return
        groups = [
            {
                "conversation_id": conversation_id,
                "mode": str(config.get("mode") or "mention"),
                "allow_from": [str(v) for v in config.get("allow_from") or ["*"]],
            }
            for conversation_id, config in self._policy.group_configs.items()
        ]
        payload = {
            "dm_policy": self._policy.dm_policy,
            "dm_allowlist": list(self._policy.dm_allowlist),
            "group_scope": self._policy.group_policy,
            "group_allowlist": [group["conversation_id"] for group in groups],
            "groups": groups,
        }
        try:
            await self._http.request(
                "PUT",
                f"/api/v1/agents/{self._cfg.member_id}/reported-policy",
                json=payload,
            )
        except Exception as exc:  # noqa: BLE001 — reporting never breaks policy enforcement
            if isinstance(exc, CwsApiError) and exc.status == 404:
                return
            self._log.warn("reported-policy update failed (non-fatal):", exc)

    async def _metrics_loop(self) -> None:
        while self._running:
            try:
                await self._metrics.report_once()
                await self._liveness.report_once()
            except Exception as exc:  # noqa: BLE001 — reporting never breaks the loop
                self._log.warn("metrics tick failed:", exc)
            await asyncio.sleep(self._metrics_interval_s)

    def _channel_health(self) -> Optional[bool]:
        if not self._running:
            return None
        return self._ws.is_open()

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
            self._canonicalize_mentions(conversation_id, content),
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
                        prep["upload_url"],
                        content=fh.read(),
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
        """System frames: agent.config.* hot updates, recall/edit events."""
        from .codec import classify_system_event

        event = str(frame.payload.get("event", ""))
        data = frame.payload.get("data") or {}
        kind = classify_system_event(event)
        if kind in ("recall", "edit"):
            await self._deliver_lifecycle_notice(kind, event, frame.payload, data)
            return
        if not event.startswith("agent.config."):
            return
        target = str(data.get("agent_member_id") or "")
        if target and target != self._cfg.member_id:
            self._log.log("config event not for this agent:", target)
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
            elif action == "set":
                self._policy.dm_allowlist = ids
            self._save_policy_state()
        elif event == "agent.config.dm_policy_changed":
            policy = str(data.get("policy", "")).lower()
            if policy in ("open", "allowlist", "owner"):
                self._policy.dm_policy = policy
                self._save_policy_state()
        elif event == "agent.config.group_mode_changed":
            conv = str(data.get("conversation_id", ""))
            if conv:
                mode = str(data.get("mode", ""))
                self._group_mode_overrides[conv] = mode
                if mode == "silent":
                    self._policy.group_configs.pop(conv, None)
                else:
                    group = self._policy.group_configs.setdefault(
                        conv, {"mode": "mention", "allow_from": ["*"]}
                    )
                    group["mode"] = mode
                self._save_policy_state()
        elif event == "agent.config.group_scope_changed":
            scope = str(data.get("scope", "")).lower()
            if scope in ("open", "allowlist", "disabled"):
                self._policy.group_policy = scope
                self._save_policy_state()
        elif event == "agent.config.group_allowlist_changed":
            self._update_group_allowlist(
                str(data.get("action", "")).lower(),
                [str(v) for v in data.get("conversation_ids") or []],
            )
        elif event == "agent.config.group_allowfrom_changed":
            conv = str(data.get("conversation_id") or "")
            if conv and isinstance(data.get("allow_from"), list):
                group = self._policy.group_configs.setdefault(
                    conv, {"mode": "mention", "allow_from": ["*"]}
                )
                group["allow_from"] = [str(v) for v in data["allow_from"]]
                self._save_policy_state()
        elif event == "agent.config.owner_changed":
            self.owner_member_id = str(data.get("new_owner_member_id", ""))
            self._save_policy_state()
        # Other events (dm_policy / group_scope / group_allowlist / allowfrom)
        # are forwarded to the adapter callback; interpretation is host policy.
        await self._report_policy()
        if self._on_config_event:
            try:
                await self._on_config_event(event, data)
            except Exception as exc:  # noqa: BLE001 — host callback must not kill WS
                self._log.warn("on_config_event error:", exc)

    async def _deliver_lifecycle_notice(
        self, kind: str, event: str, payload: dict, data: dict
    ) -> None:
        conversation_id = str(
            payload.get("conversation_id") or data.get("conversation_id") or ""
        )
        if not conversation_id:
            return
        message_id = (
            data.get("message_id") or data.get("id") or data.get("msg_id") or ""
        )
        key = f"sys:{kind}:{conversation_id}:{message_id or event}"
        if key in self._seen:
            return
        actor_id = str(
            data.get("recalled_by")
            or data.get("edited_by")
            or data.get("sender_id")
            or ""
        )
        sender_type = str(data.get("sender_type") or "human").lower()
        msg = InboundMessage(
            message_id=key,
            conversation_id=conversation_id,
            org_id=self._cfg.org_id,
            text="",
            sender_id=actor_id,
            sender_type=sender_type,
            raw={"event": event, "data": data},
        )
        info = await self._conversation_info(conversation_id)
        msg.conversation_type = info["type"]
        from copy import deepcopy
        from dataclasses import replace

        effective = self._effective_policy(conversation_id)
        lifecycle_policy = replace(
            effective, group_configs=deepcopy(effective.group_configs)
        )
        if msg.conversation_type == "group":
            # The original message already passed access control. Re-check
            # group scope and allowFrom, but do not require this synthetic
            # event to repeat the original mention payload.
            lifecycle_policy.group_require_mention = False
            group = lifecycle_policy.group_configs.get(conversation_id)
            if group:
                group["mode"] = "smart"
        decision = decide_inbound(
            msg,
            self_member_id=self._cfg.member_id,
            cfg=lifecycle_policy,
            owner_member_id=self.owner_member_id,
            sender_owner_member_id=await self._sender_owner(msg),
        )
        if not decision.handle:
            self._mark_seen(key)
            return
        if kind == "edit":
            latest = ""
            if message_id:
                try:
                    detail = await self.comm.get_message(conversation_id, message_id)
                    source = detail.get("message") or detail
                    latest = self._extract_text(
                        source, detail.get("content") or source.get("content") or {}
                    )
                except Exception:  # noqa: BLE001
                    pass
            latest = latest or str(data.get("new_content") or data.get("text") or "")
            msg.text = (
                f"[Message Edited] {latest}"
                if latest
                else "[Message Edited] A message was edited. Use the latest content."
            )
        else:
            msg.text = "[Message Recalled] A message was recalled. Do not act on it."
        await self._on_message(msg)
        self._mark_seen(key)

    def _update_group_allowlist(self, action: str, conversation_ids: list[str]) -> None:
        groups = self._policy.group_configs
        if action == "add":
            for conv in conversation_ids:
                groups.setdefault(conv, {"mode": "mention", "allow_from": ["*"]})
        elif action == "remove":
            for conv in conversation_ids:
                groups.pop(conv, None)
        elif action == "set":
            old = dict(groups)
            groups.clear()
            for conv in conversation_ids:
                groups[conv] = old.get(conv, {"mode": "mention", "allow_from": ["*"]})
        else:
            return
        self._save_policy_state()

    async def _sender_owner(self, msg: InboundMessage) -> str:
        if msg.sender_type != "agent" or not msg.sender_id:
            return ""
        try:
            member = await self.core.get_member(msg.sender_id)
            return str(member.get("owner_member_id") or "")
        except Exception:  # noqa: BLE001
            return ""

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
            if msg.sender_name:
                self._record_participant(conversation_id, msg.sender_name)
            is_group = msg.conversation_type not in ("dm",)
            if is_group:
                # zylos group-history parity: record EVERY group message
                # (handled or not) so later turns get conversation context.
                self._record_group_history(conversation_id, msg)
            mode = self._group_mode_overrides.get(conversation_id, "").lower()
            if is_group and mode == "silent":
                self._mark_seen(key)
                await self._advance(conversation_id, msg.seq or seq)
                return
            # zylos parity: dm_policy=owner with no owner bound — the first
            # human DM sender becomes the owner (persisted; platform data
            # overrides on next identity resolve if it disagrees).
            if (
                not is_group
                and msg.sender_type == "human"
                and not self.owner_member_id
                and (self._policy.dm_policy or "").lower() == "owner"
                and msg.sender_id
            ):
                self.owner_member_id = msg.sender_id
                self._save_policy_state()
                self._log.log("owner auto-bound to first DM sender:", msg.sender_id)
            await self._hydrate_media(msg)
            await self._expand_work_references(msg)
            await self._hydrate_reply_context(msg)
            decision = decide_inbound(
                msg,
                self_member_id=self._cfg.member_id,
                cfg=self._effective_policy(conversation_id),
                owner_member_id=self.owner_member_id,
                sender_owner_member_id=await self._sender_owner(msg),
            )
            if decision.handle and is_group:
                history = self._group_history.get(conversation_id, [])
                recent = [h for h in history[:-1]][-_GROUP_CONTEXT_N:]
                if recent:
                    msg.metadata["group_context"] = (
                        "<group-context>\n" + "\n".join(recent) + "\n</group-context>"
                    )
                from .access_policy import _is_mentioned

                if mode == "smart" and not _is_mentioned(msg, self._cfg.member_id):
                    msg.metadata["smart_mode_hint"] = SMART_MODE_HINT
            if (
                decision.handle
                and self._billing is not None
                and await self._billing.is_suspended()
            ):
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
                self._log.log(
                    f"policy skip [{decision.reason}] conv={conversation_id} msg={message_id}"
                )
                await self._maybe_send_reject_notice(
                    conversation_id, msg, decision.reason
                )
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
        self._spawn_bg(self._expire_ack(conversation_id, message_id), "cws-ack-expire")

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
        # zylos system-message.js parity: system events carry priority under
        # metadata.systemEvent.priority as urgent/high/normal.
        sys_event = extra_meta.get("systemEvent") or {}
        sys_prio = (
            str(sys_event.get("priority", "")).lower()
            if isinstance(sys_event, dict)
            else ""
        )
        if sys_prio in ("urgent", "high", "normal"):
            extra_meta["cws_priority"] = {"urgent": 1, "high": 2, "normal": 3}[sys_prio]
        elif priority is not None:
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

    async def _download_attachments(self, message_dict: dict) -> list[dict]:
        """Resolve+download a message's attachments; returns media entries.
        Best-effort — failures return entries without a local path."""
        attachments = (message_dict or {}).get("attachments") or []
        uris = []
        for att in attachments:
            if isinstance(att, str):
                uris.append(att)
            elif isinstance(att, dict):
                uri = (
                    att.get("uri")
                    or att.get("artifact_uri")
                    or att.get("url")
                    or (
                        f"artifact://{att['artifact_id']}"
                        if att.get("artifact_id")
                        else ""
                    )
                )
                if isinstance(uri, str) and uri:
                    uris.append(uri)
        if not uris:
            return []
        try:
            resolved = await self.artifacts.resolve_uris(uris[:10])
        except Exception as exc:  # noqa: BLE001 — media is best-effort
            self._log.warn("attachment resolve failed:", exc)
            return []
        entries: list[dict] = []
        for uri, info in (resolved.get("resolved") or {}).items():
            url = info.get("download_url")
            entry = {
                "uri": uri,
                "type": info.get("content_type", ""),
                "name": info.get("name", ""),
            }
            if url:
                try:
                    fname = f"media-{abs(hash(uri)) % 10**10}-{(info.get('name') or 'file')[-60:]}"
                    local_path = await self.artifacts.download(
                        url, fname, storage=self._storage
                    )
                    if local_path:
                        entry["path"] = local_path
                except Exception as exc:  # noqa: BLE001 — download is best-effort
                    self._log.warn("attachment download failed:", exc)
            entries.append(entry)
        return entries

    async def _hydrate_media(self, msg: InboundMessage) -> None:
        raw = msg.raw if isinstance(msg.raw, dict) else {}
        message = raw.get("message") or raw
        content = raw.get("content") or message.get("content") or {}
        msg.media.extend(await self._download_attachments(content or message))

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
                    labels = [
                        f"{r.get('kind')}:{r.get('label')}({r.get('status', '')})"
                        for r in rows
                    ]
                    blocks.append(
                        f"[proj://{ref_id}] issues: {', '.join(labels) or '(none)'}"
                    )
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
            detail = await self.comm.get_message(
                msg.conversation_id, msg.reply_to_message_id
            )
            parent = detail.get("message") or detail
            content = detail.get("content") or parent.get("content") or {}
            text = self._extract_text(parent, content)
            author_id = str(parent.get("sender_id", ""))
            msg.metadata["reply_to_text"] = text[:2000]
            msg.metadata["reply_to_author_id"] = author_id
            if author_id:
                msg.metadata["reply_to_author_name"] = await self._member_name(
                    author_id
                )
            # zylos parity: quoted media is downloaded too, so the agent can
            # actually see the image being replied to.
            quoted_media = await self._download_attachments(parent)
            if quoted_media:
                msg.media.extend(quoted_media)
                if not text:
                    labels = ", ".join(
                        f"[{'image' if 'image' in (e.get('type') or '') else 'file'}: {e.get('name', '')}]"
                        for e in quoted_media
                    )
                    msg.metadata["reply_to_text"] = labels
        except Exception as exc:  # noqa: BLE001 — quote context is best-effort
            self._log.warn("reply-context hydrate failed:", exc)

    def _effective_policy(self, conversation_id: str) -> AccessPolicyConfig:
        """Apply a per-conversation group-mode override on top of the base policy."""
        mode = self._group_mode_overrides.get(conversation_id, "")
        if not mode:
            return self._policy
        from dataclasses import replace

        low = mode.lower()
        if "smart" in low:
            # smart: receive everything, model decides ([SKIP] to stay silent).
            return replace(self._policy, group_require_mention=False)
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

    async def _initialize_or_sync(self) -> None:
        async with self._sync_lock:
            await self._initialize_or_sync_unlocked()

    async def _initialize_or_sync_unlocked(self) -> None:
        if self._sync_seq > 0:
            await self._sync_missed_unlocked()
            return
        cursor = 0
        while True:
            try:
                res = await self.comm.sync(cursor, self._cfg.device_id)
            except CwsApiError as exc:
                self._log.warn("initial /sync seek failed:", exc)
                return
            events = res.get("events") or []
            next_cursor = res.get("next_cursor")
            if next_cursor is None and events:
                next_cursor = events[-1].get("seq")
            try:
                cursor = int(next_cursor or cursor)
            except (TypeError, ValueError):
                return
            if not res.get("has_more"):
                if cursor > 0:
                    self._sync_seq = cursor
                    self._storage.write_json(_SYNC_SEQ_KEY, {"seq": cursor})
                    try:
                        await self.comm.sync_ack(self._cfg.device_id, cursor)
                    except CwsApiError:
                        pass
                return

    async def _sync_missed(self) -> None:
        async with self._sync_lock:
            await self._sync_missed_unlocked()

    async def _sync_missed_unlocked(self) -> None:
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

    def _record_group_history(self, conversation_id: str, msg: InboundMessage) -> None:
        hist = self._group_history.setdefault(conversation_id, [])
        label = msg.sender_name or msg.sender_id[:8]
        text = msg.text or "[media]"
        hist.append(f"{label}: {text[:300]}")
        del hist[:-_GROUP_HISTORY_LEN]

    async def _maybe_send_reject_notice(
        self, conversation_id: str, msg: InboundMessage, reason: str
    ) -> None:
        """zylos parity: polite rejection for human DMs blocked by policy —
        throttled, never for agents/system, never for group-no-mention."""
        if reason not in ("dm_owner_only", "dm_not_allowlisted"):
            return
        if msg.sender_type != "human":
            return
        import time as _time

        now = _time.time()
        if now - self._last_reject_notice.get(conversation_id, 0) < 3600:
            return
        self._last_reject_notice[conversation_id] = now
        try:
            await self.comm.send_message(conversation_id, DM_REJECT_NOTICE)
        except Exception as exc:  # noqa: BLE001 — notice is best-effort
            self._log.warn("reject notice failed:", exc)

    # -- policy state persistence (zylos config.json parity) --------------------

    def _load_policy_state(self) -> None:
        saved = self._storage.read_json(_POLICY_KEY)
        if not isinstance(saved, dict):
            return
        if saved.get("dm_policy"):
            self._policy.dm_policy = str(saved["dm_policy"])
        if saved.get("owner_member_id"):
            self.owner_member_id = str(saved["owner_member_id"])
        if isinstance(saved.get("dm_allowlist"), list):
            self._policy.dm_allowlist = [str(i) for i in saved["dm_allowlist"]]
        if isinstance(saved.get("group_modes"), dict):
            self._group_mode_overrides.update(
                {str(k): str(v) for k, v in saved["group_modes"].items()}
            )
        if saved.get("group_policy"):
            self._policy.group_policy = str(saved["group_policy"])
        if isinstance(saved.get("group_configs"), dict):
            self._policy.group_configs = dict(saved["group_configs"])

    def _save_policy_state(self) -> None:
        self._storage.write_json(
            _POLICY_KEY,
            {
                "dm_policy": self._policy.dm_policy,
                "dm_allowlist": self._policy.dm_allowlist,
                "group_modes": self._group_mode_overrides,
                "group_policy": self._policy.group_policy,
                "group_configs": self._policy.group_configs,
                "owner_member_id": self.owner_member_id,
            },
        )

    # -- outbound mention canonicalization (zylos mention.js parity) -----------

    def _record_participant(self, conversation_id: str, name: str) -> None:
        names = self._participants.setdefault(conversation_id, set())
        names.add(name)
        while len(self._participants) > 512:
            self._participants.popitem(last=False)

    def _canonicalize_mentions(self, conversation_id: str, text: str) -> str:
        """Rewrite @name to the participant's exact display_name (longest-first,
        case-insensitive) so the FE's participant-name matcher highlights it."""
        if not text or "@" not in text:
            return text
        import re

        names = sorted(
            self._participants.get(conversation_id, ()), key=len, reverse=True
        )
        for name in names:
            text = re.sub("@" + re.escape(name), "@" + name, text, flags=re.IGNORECASE)
        return text

    # -- small state helpers ---------------------------------------------------

    def _mark_seen(self, key: str) -> None:
        self._seen[key] = None
        while len(self._seen) > _DEDUP_MAX:
            self._seen.popitem(last=False)
        # zylos parity: persist dedup across restarts so the /sync window
        # boundary can't double-deliver after a crash.
        self._marks_since_persist += 1
        if self._marks_since_persist >= _DEDUP_PERSIST_EVERY:
            self._marks_since_persist = 0
            self._storage.write_json(_DEDUP_KEY, list(self._seen.keys()))

    def _remember_own(self, client_msg_id: str) -> None:
        self._own_client_msg_ids[client_msg_id] = None
        while len(self._own_client_msg_ids) > _DEDUP_MAX:
            self._own_client_msg_ids.popitem(last=False)

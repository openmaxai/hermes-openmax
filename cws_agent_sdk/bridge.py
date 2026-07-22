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
import hashlib
import time
from collections import OrderedDict, deque
from typing import Awaitable, Callable, Optional

from .access_policy import AccessPolicyConfig, _is_mentioned, decide_inbound
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
_AGENT_TURN_KEYS_MAX = 2048

DM_REJECT_NOTICE = (
    "你好,我暂时无法处理这条私信(访问策略限制)。请联系我的 owner 开通权限。"
)
GROUP_REJECT_NOTICES = {
    "group_disabled": "Sorry, group chat is currently disabled.",
    "group_not_allowlisted": "Sorry, this group is not enabled for this agent.",
    "group_sender_not_allowed": "Sorry, you are not allowed to trigger this agent in this group.",
}

_VALID_DM_POLICIES = {"open", "allowlist", "owner"}
_VALID_GROUP_POLICIES = {"open", "allowlist", "disabled"}
_VALID_GROUP_MODES = {"mention", "smart", "silent"}
_VALID_LIST_ACTIONS = {"add", "remove", "set"}

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
        billing_gate_enabled: bool = False,
        metrics_interval_s: float = 300.0,
        control_sync_interval_s: float = 300.0,
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
        self._control_sync_interval_s = control_sync_interval_s
        self._metrics_task: Optional[asyncio.Task] = None
        self._control_sync_task: Optional[asyncio.Task] = None
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
            on_reconnected=self._on_reconnected,
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
        self._inflight_done: dict[str, asyncio.Event] = {}
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
        self._agent_turns: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._agent_fingerprints: "OrderedDict[str, float]" = OrderedDict()
        self._agent_loop_admitted: "OrderedDict[str, None]" = OrderedDict()
        self._sync_seq: int = int(
            (storage.read_json(_SYNC_SEQ_KEY) or {}).get("seq", 0)
        )
        self._sync_lock = asyncio.Lock()
        self._owner_sync_lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        # Fail fast on bad credentials before going async.
        self._loop = asyncio.get_running_loop()
        await self._tokens.get_access_token()
        await self._resolve_identity()
        self._running = True
        self._ws.start()
        await self._ws.wait_until_connected()
        if self._cfg.member_id:
            # This is a boot/onboarding report, not proof of a model turn. Do
            # not emit it until the transport has completed a real handshake.
            self._spawn_bg(
                self._online.report(self._cfg.member_id), "cws-online-report"
            )
            self._metrics_task = asyncio.create_task(
                self._metrics_loop(), name="cws-metrics"
            )
            self._control_sync_task = asyncio.create_task(
                self._control_sync_loop(), name="cws-control-sync"
            )
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
        if self._control_sync_task:
            self._control_sync_task.cancel()
            self._control_sync_task = None
        for task in list(self._bg_tasks):
            task.cancel()
        await self._ws.stop()
        await self._http.aclose()
        await self._tokens.aclose()
        self._loop = None

    async def _resolve_identity(self) -> None:
        """Fill member_id from /me when unset; pull authoritative owner."""
        try:
            me = await self.core.me()
            if not self._cfg.member_id:
                self._cfg.member_id = str(me.get("member_id") or "")
                if not self._cfg.org_id:
                    self._cfg.org_id = str(me.get("org_id") or "")
            self._policy.self_display_name = str(me.get("display_name") or "")
            await self._sync_owner_from_core()
            await self._report_policy()
        except CwsApiError as exc:
            self._log.warn("identity/owner resolve failed (non-fatal):", exc)

    async def _sync_owner_from_core(self, *, notify: bool = False) -> bool:
        """Reconcile the local owner cache from Core's authenticated member view."""
        member_id = self._cfg.member_id
        if not member_id:
            return False

        change: Optional[dict] = None
        async with self._owner_sync_lock:
            try:
                member = await self.core.get_member(member_id)
            except Exception as exc:  # noqa: BLE001 — control sync is best-effort
                self._log.warn("owner sync failed; keeping local owner:", exc)
                return False

            platform_owner = str(member.get("owner_member_id") or "")
            # Preserve first-DM fallback when Core has no authoritative owner.
            if not platform_owner or platform_owner == self.owner_member_id:
                return False

            previous_owner = self.owner_member_id
            self.owner_member_id = platform_owner
            self._save_policy_state()
            self._log.log(
                "owner synced from Core:",
                previous_owner or "(none)",
                "->",
                platform_owner,
            )
            change = {
                "agent_member_id": member_id,
                "old_owner_member_id": previous_owner,
                "new_owner_member_id": platform_owner,
                "source": "core",
            }

        if notify and change:
            await self._notify_config_event("agent.config.owner_changed", change)
        return change is not None

    async def _report_policy(self) -> None:
        if not self._cfg.member_id:
            return
        groups = [
            {
                "conversation_id": conversation_id,
                "mode": str(config.get("mode") or "mention"),
                "allow_from": [
                    str(v)
                    for v in (
                        config.get("allow_from")
                        if "allow_from" in config
                        else config.get("allowFrom")
                    )
                    or ["*"]
                ],
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
            except Exception as exc:  # noqa: BLE001 — reporting never breaks the loop
                self._log.warn("metrics tick failed:", exc)
            await asyncio.sleep(self._metrics_interval_s)

    async def _control_sync_loop(self) -> None:
        """Heal missed config events on the same five-minute cadence as Zylos."""
        while self._running:
            await asyncio.sleep(self._control_sync_interval_s)
            if not self._running:
                return
            await self._sync_owner_from_core(notify=True)
            await self._report_policy()

    async def _on_reconnected(self) -> None:
        # Refresh control-plane state before replaying missed user messages.
        await self._sync_owner_from_core(notify=True)
        await self._report_policy()
        await self._sync_missed()

    async def _notify_config_event(self, event: str, data: dict) -> None:
        if not self._on_config_event:
            return
        try:
            await self._on_config_event(event, data)
        except Exception as exc:  # noqa: BLE001 — host callback must not kill WS
            self._log.warn("on_config_event error:", exc)

    def is_running(self) -> bool:
        return self._running and self._ws.is_open()

    # -- outbound ------------------------------------------------------------

    def _outbound_agent_metadata(self, metadata: Optional[dict], cmid: str) -> dict:
        result = dict(metadata or {})
        try:
            prior_hop = int(result.get("agent_hop_count") or 0)
        except (TypeError, ValueError):
            prior_hop = 0
        result["agent_hop_count"] = prior_hop + 1
        result.setdefault("agent_origin_member_id", self._cfg.member_id)
        result.setdefault("agent_trace_id", cmid)
        return result

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
        outbound_metadata = self._outbound_agent_metadata(metadata, cmid)
        receipt = await self.comm.send_message(
            conversation_id,
            self._canonicalize_mentions(conversation_id, content),
            reply_to=reply_to,
            metadata=outbound_metadata,
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
        metadata: Optional[dict] = None,
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
        from .codec import new_client_msg_id

        cmid = new_client_msg_id()
        self._remember_own(cmid)
        receipt = await self.comm.send_image_message(
            conversation_id,
            artifact_id=str(fin.get("artifact_id", "")),
            file_name=fname,
            content_type=ctype,
            size_bytes=size,
            caption=caption,
            reply_to=reply_to,
            client_msg_id=cmid,
            metadata=self._outbound_agent_metadata(metadata, cmid),
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
            async with self._sync_lock:
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
            raw_ids = data.get("member_ids")
            if action not in _VALID_LIST_ACTIONS or not isinstance(raw_ids, list):
                self._log.warn("invalid dm allowlist event:", action)
                return
            ids = [str(i) for i in raw_ids if str(i)]
            if action == "add":
                for i in ids:
                    if i not in self._policy.dm_allowlist:
                        self._policy.dm_allowlist.append(i)
            elif action == "remove":
                self._policy.dm_allowlist = [
                    i for i in self._policy.dm_allowlist if i not in ids
                ]
            else:  # set
                self._policy.dm_allowlist = ids
            self._save_policy_state()
        elif event == "agent.config.dm_policy_changed":
            policy = str(data.get("policy", "")).lower()
            if policy not in _VALID_DM_POLICIES:
                self._log.warn("invalid dm policy event:", policy)
                return
            self._policy.dm_policy = policy
            self._save_policy_state()
        elif event == "agent.config.group_mode_changed":
            conv = str(data.get("conversation_id", ""))
            mode = str(data.get("mode", "")).lower()
            if not conv or mode not in _VALID_GROUP_MODES:
                self._log.warn("invalid group mode event:", conv, mode)
                return
            self._group_mode_overrides[conv] = mode
            group = self._policy.group_configs.setdefault(
                conv, {"mode": "mention", "allow_from": ["*"]}
            )
            group["mode"] = mode
            self._save_policy_state()
        elif event == "agent.config.group_scope_changed":
            scope = str(data.get("scope", "")).lower()
            if scope not in _VALID_GROUP_POLICIES:
                self._log.warn("invalid group scope event:", scope)
                return
            self._policy.group_policy = scope
            self._save_policy_state()
        elif event == "agent.config.group_allowlist_changed":
            action = str(data.get("action", "")).lower()
            raw_ids = data.get("conversation_ids")
            if action not in _VALID_LIST_ACTIONS or not isinstance(raw_ids, list):
                self._log.warn("invalid group allowlist event:", action)
                return
            self._update_group_allowlist(
                action,
                [str(v) for v in raw_ids if str(v)],
            )
        elif event == "agent.config.group_allowfrom_changed":
            conv = str(data.get("conversation_id") or "")
            raw_allow = data.get("allow_from")
            if not conv or not isinstance(raw_allow, list):
                self._log.warn("invalid group allowFrom event:", conv)
                return
            group = self._policy.group_configs.setdefault(
                conv, {"mode": "mention", "allow_from": ["*"]}
            )
            group["allow_from"] = [str(v) for v in raw_allow if str(v)]
            self._save_policy_state()
        elif event == "agent.config.owner_changed":
            # The pushed owner is only a refresh hint. Core's authenticated
            # member record remains authoritative.
            await self._sync_owner_from_core(notify=True)
            return
        else:
            return
        # Other events (dm_policy / group_scope / group_allowlist / allowfrom)
        # are forwarded to the adapter callback; interpretation is host policy.
        await self._report_policy()
        await self._notify_config_event(event, data)

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
                self._group_mode_overrides.pop(conv, None)
        elif action == "set":
            old = dict(groups)
            old_modes = dict(self._group_mode_overrides)
            groups.clear()
            self._group_mode_overrides.clear()
            for conv in conversation_ids:
                groups[conv] = old.get(conv, {"mode": "mention", "allow_from": ["*"]})
                if conv in old_modes:
                    self._group_mode_overrides[conv] = old_modes[conv]
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

    def _agent_loop_rejection(self, msg: InboundMessage) -> str:
        """Local fail-closed circuit breakers after Agent policy admission."""
        if msg.sender_type != "agent":
            return ""
        message_key = f"{msg.conversation_id}:{msg.message_id}"
        if message_key in self._agent_loop_admitted:
            return ""  # delivery retry for the same un-acked message
        if "agent_hop_count" not in msg.metadata:
            hop = 1
        else:
            raw_hop = msg.metadata["agent_hop_count"]
            if isinstance(raw_hop, bool):
                return "agent_hop_invalid"
            if isinstance(raw_hop, int):
                hop = raw_hop
            elif isinstance(raw_hop, str):
                try:
                    hop = int(raw_hop.strip())
                except (TypeError, ValueError):
                    return "agent_hop_invalid"
            else:
                return "agent_hop_invalid"
        if hop < 1 or hop > self._policy.max_agent_hops:
            return "agent_hop_limit"

        now = time.monotonic()
        fingerprint_input = "\0".join(
            (
                msg.conversation_id,
                msg.sender_id,
                msg.reply_to_message_id or "",
                " ".join((msg.text or "").split()).casefold(),
            )
        )
        fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
        duplicate_window = self._policy.agent_duplicate_window_s
        previous = self._agent_fingerprints.get(fingerprint)
        if previous is not None and now - previous < duplicate_window:
            return "agent_duplicate"

        turn_key = f"{msg.conversation_id}:{msg.sender_id}"
        cutoff = now - self._policy.agent_turn_window_s
        while self._agent_turns:
            oldest_key, oldest_turns = next(iter(self._agent_turns.items()))
            while oldest_turns and oldest_turns[0] <= cutoff:
                oldest_turns.popleft()
            if oldest_turns and len(self._agent_turns) <= _AGENT_TURN_KEYS_MAX:
                break
            self._agent_turns.pop(oldest_key, None)
        turns = self._agent_turns.get(turn_key)
        if turns is None:
            while len(self._agent_turns) >= _AGENT_TURN_KEYS_MAX:
                self._agent_turns.popitem(last=False)
            turns = deque()
            self._agent_turns[turn_key] = turns
        else:
            self._agent_turns.move_to_end(turn_key)
        while turns and turns[0] <= cutoff:
            turns.popleft()
        if len(turns) >= self._policy.agent_turn_budget:
            return "agent_turn_budget"

        turns.append(now)
        self._agent_fingerprints[fingerprint] = now
        self._agent_fingerprints.move_to_end(fingerprint)
        while self._agent_fingerprints:
            _, oldest = next(iter(self._agent_fingerprints.items()))
            if len(self._agent_fingerprints) <= 2048 and now - oldest < duplicate_window:
                break
            self._agent_fingerprints.popitem(last=False)
        self._agent_loop_admitted[message_key] = None
        while len(self._agent_loop_admitted) > _DEDUP_MAX:
            self._agent_loop_admitted.popitem(last=False)
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
        if key in self._seen:
            # Realtime delivery never commits the global cursor. Serialized
            # /sync later advances through already-seen messages in server order.
            if frame is None and seq > 0:
                await self._advance(conversation_id, 0, inbox_seq=seq)
            return
        # If /sync catches a realtime attempt still in progress, wait for its
        # outcome. Success becomes an ordered cursor-only commit; failure is
        # retried here and stops this serialized sync batch if it fails again.
        if key in self._inflight:
            if frame is not None:
                return
            done = self._inflight_done[key]
            await done.wait()
            if key in self._seen:
                await self._advance(conversation_id, 0, inbox_seq=seq)
                return
            await self._deliver_by_id(conversation_id, message_id, seq)
            return
        self._inflight.add(key)
        done = asyncio.Event()
        self._inflight_done[key] = done
        try:
            detail = await self.comm.get_message(conversation_id, message_id)
            # /sync carries the org-level inbox watermark in event.seq.  A
            # realtime message frame may carry only a conversation-local seq;
            # use detail.inbox_seq when the server provides it and otherwise do
            # not advance the global cursor from the realtime frame.
            detail_inbox = detail.get("inbox_seq") or (
                (detail.get("message") or {}).get("inbox_seq")
                if isinstance(detail.get("message"), dict)
                else None
            )
            observed_inbox_seq = int(detail_inbox or 0)
            inbox_seq = int(seq or 0) if frame is None else 0
            metadata_inbox_seq = observed_inbox_seq or inbox_seq
            raw_message = detail.get("message") or detail
            conversation_seq = int(raw_message.get("seq") or seq or 0)
            if metadata_inbox_seq > 0:
                detail = dict(detail)
                detail["_inbox_seq"] = metadata_inbox_seq
            msg = self._normalize(detail, conversation_id, seq)
            if msg is None:
                self._mark_seen(key)
                await self._advance(
                    conversation_id, conversation_seq, inbox_seq=inbox_seq
                )
                return
            conv_info = await self._conversation_info(conversation_id)
            msg.conversation_type = conv_info["type"]
            if conv_info.get("name"):
                msg.metadata["conversation_name"] = conv_info["name"]
            is_group = msg.conversation_type not in ("dm",)
            effective_policy = self._effective_policy(conversation_id)
            group_cfg = effective_policy.group_configs.get(conversation_id) or {}
            mode = str(group_cfg.get("mode") or "").lower()
            if not msg.sender_name and msg.sender_id and mode != "silent":
                msg.sender_name = await self._member_name(msg.sender_id)
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
            decision = decide_inbound(
                msg,
                self_member_id=self._cfg.member_id,
                cfg=effective_policy,
                owner_member_id=self.owner_member_id,
                sender_owner_member_id=(
                    await self._sender_owner(msg) if not is_group else ""
                ),
            )
            if decision.handle:
                loop_rejection = self._agent_loop_rejection(msg)
                if loop_rejection:
                    self._log.log(
                        f"policy skip [{loop_rejection}] conv={conversation_id} msg={message_id}"
                    )
                    self._mark_seen(key)
                    await self._advance(
                        conversation_id,
                        msg.seq or conversation_seq,
                        inbox_seq=inbox_seq,
                    )
                    return
            context_eligible = decision.handle or decision.reason in (
                "group_no_mention",
                "group_silent",
            )
            if msg.sender_name and (not is_group or context_eligible):
                self._record_participant(conversation_id, msg.sender_name)
            if is_group and context_eligible:
                # Allowed background traffic is cached in a small bridge-owned
                # history window. Rejected groups/senders never enter context.
                self._record_group_history(conversation_id, msg)
            if not decision.handle:
                self._log.log(
                    f"policy skip [{decision.reason}] conv={conversation_id} msg={message_id}"
                )
                await self._maybe_send_reject_notice(
                    conversation_id,
                    msg,
                    decision.reason,
                    is_sync_replay=frame is None,
                )
                self._mark_seen(key)
                await self._advance(
                    conversation_id,
                    msg.seq or conversation_seq,
                    inbox_seq=inbox_seq,
                )
                return
            if is_group and (mode == "silent" or decision.reason == "group_silent"):
                # Observe at the bridge only: no attachment/work hydration,
                # billing lookup, ack reaction, Hermes session, or model turn.
                self._mark_seen(key)
                await self._advance(
                    conversation_id,
                    msg.seq or conversation_seq,
                    inbox_seq=inbox_seq,
                )
                return
            await self._hydrate_media(msg)
            await self._expand_work_references(msg)
            await self._hydrate_reply_context(msg)
            if decision.handle and is_group:
                history = self._group_history.get(conversation_id, [])
                recent = [h for h in history[:-1]][-_GROUP_CONTEXT_N:]
                if recent:
                    msg.metadata["group_context"] = (
                        "<group-context>\n" + "\n".join(recent) + "\n</group-context>"
                    )
                if mode == "smart" and not _is_mentioned(
                    msg,
                    self._cfg.member_id,
                    (self._policy.self_display_name, *self._policy.self_aliases),
                ):
                    msg.metadata["smart_mode_hint"] = SMART_MODE_HINT
            if (
                decision.handle
                and self._billing is not None
                and await self._billing.is_suspended()
            ):
                self._log.warn("billing suspended — skipping delivery", conversation_id)
                if (
                    frame is not None
                    and self._billing.should_send_overdue_notice(conversation_id)
                ):
                    try:
                        await self.comm.send_message(conversation_id, OVERDUE_NOTICE)
                    except CwsApiError as exc:
                        self._log.warn("overdue notice failed:", exc)
                self._mark_seen(key)
                await self._advance(
                    conversation_id,
                    msg.seq or conversation_seq,
                    inbox_seq=inbox_seq,
                )
                return
            await self._ack_received(conversation_id, str(message_id))
            # Delivery point — exceptions propagate, watermark stays put, /sync replays.
            await self._on_message(msg)
            self._mark_seen(key)
            await self._advance(
                conversation_id,
                msg.seq or conversation_seq,
                inbox_seq=inbox_seq,
            )
        finally:
            self._inflight.discard(key)
            self._inflight_done.pop(key, None)
            done.set()

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
        extra_meta = dict(m.get("metadata") or {})
        if detail.get("_inbox_seq") is not None:
            extra_meta["cws_inbox_seq"] = int(detail["_inbox_seq"])
        mentions = (
            m.get("mentions")
            or m.get("mention_user_ids")
            or extra_meta.get("mentions")
            or []
        )
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
            mentions=[mm for mm in mentions if isinstance(mm, (dict, str))],
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
        if low == "smart":
            # smart: receive everything, model decides ([SKIP] to stay silent).
            return replace(self._policy, group_require_mention=False)
        if low == "mention":
            return replace(self._policy, group_require_mention=True)
        if low == "silent":
            from copy import deepcopy

            groups = deepcopy(self._policy.group_configs)
            groups.setdefault(conversation_id, {"mode": "silent", "allow_from": ["*"]})
            groups[conversation_id]["mode"] = "silent"
            return replace(
                self._policy,
                group_require_mention=False,
                group_configs=groups,
            )
        return self._policy

    async def _conversation_info(self, conversation_id: str) -> dict:
        cached = self._conv_types.get(conversation_id)
        if cached:
            return cached
        try:
            info = await self.comm.get_conversation(conversation_id)
        except Exception as exc:  # noqa: BLE001 — unknown type must fail closed
            self._log.warn("get_conversation failed; delivery remains pending:", exc)
            raise
        conversation_type = str(info.get("type") or "").lower()
        if not conversation_type:
            raise ValueError("conversation response is missing type")
        info_out = {
            "type": conversation_type,
            "name": str(info.get("name") or ""),
        }
        self._conv_types[conversation_id] = info_out
        while len(self._conv_types) > 512:
            self._conv_types.popitem(last=False)
        return info_out

    async def _conversation_type(self, conversation_id: str) -> str:
        return (await self._conversation_info(conversation_id))["type"]

    # -- watermarks ---------------------------------------------------------

    async def _advance(
        self, conversation_id: str, conversation_seq: int, *, inbox_seq: int = 0
    ) -> None:
        if conversation_seq <= 0 and inbox_seq <= 0:
            return
        if conversation_seq > 0:
            try:
                await self.comm.mark_read(conversation_id, conversation_seq)
            except CwsApiError as exc:
                self._log.warn("mark_read failed:", exc)
        if inbox_seq > self._sync_seq:
            self._sync_seq = inbox_seq
            self._storage.write_json(_SYNC_SEQ_KEY, {"seq": inbox_seq})
            try:
                await self._sync_ack(inbox_seq)
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
                        await self._sync_ack(cursor)
                    except CwsApiError:
                        pass
                return

    async def _sync_ack(self, seq: int) -> None:
        """Send alpha.2 ack metadata, tolerating legacy test doubles."""
        try:
            await self.comm.sync_ack(
                self._cfg.device_id, seq, self._cfg.client_version
            )
        except TypeError as exc:
            if "positional argument" not in str(exc):
                raise
            await self.comm.sync_ack(self._cfg.device_id, seq)

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
        self,
        conversation_id: str,
        msg: InboundMessage,
        reason: str,
        *,
        is_sync_replay: bool = False,
    ) -> None:
        """Send a throttled notice only for a live, actionable human rejection."""
        if msg.sender_type != "human" or is_sync_replay:
            return
        notice = ""
        if reason in ("dm_owner_only", "dm_not_allowlisted"):
            notice = DM_REJECT_NOTICE
        elif reason in GROUP_REJECT_NOTICES and _is_mentioned(
            msg,
            self._cfg.member_id,
            (self._policy.self_display_name, *self._policy.self_aliases),
        ):
            notice = GROUP_REJECT_NOTICES[reason]
        if not notice:
            return
        import time as _time

        now = _time.time()
        throttle_key = f"{conversation_id}:{reason}"
        if now - self._last_reject_notice.get(throttle_key, 0) < 3600:
            return
        self._last_reject_notice[throttle_key] = now
        try:
            await self.comm.send_message(conversation_id, notice)
        except Exception as exc:  # noqa: BLE001 — notice is best-effort
            self._log.warn("reject notice failed:", exc)

    # -- policy state persistence (zylos config.json parity) --------------------

    def _load_policy_state(self) -> None:
        saved = self._storage.read_json(_POLICY_KEY)
        if not isinstance(saved, dict):
            return
        dm_policy = str(saved.get("dm_policy") or "").lower()
        if dm_policy in _VALID_DM_POLICIES:
            self._policy.dm_policy = dm_policy
        if saved.get("owner_member_id"):
            self.owner_member_id = str(saved["owner_member_id"])
        if isinstance(saved.get("dm_allowlist"), list):
            self._policy.dm_allowlist = [
                str(i) for i in saved["dm_allowlist"] if str(i)
            ]
        group_policy = str(saved.get("group_policy") or "").lower()
        if group_policy in _VALID_GROUP_POLICIES:
            self._policy.group_policy = group_policy
        groups: dict[str, dict] = {}
        if isinstance(saved.get("group_configs"), dict):
            for raw_conv, raw_config in saved["group_configs"].items():
                conv = str(raw_conv)
                if not conv or not isinstance(raw_config, dict):
                    continue
                mode = str(raw_config.get("mode") or "mention").lower()
                if mode not in _VALID_GROUP_MODES:
                    mode = "mention"
                raw_allow = (
                    raw_config.get("allow_from")
                    if "allow_from" in raw_config
                    else raw_config.get("allowFrom")
                )
                allow_from = (
                    [str(v) for v in raw_allow if str(v)]
                    if isinstance(raw_allow, list)
                    else ["*"]
                )
                groups[conv] = {"mode": mode, "allow_from": allow_from}
        self._policy.group_configs = groups
        if isinstance(saved.get("group_modes"), dict):
            for raw_conv, raw_mode in saved["group_modes"].items():
                conv, mode = str(raw_conv), str(raw_mode).lower()
                if not conv or mode not in _VALID_GROUP_MODES:
                    continue
                self._group_mode_overrides[conv] = mode
                group = self._policy.group_configs.setdefault(
                    conv, {"mode": "mention", "allow_from": ["*"]}
                )
                group["mode"] = mode

    def get_dm_access(self) -> dict:
        return {
            "dm_policy": self._policy.dm_policy,
            "dm_allowlist": list(self._policy.dm_allowlist),
        }

    async def apply_local_dm_access(
        self, dm_policy: str, dm_allowlist: list[str]
    ) -> dict:
        policy = str(dm_policy or "").lower()
        if policy not in _VALID_DM_POLICIES:
            raise ValueError("policy must be one of: open, allowlist, owner")
        self._policy.dm_policy = policy
        self._policy.dm_allowlist = list(
            dict.fromkeys(str(v) for v in dm_allowlist if str(v))
        )
        self._save_policy_state()
        self._spawn_bg(self._report_policy(), "cws-local-policy-report")
        return self.get_dm_access()

    def apply_local_dm_access_threadsafe(
        self, dm_policy: str, dm_allowlist: list[str]
    ) -> dict:
        loop = self._loop
        if not loop or not loop.is_running():
            raise RuntimeError("CWS bridge event loop is not running")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            raise RuntimeError("cannot block the CWS bridge event loop")
        future = asyncio.run_coroutine_threadsafe(
            self.apply_local_dm_access(dm_policy, dm_allowlist), loop
        )
        return future.result(timeout=10)

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

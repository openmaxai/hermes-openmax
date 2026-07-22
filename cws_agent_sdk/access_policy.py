"""Inbound access policy — decide whether a message should trigger the agent.

Port of zylos-openmax's shouldHandleMessage semantics, as a pure function:

- DM from a human: handle (optionally restricted to owner / allowlist).
- DM from a sibling agent (same owner): handle only if sibling_dm allowed —
  default False to prevent agent-to-agent chat loops.
- Group: handle only when the agent is mentioned (@agent / all / all_agents),
  unless group_require_mention is disabled.
- Messages from SYSTEM senders: delivered by default for scheduler/lifecycle
  work and never participate in owner auto-binding.
- Own messages: never handled (also enforced upstream in the bridge).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .types import InboundMessage


@dataclass
class AccessPolicyConfig:
    # DM admission: "open" (any org member), "allowlist" (dm_allowlist +
    # owner), "owner" (bound owner only — zylos's default private model).
    dm_policy: str = "owner"
    group_require_mention: bool = True
    allow_agent_senders: bool = False  # let other agents' messages trigger us
    allow_sibling_dm: bool = False  # same-owner agent DMs
    agent_allowlist: list[str] = field(default_factory=list)
    max_agent_hops: int = 4
    agent_turn_budget: int = 4
    agent_turn_window_s: float = 60.0
    agent_duplicate_window_s: float = 60.0
    dm_allowlist: list[str] = field(
        default_factory=list
    )  # member_ids (dm_policy=allowlist)
    # System Member DMs (scheduler "dependencies ready", issue.activated, ...)
    # DRIVE the task flow — zylos lets them straight through. Default True.
    handle_system: bool = True
    group_policy: str = "allowlist"  # open | allowlist | disabled
    group_configs: dict[str, dict] = field(default_factory=dict)
    self_display_name: str = ""
    self_aliases: list[str] = field(default_factory=list)


@dataclass
class AccessDecision:
    handle: bool
    reason: str


def _text_mentions_name(text: str, name: str) -> bool:
    if not text or not name:
        return False
    # Do not let @Name match @NameSuffix or @Name-team. Python's Unicode-aware
    # \w also keeps the boundary correct for non-ASCII display names.
    return bool(re.search(r"@" + re.escape(name) + r"(?![\w-])", text, re.IGNORECASE))


def _is_mentioned(
    msg: InboundMessage,
    self_member_id: str,
    self_names: Iterable[str] = (),
) -> bool:
    for m in msg.mentions or []:
        if isinstance(m, str) and m == self_member_id:
            return True
        mtype = str(m.get("type", "")).lower() if isinstance(m, dict) else ""
        if mtype in ("all", "all_agents"):
            return True
        if isinstance(m, dict):
            target = (
                m.get("member_id")
                or m.get("entity_id")
                or m.get("mentioned_id")
                or m.get("id")
            )
            if str(target or "") == self_member_id:
                return True
    return any(_text_mentions_name(msg.text or "", name) for name in self_names if name)


def _is_directly_mentioned(msg: InboundMessage, self_member_id: str) -> bool:
    """Structured member mention only; broadcast mentions do not trigger Agents."""
    for mention in msg.mentions or []:
        if isinstance(mention, str) and mention == self_member_id:
            return True
        if not isinstance(mention, dict):
            continue
        target = (
            mention.get("member_id")
            or mention.get("entity_id")
            or mention.get("mentioned_id")
            or mention.get("id")
        )
        if str(target or "") == self_member_id:
            return True
    return False


def decide_inbound(
    msg: InboundMessage,
    *,
    self_member_id: str,
    conversation_type: Optional[str] = None,
    cfg: Optional[AccessPolicyConfig] = None,
    owner_member_id: str = "",
    sender_owner_member_id: str = "",
) -> AccessDecision:
    cfg = cfg or AccessPolicyConfig()
    conv_type = (conversation_type or msg.conversation_type or "dm").lower()
    is_owner = bool(owner_member_id and msg.sender_id == owner_member_id)

    if self_member_id and msg.sender_id == self_member_id:
        return AccessDecision(False, "own_message")

    if msg.sender_type == "system":
        return AccessDecision(cfg.handle_system, "system_sender")

    if msg.sender_type == "agent":
        agent_allowed = "*" in cfg.agent_allowlist or msg.sender_id in cfg.agent_allowlist
        if conv_type == "dm":
            if (
                cfg.allow_sibling_dm
                and agent_allowed
                and owner_member_id
                and sender_owner_member_id == owner_member_id
            ):
                return AccessDecision(True, "sibling_dm_allowed")
            return AccessDecision(False, "agent_dm_blocked")
        # Agent group traffic has an extra loop guard. Once admitted it still
        # passes through the ordinary group scope/allowlist/allowFrom gates.
        if (
            not cfg.allow_agent_senders
            or not agent_allowed
            or not _is_directly_mentioned(msg, self_member_id)
        ):
            return AccessDecision(False, "agent_sender_blocked")

    if conv_type == "dm":
        if is_owner:
            return AccessDecision(True, "dm_owner")  # owner always exempt
        policy = (cfg.dm_policy or "open").lower()
        if policy == "owner":
            return AccessDecision(False, "dm_owner_only")
        if policy == "allowlist":
            if msg.sender_id in cfg.dm_allowlist:
                return AccessDecision(True, "dm_allowlisted")
            return AccessDecision(False, "dm_not_allowlisted")
        return AccessDecision(True, "dm")

    # Group / broadcast / bridge conversations.
    self_names = (
        (cfg.self_display_name, *cfg.self_aliases)
        if msg.sender_type == "human"
        else ()
    )
    mentioned = _is_mentioned(msg, self_member_id, self_names)
    group = cfg.group_configs.get(msg.conversation_id)
    policy = (cfg.group_policy or "allowlist").lower()
    if policy == "disabled":
        return AccessDecision(False, "group_disabled")
    owner_mention_bypass = policy == "allowlist" and group is None and is_owner and mentioned
    if policy == "allowlist" and group is None and not owner_mention_bypass:
        return AccessDecision(False, "group_not_allowlisted")
    group = group or {}
    allow_from = [
        str(v)
        for v in (
            group.get("allow_from")
            if "allow_from" in group
            else group.get("allowFrom")
        )
        or []
    ]
    if (
        allow_from
        and "*" not in allow_from
        and msg.sender_id not in allow_from
        and not is_owner
    ):
        return AccessDecision(False, "group_sender_not_allowed")
    mode = str(group.get("mode") or "").lower()
    if mode == "silent":
        return AccessDecision(True, "group_silent")
    if mode == "smart" or not cfg.group_require_mention:
        return AccessDecision(True, "group_open")
    if mentioned:
        return AccessDecision(
            True,
            "group_owner_mention" if owner_mention_bypass else "group_mention",
        )
    return AccessDecision(False, "group_no_mention")

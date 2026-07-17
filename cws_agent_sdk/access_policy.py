"""Inbound access policy — decide whether a message should trigger the agent.

Port of zylos-openmax's shouldHandleMessage semantics, as a pure function:

- DM from a human: handle (optionally restricted to owner / allowlist).
- DM from a sibling agent (same owner): handle only if sibling_dm allowed —
  default False to prevent agent-to-agent chat loops.
- Group: handle only when the agent is mentioned (@agent / all / all_agents),
  unless group_require_mention is disabled.
- Messages from SYSTEM senders: surfaced as handle=False by default
  (delivered separately if the adapter wants lifecycle events).
- Own messages: never handled (also enforced upstream in the bridge).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .types import InboundMessage


@dataclass
class AccessPolicyConfig:
    group_require_mention: bool = True
    allow_agent_senders: bool = False  # let other agents' messages trigger us
    allow_sibling_dm: bool = False  # same-owner agent DMs
    dm_allowlist: list[str] = field(default_factory=list)  # member_ids; empty = allow all
    handle_system: bool = False


@dataclass
class AccessDecision:
    handle: bool
    reason: str


def _is_mentioned(msg: InboundMessage, self_member_id: str) -> bool:
    for m in msg.mentions or []:
        mtype = str(m.get("type", "")).lower() if isinstance(m, dict) else ""
        if mtype in ("all", "all_agents"):
            return True
        if isinstance(m, dict) and str(m.get("member_id", "")) == self_member_id:
            return True
    return False


def decide_inbound(
    msg: InboundMessage,
    *,
    self_member_id: str,
    conversation_type: Optional[str] = None,
    cfg: Optional[AccessPolicyConfig] = None,
) -> AccessDecision:
    cfg = cfg or AccessPolicyConfig()
    conv_type = (conversation_type or msg.conversation_type or "dm").lower()

    if self_member_id and msg.sender_id == self_member_id:
        return AccessDecision(False, "own_message")

    if msg.sender_type == "system":
        return AccessDecision(cfg.handle_system, "system_sender")

    if msg.sender_type == "agent":
        if conv_type == "dm":
            if cfg.allow_sibling_dm:
                return AccessDecision(True, "sibling_dm_allowed")
            return AccessDecision(False, "agent_dm_blocked")
        # Group: an agent sender only triggers us when explicitly allowed AND
        # we are mentioned — both gates guard against agent-to-agent loops.
        if cfg.allow_agent_senders and _is_mentioned(msg, self_member_id):
            return AccessDecision(True, "agent_mention")
        return AccessDecision(False, "agent_sender_blocked")

    # Human sender.
    if conv_type == "dm":
        if cfg.dm_allowlist and msg.sender_id not in cfg.dm_allowlist:
            return AccessDecision(False, "dm_not_allowlisted")
        return AccessDecision(True, "dm")

    # Group / broadcast / bridge conversations.
    if not cfg.group_require_mention:
        return AccessDecision(True, "group_open")
    if _is_mentioned(msg, self_member_id):
        return AccessDecision(True, "group_mention")
    return AccessDecision(False, "group_no_mention")

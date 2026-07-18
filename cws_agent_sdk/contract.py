"""Contract v1 adapter — maps this SDK's internal types onto the canonical,
language-neutral protocol contract shipped by @openmaxai/openmax-agent-sdk
(schemas/v1 + fixtures/v1).

This module exists so the golden conformance corpus can be run against OUR
real code paths (text extraction, access-policy decisions) and the output
compared/validated in the contract's vocabulary. It is not used on the hot
message path.
"""

from __future__ import annotations

from typing import Any, Optional

from .access_policy import AccessPolicyConfig, decide_inbound
from .bridge import CwsBridge
from .types import InboundMessage

# our decision reasons -> contract decision-reason vocabulary
_REASON_MAP = {
    "dm": "dm:open",
    "dm_allowlisted": "dm:allowlist",
    "dm_owner": "dm:owner-exempt",
    "dm_owner_only": "dm:owner-only-rejected",
    "dm_not_allowlisted": "dm:allowlist-rejected",
    "group_open": "group:open",
    "group_mention": "group:mention",
    "group_owner_mention": "group:owner-mention",
    "group_no_mention": "group:no-mention-rejected",
    "system_sender": "system:passthrough",
}


def _policy_from_org(
    org: dict, conversation: Optional[dict]
) -> tuple[AccessPolicyConfig, str]:
    """Translate a fixture's org.access block into our policy + group mode."""
    access = (org or {}).get("access") or {}
    dm_policy = str(access.get("dmPolicy", "open")).lower()
    cfg = AccessPolicyConfig(
        dm_policy=dm_policy,
        dm_allowlist=[str(i) for i in access.get("dmAllowFrom") or []],
    )
    mode = ""
    conv_id = str((conversation or {}).get("id", ""))
    groups = access.get("groups") or {}
    if conv_id and conv_id in groups:
        mode = str((groups[conv_id] or {}).get("mode", ""))
        if "smart" in mode.lower():
            cfg.group_require_mention = False
    return cfg, mode


def normalize_for_contract(
    org: dict,
    frame: dict,
    detail: dict,
    conversation: Optional[dict] = None,
    via: str = "ws",
) -> dict:
    """Run a fixture's inputs through our real primitives and emit the
    contract-shaped normalized InboundMessage."""
    payload = (frame or {}).get("payload") or {}
    detail = detail or {}
    content = detail.get("content") or {}
    conv = conversation or {}
    conv_type = str(conv.get("type") or "dm").lower()
    conv_id = str(payload.get("conversation_id") or conv.get("id") or "")
    sender_id = str(detail.get("sender_id") or payload.get("sender_id") or "")
    self_member = str(((org or {}).get("self") or {}).get("member_id", ""))
    owner = (org or {}).get("owner") or {}
    owner_id = str(owner.get("member_id", ""))

    text = CwsBridge._extract_text(detail, content)

    msg = InboundMessage(
        message_id=str(detail.get("id") or payload.get("id") or ""),
        conversation_id=conv_id,
        org_id=str(org.get("org_id", "")),
        text=text,
        sender_id=sender_id,
        sender_name=str(detail.get("sender_display_name") or ""),
        sender_type=str(detail.get("sender_type", "")).lower(),
        conversation_type=conv_type,
        seq=int(detail.get("seq") or 0) or None,
        mentions=[m for m in detail.get("mentions") or [] if isinstance(m, dict)],
    )
    cfg, group_mode = _policy_from_org(org, conv)
    decision = decide_inbound(
        msg, self_member_id=self_member, cfg=cfg, owner_member_id=owner_id
    )
    reason = _REASON_MAP.get(decision.reason, decision.reason)
    if conv_type != "dm" and "smart" in group_mode.lower() and decision.handle:
        reason = "group:allowlist/smart"

    decision_out: dict[str, Any] = {"handle": decision.handle, "reason": reason}
    if decision.reason == "dm_owner" and not owner.get("name"):
        # Contract: when owner.name is unbound, surface the sender's display
        # name as a hint for the owner-binding UX.
        hint = msg.sender_name or ""
        if hint:
            decision_out["ownerNameHint"] = hint

    out: dict[str, Any] = {
        "orgId": str(org.get("org_id", "")),
        "orgSlug": str(org.get("slug", "")),
        "conversation": conv,
        "conversationId": conv_id,
        "conversationType": conv_type,
        "messageId": msg.message_id,
        "senderType": str(detail.get("sender_type", "")),
        "senderDisplayName": msg.sender_name,
        "type": str(content.get("content_type") or "text"),
        "text": text,
        "attachments": content.get("attachments") or [],
        "parentMessageId": (
            str(detail["parent_id"]) if detail.get("parent_id") else None
        ),
        "threadId": None,
        "endpoint": conv_id,
        "via": via,
        "decision": decision_out,
        "message": detail,
    }
    if org.get("org_name"):
        out["orgName"] = str(org["org_name"])
    if msg.seq is not None:
        out["seq"] = msg.seq
    if sender_id:
        out["senderId"] = sender_id
    if detail.get("priority") is not None:
        out["priority"] = int(detail["priority"])
    return out

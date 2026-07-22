from cws_agent_sdk.access_policy import AccessPolicyConfig, decide_inbound
from cws_agent_sdk.types import InboundMessage

ME = "me-1"


def msg(
    sender_type="human",
    sender_id="u-7",
    conv_type="dm",
    mentions=None,
    text="hi",
):
    return InboundMessage(
        message_id="1",
        conversation_id="c-1",
        org_id="o-1",
        text=text,
        sender_id=sender_id,
        sender_type=sender_type,
        conversation_type=conv_type,
        mentions=mentions or [],
    )


def test_safe_defaults_match_zylos_human_policy():
    cfg = AccessPolicyConfig()
    assert cfg.dm_policy == "owner"
    assert cfg.group_policy == "allowlist"
    assert cfg.allow_sibling_dm is False
    assert cfg.allow_agent_senders is False
    assert cfg.agent_allowlist == []


def test_human_dm_handled():
    d = decide_inbound(msg(), self_member_id=ME, cfg=AccessPolicyConfig(dm_policy="open"))
    assert d.handle and d.reason == "dm"


def test_dm_allowlist_blocks_stranger():
    cfg = AccessPolicyConfig(dm_policy="allowlist", dm_allowlist=["owner-1"])
    assert not decide_inbound(msg(sender_id="u-7"), self_member_id=ME, cfg=cfg).handle
    assert decide_inbound(msg(sender_id="owner-1"), self_member_id=ME, cfg=cfg).handle


def test_dm_owner_policy_and_exemption():
    cfg = AccessPolicyConfig(dm_policy="owner")
    d = decide_inbound(msg(sender_id="u-7"), self_member_id=ME, cfg=cfg)
    assert not d.handle and d.reason == "dm_owner_only"
    d = decide_inbound(
        msg(sender_id="boss-1"), self_member_id=ME, cfg=cfg, owner_member_id="boss-1"
    )
    assert d.handle and d.reason == "dm_owner"
    # owner also bypasses allowlist
    cfg2 = AccessPolicyConfig(dm_policy="allowlist", dm_allowlist=[])
    assert decide_inbound(
        msg(sender_id="boss-1"), self_member_id=ME, cfg=cfg2, owner_member_id="boss-1"
    ).handle


def test_group_owner_mention_bypass():
    cfg = AccessPolicyConfig(group_policy="allowlist", group_require_mention=True)
    m = msg(
        sender_id="boss-1",
        conv_type="group",
        mentions=[{"type": "member", "member_id": ME}],
    )
    d = decide_inbound(m, self_member_id=ME, cfg=cfg, owner_member_id="boss-1")
    assert d.handle and d.reason == "group_owner_mention"


def test_group_requires_mention_by_default():
    cfg = AccessPolicyConfig(group_policy="open")
    d = decide_inbound(msg(conv_type="group"), self_member_id=ME, cfg=cfg)
    assert not d.handle and d.reason == "group_no_mention"


def test_group_mention_by_member_id():
    cfg = AccessPolicyConfig(group_policy="open")
    d = decide_inbound(
        msg(conv_type="group", mentions=[{"type": "member", "member_id": ME}]),
        self_member_id=ME,
        cfg=cfg,
    )
    assert d.handle and d.reason == "group_mention"


def test_group_mention_all_agents():
    cfg = AccessPolicyConfig(group_policy="open")
    d = decide_inbound(
        msg(conv_type="group", mentions=[{"type": "all_agents"}]),
        self_member_id=ME,
        cfg=cfg,
    )
    assert d.handle


def test_group_open_mode():
    cfg = AccessPolicyConfig(group_policy="open", group_require_mention=False)
    assert decide_inbound(msg(conv_type="group"), self_member_id=ME, cfg=cfg).handle


def test_agent_dm_blocked_by_default():
    d = decide_inbound(msg(sender_type="agent"), self_member_id=ME)
    assert not d.handle and d.reason == "agent_dm_blocked"


def test_agent_group_blocked_even_with_mention_unless_allowed():
    m = msg(
        sender_type="agent",
        conv_type="group",
        mentions=[{"type": "member", "member_id": ME}],
    )
    assert not decide_inbound(m, self_member_id=ME).handle
    cfg = AccessPolicyConfig(
        group_policy="open",
        allow_agent_senders=True,
        agent_allowlist=["u-7"],
    )
    assert decide_inbound(m, self_member_id=ME, cfg=cfg).handle


def test_agent_group_still_obeys_group_scope_and_allow_from():
    message = msg(
        sender_type="agent",
        sender_id="agent-1",
        conv_type="group",
        mentions=[{"type": "member", "member_id": ME}],
    )
    base = dict(allow_agent_senders=True, agent_allowlist=["agent-1"])
    assert not decide_inbound(
        message,
        self_member_id=ME,
        cfg=AccessPolicyConfig(group_policy="disabled", **base),
    ).handle
    assert not decide_inbound(
        message,
        self_member_id=ME,
        cfg=AccessPolicyConfig(group_policy="allowlist", **base),
    ).handle
    allowed_group = {"c-1": {"mode": "mention", "allow_from": ["someone-else"]}}
    assert not decide_inbound(
        message,
        self_member_id=ME,
        cfg=AccessPolicyConfig(
            group_policy="allowlist", group_configs=allowed_group, **base
        ),
    ).handle


def test_agent_plain_text_mention_does_not_bypass_structured_loop_guard():
    cfg = AccessPolicyConfig(
        group_policy="open",
        allow_agent_senders=True,
        agent_allowlist=["agent-1"],
        self_display_name="COCO",
    )
    decision = decide_inbound(
        msg(
            sender_type="agent",
            sender_id="agent-1",
            conv_type="group",
            text="@COCO hello",
        ),
        self_member_id=ME,
        cfg=cfg,
    )
    assert not decision.handle and decision.reason == "agent_sender_blocked"


def test_agent_broadcast_mentions_do_not_bypass_direct_mention_loop_guard():
    cfg = AccessPolicyConfig(
        group_policy="open",
        allow_agent_senders=True,
        agent_allowlist=["agent-1"],
    )
    for mention_type in ("all", "all_agents"):
        decision = decide_inbound(
            msg(
                sender_type="agent",
                sender_id="agent-1",
                conv_type="group",
                mentions=[{"type": mention_type}],
            ),
            self_member_id=ME,
            cfg=cfg,
        )
        assert not decision.handle and decision.reason == "agent_sender_blocked"


def test_system_sender_delivered_by_default():
    # Scheduler DMs drive the task flow (dependency-ready, issue.activated).
    assert decide_inbound(msg(sender_type="system"), self_member_id=ME).handle
    cfg = AccessPolicyConfig(handle_system=False)
    assert not decide_inbound(
        msg(sender_type="system"), self_member_id=ME, cfg=cfg
    ).handle


def test_own_message_never_handled():
    assert not decide_inbound(msg(sender_id=ME), self_member_id=ME).handle

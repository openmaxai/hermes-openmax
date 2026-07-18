"""Runtime parity regressions against zylos-openmax."""

import pytest

from cws_agent_sdk.access_policy import AccessPolicyConfig, decide_inbound
from cws_agent_sdk.codec import FRAME_SYSTEM, Frame
from cws_agent_sdk.types import InboundMessage

from test_bridge import detail, make_bridge


def _msg(
    *,
    sender_id="user-1",
    sender_type="human",
    conversation_type="group",
    conversation_id="conv-1",
    text="hello",
    mentions=None,
):
    return InboundMessage(
        message_id="m-1",
        conversation_id=conversation_id,
        org_id="org-1",
        text=text,
        sender_id=sender_id,
        sender_type=sender_type,
        conversation_type=conversation_type,
        mentions=mentions or [],
    )


def test_group_scope_allowlist_and_allow_from():
    cfg = AccessPolicyConfig(
        group_policy="allowlist",
        group_configs={
            "conv-1": {"mode": "smart", "allow_from": ["user-1"]},
        },
    )
    assert decide_inbound(_msg(), self_member_id="me-1", cfg=cfg).handle
    assert not decide_inbound(
        _msg(sender_id="user-2"), self_member_id="me-1", cfg=cfg
    ).handle
    assert not decide_inbound(
        _msg(sender_id="user-1", conversation_id="conv-2"),
        self_member_id="me-1",
        cfg=cfg,
    ).handle


def test_group_disabled_but_owner_mention_bypasses():
    cfg = AccessPolicyConfig(group_policy="disabled")
    owner = _msg(
        sender_id="owner-1",
        mentions=[{"type": "member", "member_id": "me-1"}],
    )
    assert decide_inbound(
        owner, self_member_id="me-1", cfg=cfg, owner_member_id="owner-1"
    ).handle
    assert not decide_inbound(_msg(), self_member_id="me-1", cfg=cfg).handle


def test_plain_text_display_name_mention():
    cfg = AccessPolicyConfig(self_display_name="COCO")
    decision = decide_inbound(
        _msg(text="@COCO 请看一下"), self_member_id="me-1", cfg=cfg
    )
    assert decision.handle and decision.reason == "group_mention"


def test_sibling_dm_requires_same_owner():
    same_owner = _msg(sender_id="agent-1", sender_type="agent", conversation_type="dm")
    cfg = AccessPolicyConfig(allow_sibling_dm=True)
    assert decide_inbound(
        same_owner,
        self_member_id="me-1",
        cfg=cfg,
        owner_member_id="owner-1",
        sender_owner_member_id="owner-1",
    ).handle
    assert not decide_inbound(
        same_owner,
        self_member_id="me-1",
        cfg=cfg,
        owner_member_id="owner-1",
        sender_owner_member_id="owner-2",
    ).handle


@pytest.mark.asyncio
async def test_config_events_ignore_other_agent_and_apply_group_policy(tmp_path):
    async def on_message(_):
        pass

    b = make_bridge(tmp_path, on_message)

    def config(event, data):
        return Frame(type=FRAME_SYSTEM, payload={"event": event, "data": data})

    await b._handle_frame(
        config(
            "agent.config.dm_policy_changed",
            {"agent_member_id": "someone-else", "policy": "allowlist"},
        )
    )
    assert b._policy.dm_policy == "open"

    await b._handle_frame(
        config("agent.config.group_scope_changed", {"scope": "allowlist"})
    )
    await b._handle_frame(
        config(
            "agent.config.group_allowlist_changed",
            {"action": "set", "conversation_ids": ["conv-1"]},
        )
    )
    await b._handle_frame(
        config(
            "agent.config.group_allowfrom_changed",
            {"conversation_id": "conv-1", "allow_from": ["user-1"]},
        )
    )
    assert b._policy.group_policy == "allowlist"
    assert b._policy.group_configs["conv-1"]["allow_from"] == ["user-1"]


@pytest.mark.asyncio
async def test_edit_and_recall_are_delivered_as_runtime_messages(tmp_path):
    got = []

    async def on_message(message):
        got.append(message)

    b = make_bridge(tmp_path, on_message)
    b.comm.messages["conv-1:7"] = detail(msg_id=7, text="latest text")

    await b._handle_frame(
        Frame(
            type=FRAME_SYSTEM,
            payload={
                "event": "message.updated",
                "conversation_id": "conv-1",
                "data": {"message_id": 7, "edited_by": "user-1"},
            },
        )
    )
    await b._handle_frame(
        Frame(
            type=FRAME_SYSTEM,
            payload={
                "event": "message.recalled",
                "conversation_id": "conv-1",
                "data": {"message_id": 8, "recalled_by": "user-1"},
            },
        )
    )

    assert got[0].text == "[Message Edited] latest text"
    assert got[1].text.startswith("[Message Recalled]")


@pytest.mark.asyncio
async def test_group_lifecycle_notice_bypasses_only_mention_gate(tmp_path):
    got = []

    async def on_message(message):
        got.append(message)

    b = make_bridge(tmp_path, on_message)
    b._policy.group_policy = "open"
    b._policy.group_require_mention = True
    b.comm.conv_type = "group"

    await b._handle_frame(
        Frame(
            type=FRAME_SYSTEM,
            payload={
                "event": "message.recalled",
                "conversation_id": "group-1",
                "data": {"message_id": 8, "recalled_by": "user-1"},
            },
        )
    )

    assert len(got) == 1
    assert got[0].text.startswith("[Message Recalled]")
    assert b._policy.group_require_mention is True


@pytest.mark.asyncio
async def test_hot_config_change_reports_updated_policy(tmp_path):
    b = make_bridge(tmp_path, lambda _message: None)
    b._cfg.member_id = "agent-1"
    reported = []

    async def request(method, path, json=None, **_kwargs):
        reported.append((method, path, json))
        return {}

    b._http.request = request
    await b._handle_frame(
        Frame(
            type=FRAME_SYSTEM,
            payload={
                "event": "agent.config.dm_policy_changed",
                "data": {
                    "agent_member_id": "agent-1",
                    "policy": "allowlist",
                },
            },
        )
    )

    assert reported[-1][2]["dm_policy"] == "allowlist"


@pytest.mark.asyncio
async def test_top_level_content_attachments_are_hydrated(tmp_path):
    got = []

    async def on_message(message):
        got.append(message)

    b = make_bridge(tmp_path, on_message)
    b.comm.messages["conv-1:1"] = {
        **detail(),
        "content": {
            "content_type": "text",
            "body": {"text": "see file"},
            "attachments": [
                {
                    "artifact_id": "art-1",
                    "file_name": "a.txt",
                    "content_type": "text/plain",
                }
            ],
        },
    }

    async def resolve(uris, **_):
        return {
            "resolved": {
                "artifact://art-1": {
                    "download_url": "https://files.test/a.txt",
                    "content_type": "text/plain",
                    "name": "a.txt",
                }
            }
        }

    async def download(url, filename, **_):
        assert url == "https://files.test/a.txt"
        return "/tmp/a.txt"

    b.artifacts.resolve_uris = resolve
    b.artifacts.download = download
    await b._deliver_by_id("conv-1", 1, 10)

    assert got[0].media[0]["path"] == "/tmp/a.txt"


@pytest.mark.asyncio
async def test_first_sync_seeks_to_end_without_delivering_history(tmp_path):
    got = []

    async def on_message(message):
        got.append(message)

    b = make_bridge(tmp_path, on_message)
    calls = []

    async def sync(cursor, device_id, limit=100):
        calls.append(cursor)
        if cursor == 0:
            return {
                "events": [{"conversation_id": "old", "message_id": 1, "seq": 9}],
                "next_cursor": "9",
                "has_more": False,
            }
        return {"events": [], "has_more": False}

    b.comm.sync = sync
    await b._initialize_or_sync()

    assert calls == [0]
    assert got == []
    assert b._sync_seq == 9


@pytest.mark.asyncio
async def test_initial_and_reconnect_sync_are_serialized(tmp_path):
    b = make_bridge(tmp_path, lambda _message: None)
    active = 0
    peak = 0

    async def sync(cursor, device_id, limit=100):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        import asyncio

        await asyncio.sleep(0.01)
        active -= 1
        return {"events": [], "next_cursor": str(cursor), "has_more": False}

    b.comm.sync = sync
    import asyncio

    await asyncio.gather(b._initialize_or_sync(), b._sync_missed())
    assert peak == 1

"""Runtime parity regressions against zylos-openmax."""

import pytest

from cws_agent_sdk.access_policy import AccessPolicyConfig, decide_inbound
from cws_agent_sdk.codec import FRAME_SYSTEM, Frame
from cws_agent_sdk.providers import FileStorage
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


def test_group_disabled_blocks_owner_mention():
    cfg = AccessPolicyConfig(group_policy="disabled")
    owner = _msg(
        sender_id="owner-1",
        mentions=[{"type": "member", "member_id": "me-1"}],
    )
    assert not decide_inbound(
        owner, self_member_id="me-1", cfg=cfg, owner_member_id="owner-1"
    ).handle
    assert not decide_inbound(_msg(), self_member_id="me-1", cfg=cfg).handle


def test_plain_text_display_name_mention():
    cfg = AccessPolicyConfig(group_policy="open", self_display_name="COCO")
    decision = decide_inbound(
        _msg(text="@COCO 请看一下"), self_member_id="me-1", cfg=cfg
    )
    assert decision.handle and decision.reason == "group_mention"


def test_plain_text_mention_uses_boundaries_and_aliases():
    cfg = AccessPolicyConfig(
        group_policy="open",
        self_display_name="COCO",
        self_aliases=["helper.bot"],
    )
    assert not decide_inbound(
        _msg(text="@COCO-Suffix hello"), self_member_id="me-1", cfg=cfg
    ).handle
    assert not decide_inbound(
        _msg(text="@COCOX hello"), self_member_id="me-1", cfg=cfg
    ).handle
    assert decide_inbound(
        _msg(text="@helper.bot, hello"), self_member_id="me-1", cfg=cfg
    ).handle


def test_owner_group_exemptions_match_zylos_ordering():
    owner = "owner-1"
    mentioned = _msg(
        sender_id=owner,
        conversation_id="unlisted",
        mentions=[{"type": "member", "member_id": "me-1"}],
    )
    cfg = AccessPolicyConfig(group_policy="allowlist")
    assert decide_inbound(
        mentioned, self_member_id="me-1", cfg=cfg, owner_member_id=owner
    ).handle

    smart = _msg(sender_id=owner)
    cfg = AccessPolicyConfig(
        group_policy="allowlist",
        group_configs={
            "conv-1": {"mode": "smart", "allow_from": ["someone-else"]}
        },
    )
    assert decide_inbound(
        smart, self_member_id="me-1", cfg=cfg, owner_member_id=owner
    ).handle


def test_corrupt_persisted_policy_falls_back_to_safe_normalized_state(tmp_path):
    FileStorage(tmp_path).write_json(
        "policy.json",
        {
            "dm_policy": "closed",
            "group_policy": "everything",
            "dm_allowlist": "not-a-list",
            "group_modes": {"c-1": "open", "c-2": "silent"},
            "group_configs": {
                "c-2": {"mode": "unexpected", "allow_from": "u-1"},
                "c-3": "bad-shape",
            },
        },
    )

    bridge = make_bridge(tmp_path, lambda _message: None)

    assert bridge._policy.dm_policy == "owner"
    assert bridge._policy.group_policy == "allowlist"
    assert bridge._policy.dm_allowlist == []
    assert bridge._group_mode_overrides == {"c-2": "silent"}
    assert bridge._policy.group_configs == {
        "c-2": {"mode": "silent", "allow_from": ["*"]}
    }


def test_sibling_dm_requires_same_owner():
    same_owner = _msg(sender_id="agent-1", sender_type="agent", conversation_type="dm")
    cfg = AccessPolicyConfig(allow_sibling_dm=True, agent_allowlist=["agent-1"])
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
    assert b._policy.dm_policy == "owner"

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
async def test_invalid_config_events_do_not_mutate_persist_report_or_callback(tmp_path):
    b = make_bridge(tmp_path, lambda _message: None)
    b._cfg.member_id = "me-1"
    reports = []
    callbacks = []

    async def request(method, path, json=None, **_kwargs):
        reports.append((method, path, json))
        return {}

    async def on_config(event, data):
        callbacks.append((event, data))

    b._http.request = request
    b._on_config_event = on_config

    invalid = [
        ("agent.config.dm_policy_changed", {"policy": "closed"}),
        ("agent.config.dm_allowlist_changed", {"action": "append", "member_ids": []}),
        ("agent.config.group_mode_changed", {"conversation_id": "c-1", "mode": "open"}),
        ("agent.config.group_scope_changed", {"scope": "closed"}),
        ("agent.config.group_allowlist_changed", {"action": "set", "conversation_ids": "c-1"}),
        ("agent.config.group_allowfrom_changed", {"conversation_id": "c-1", "allow_from": "u-1"}),
    ]
    for event, data in invalid:
        await b._handle_frame(
            Frame(type=FRAME_SYSTEM, payload={"event": event, "data": data})
        )

    assert b._policy.dm_policy == "owner"
    assert b._policy.group_policy == "allowlist"
    assert b._policy.group_configs == {}
    assert b._storage.read_json("policy.json") is None
    assert reports == []
    assert callbacks == []


@pytest.mark.asyncio
async def test_silent_config_is_persisted_and_reported_as_effective_group(tmp_path):
    b = make_bridge(tmp_path, lambda _message: None)
    b._cfg.member_id = "me-1"
    reports = []

    async def request(method, path, json=None, **_kwargs):
        reports.append(json)
        return {}

    b._http.request = request
    await b._handle_frame(
        Frame(
            type=FRAME_SYSTEM,
            payload={
                "event": "agent.config.group_mode_changed",
                "data": {"conversation_id": "c-1", "mode": "silent"},
            },
        )
    )

    assert b._policy.group_configs["c-1"]["mode"] == "silent"
    assert b._storage.read_json("policy.json")["group_configs"]["c-1"]["mode"] == "silent"
    assert reports[-1]["groups"] == [
        {"conversation_id": "c-1", "mode": "silent", "allow_from": ["*"]}
    ]


@pytest.mark.asyncio
async def test_local_dm_policy_update_changes_live_state_persistence_and_report(tmp_path):
    import asyncio

    b = make_bridge(tmp_path, lambda _message: None)
    b._cfg.member_id = "me-1"
    reports = []

    async def request(method, path, json=None, **_kwargs):
        reports.append(json)
        return {}

    b._http.request = request
    result = await b.apply_local_dm_access("allowlist", ["u-1", "u-1"])
    await asyncio.sleep(0)

    assert result == {"dm_policy": "allowlist", "dm_allowlist": ["u-1"]}
    assert b.get_dm_access() == result
    assert b._storage.read_json("policy.json")["dm_policy"] == "allowlist"
    assert reports[-1]["dm_policy"] == "allowlist"
    assert reports[-1]["dm_allowlist"] == ["u-1"]


@pytest.mark.asyncio
async def test_local_dm_policy_thread_bridge_runs_on_owner_loop(tmp_path):
    import asyncio

    b = make_bridge(tmp_path, lambda _message: None)
    b._cfg.member_id = "me-1"
    b._loop = asyncio.get_running_loop()

    async def request(*_args, **_kwargs):
        return {}

    b._http.request = request
    result = await asyncio.to_thread(
        b.apply_local_dm_access_threadsafe, "open", ["u-1"]
    )
    await asyncio.sleep(0)

    assert result == {"dm_policy": "open", "dm_allowlist": ["u-1"]}
    assert b.get_dm_access() == result


@pytest.mark.asyncio
async def test_edit_and_recall_are_delivered_as_runtime_messages(tmp_path):
    got = []

    async def on_message(message):
        got.append(message)

    b = make_bridge(tmp_path, on_message)
    b._policy.dm_policy = "open"
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

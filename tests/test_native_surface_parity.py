from __future__ import annotations

import asyncio
import json

import pytest

from cws_agent_sdk.errors import CwsApiError
from cws_agent_sdk.services import (
    AsService,
    CommService,
    CoreService,
    KbService,
    TmService,
)
from hermes_openmax import tools


class FakeHttp:
    def __init__(self, result=None):
        self.calls = []
        self.result = {} if result is None else result

    async def get(self, path, params=None):
        self.calls.append(("GET", path, params, None))
        return self.result

    async def post(self, path, json=None):
        self.calls.append(("POST", path, None, json))
        return self.result

    async def request(self, method, path, *, json=None, params=None, **kw):
        self.calls.append((method, path, params, json))
        return self.result

    async def get_page(self, path, params=None):
        self.calls.append(("GET_PAGE", path, params, None))
        if isinstance(self.result, tuple):
            return self.result
        return self.result if isinstance(self.result, list) else [], {}


def run(coro):
    return asyncio.run(coro)


def test_tool_errors_preserve_status_and_structured_body(monkeypatch):
    async def fail(_):
        raise CwsApiError(
            "invalid", status=422, body={"error": {"errors": [{"field": "name"}]}}
        )

    real_run = tools.asyncio.run

    def raise_status(coro):
        coro.close()
        raise CwsApiError(
            "invalid", status=422, body={"error": {"errors": [{"field": "name"}]}}
        )

    monkeypatch.setattr(tools.asyncio, "run", raise_status)
    assert json.loads(tools._run(fail)) == {
        "error": "invalid",
        "status": 422,
        "body": {"error": {"errors": [{"field": "name"}]}},
        "errors": [{"field": "name"}],
    }
    monkeypatch.setattr(tools.asyncio, "run", real_run)


def test_core_rename_profiles_and_full_lifecycle_surface():
    h = FakeHttp([])
    s = CoreService(h)
    run(s.set_display_name("New"))
    run(
        s.list_agent_profiles(
            project_id="p", member_ids=["a", "b"], include=["capabilities", "tags"]
        )
    )
    run(s.get_organization("o"))
    run(s.create_organization({"name": "N"}))
    run(s.switch_organization("o"))
    run(s.create_invitation({"role_id": "r"}))
    run(
        s.list_invitations(
            status="pending", page=2, page_size=10, order_by="created_at"
        )
    )
    run(s.accept_invitation("i", "tok"))
    run(s.revoke_invitation("i"))
    run(s.get_onboarding_session())
    run(s.report_onboarding_event({"event_type": "d1_activation"}))
    run(s.create_platform_agent({"display_name": "A"}))
    run(s.delete_platform_agent("a"))
    run(s.get_agent_domain("identity"))
    assert h.calls == [
        ("PATCH", "/api/v1/me", None, {"display_name": "New"}),
        (
            "GET_PAGE",
            "/api/v1/agent-profiles",
            {
                "project_id": "p",
                "member_id": ["a", "b"],
                "include": ["capabilities", "tags"],
            },
            None,
        ),
        ("GET", "/api/v1/organizations/o", None, None),
        ("POST", "/api/v1/organizations", None, {"name": "N"}),
        ("POST", "/api/v1/organizations/o/switch", None, {}),
        ("POST", "/api/v1/invitations", None, {"role_id": "r"}),
        (
            "GET_PAGE",
            "/api/v1/invitations",
            {"status": "pending", "page": 2, "page_size": 10, "order_by": "created_at"},
            None,
        ),
        ("POST", "/api/v1/invitations/i/accept", None, {"token": "tok"}),
        ("DELETE", "/api/v1/invitations/i", None, None),
        ("GET", "/api/v1/onboarding/session", None, None),
        ("POST", "/api/v1/onboarding/events", None, {"event_type": "d1_activation"}),
        ("POST", "/api/v1/platform-agents", None, {"display_name": "A"}),
        ("DELETE", "/api/v1/platform-agents/a", None, None),
        ("GET", "/api/v1/platform-agents/identity/domain", None, None),
    ]
    with pytest.raises(ValueError, match="scope"):
        run(s.list_agent_profiles())


def test_tm_filters_paging_and_missing_fields():
    h = FakeHttp([])
    s = TmService(h)
    run(
        s.list_projects(
            status="active", query="x", page=2, page_size=5, order_by="name"
        )
    )
    run(
        s.list_issues(
            status="open",
            statuses=["open"],
            priority="high",
            include_archived=True,
            query="q",
            page=3,
            page_size=6,
            order_by="x",
        )
    )
    run(
        s.list_tasks(
            project_id="p",
            issue_id="i",
            status="running",
            include_archived=True,
            page=1,
            page_size=7,
            order_by="x",
        )
    )
    run(s.get_comment("c"))
    run(s.get_attempt("a"))
    run(s.add_project_member("p", "m", role="lead"))
    run(s.create_blueprint("i", [], estimated_budget={"tokens": 5}, notes="n"))
    run(
        s.transition_attempt(
            "a",
            "blocked",
            failure_reason="wait",
            blocked_on_approval_request_ids=["ap"],
        )
    )
    assert h.calls[0][2] == {
        "status": "active",
        "query": "x",
        "page": 2,
        "page_size": 5,
        "order_by": "name",
    }
    assert h.calls[1][2] == {
        "status": "open",
        "statuses": ["open"],
        "priority": "high",
        "include_archived": True,
        "query": "q",
        "page": 3,
        "page_size": 6,
        "order_by": "x",
    }
    assert h.calls[2][1] == "/api/v1/tasks"
    assert h.calls[2][2] == {
        "project_id": "p",
        "issue_id": "i",
        "status": "running",
        "include_archived": True,
        "page": 1,
        "page_size": 7,
        "order_by": "x",
    }
    assert (
        h.calls[3][1] == "/api/v1/comments/c" and h.calls[4][1] == "/api/v1/attempts/a"
    )
    assert h.calls[5][3] == {"member_id": "m", "role": "lead"}
    assert h.calls[6][3]["estimated_budget"] == {"tokens": 5}
    assert h.calls[7][3]["blocked_on_approval_request_ids"] == ["ap"]


def test_project_restore_is_not_advertised_outside_zylos_contract():
    assert "project_restore" not in tools.TASKS_SCHEMA["description"]


def test_kb_complete_collection_tree_page_and_pagination_surface():
    h = FakeHttp([])
    s = KbService(h)
    run(s.init_kb())
    run(s.get_kb("k"))
    run(s.update_kb("k", {"name": "N"}))
    run(s.delete_kb("k"))
    run(s.archive_kb("k"))
    run(s.unarchive_kb("k"))
    run(s.get_tree("k"))
    run(s.get_node("k", "n"))
    run(s.get_breadcrumb("k", "n"))
    run(s.list_children("k", "n"))
    run(s.preview_node("k", "n"))
    run(s.list_pages(cursor="c", limit=3, offset=4))
    run(s.get_revision("p", 2))
    run(s.search_pages("q", kb_id="k", limit=3, offset=4, sort="new"))
    run(s.download_node("k", "n", inline=True))
    run(s.batch_download("k", ["n"], inline=True))
    assert [c[1] for c in h.calls[:11]] == [
        "/api/v1/kbs/init",
        "/api/v1/kbs/k",
        "/api/v1/kbs/k",
        "/api/v1/kbs/k",
        "/api/v1/kbs/k/archive",
        "/api/v1/kbs/k/unarchive",
        "/api/v1/kbs/k/tree/roots",
        "/api/v1/kbs/k/tree/nodes/n",
        "/api/v1/kbs/k/tree/nodes/n/breadcrumb",
        "/api/v1/kbs/k/tree/nodes/n/children",
        "/api/v1/kbs/k/tree/nodes/n/preview",
    ]
    assert h.calls[11][2] == {"cursor": "c", "limit": 3, "offset": 4}
    assert h.calls[13][2] == {
        "query": "q",
        "kb_id": "k",
        "limit": 3,
        "offset": 4,
        "sort": "new",
    }
    assert h.calls[14][2] == {"inline": True}
    assert h.calls[15][3] == {"node_ids": ["n"], "inline": True}


def test_comm_get_unread_queries_and_attachment_message():
    h = FakeHttp({"id": "m", "conversation_id": "c"})
    s = CommService(h)
    run(s.list_conversations(limit=5, cursor="cur", include_archived=True))
    run(s.get_conversation("c"))
    run(s.get_message("c", "m"))
    run(s.get_unread("c"))
    run(s.mark_read("c", 9))
    run(
        s.send_attachment(
            "c",
            artifact_id="a",
            file_name="x.pdf",
            content_type="application/pdf",
            size_bytes=4,
            caption="see",
            reply_to="m",
        )
    )
    assert h.calls[0][2] == {"limit": 5, "cursor": "cur", "include_archived": True}
    assert [c[1] for c in h.calls[1:5]] == [
        "/api/v1/conversations/c",
        "/api/v1/conversations/c/messages/m",
        "/api/v1/conversations/c/unread",
        "/api/v1/conversations/c/read",
    ]
    sent = h.calls[5][3]
    assert (
        sent["type"] == "FILE"
        and sent["parent_id"] == "m"
        and sent["content"]["attachments"][0]["artifact_id"] == "a"
    )


def test_artifact_url_normalization_and_root_upload():
    resolved = {
        "resolved": {"artifact://a": {"download_url": "https://x", "name": "f"}},
        "failed": [],
    }
    h = FakeHttp(resolved)
    s = AsService(h)
    assert run(s.get_url("a", inline=True))["url"] == "https://x"
    run(s.prepare_kb_upload("f", "text/plain", 1))
    assert h.calls[0][3] == {"uris": ["artifact://a"], "inline": True}
    assert h.calls[1][3] == {
        "filename": "f",
        "content_type": "text/plain",
        "size_bytes": 1,
    }

"""Native Hermes tools exposing workspace services (tm / kb / members).

Handlers are SYNC (Hermes tool contract: fn(args, **kw) -> str) and run on
agent worker threads, so each call uses a short-lived SDK client driven by
asyncio.run(). Tokens are shared with the platform adapter via the same
FileStorage state dir, so calls reuse the cached JWT instead of re-exchanging.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

_STATE_DIR = "~/.hermes/platforms/cws"


def _run(coro_factory) -> str:
    async def go():
        from cws_agent_sdk import CwsConfig
        from cws_agent_sdk.http import CwsHttpClient
        from cws_agent_sdk.providers import FileStorage
        from cws_agent_sdk.services import CommService, CoreService, KbService, TmService
        from cws_agent_sdk.token import TokenManager

        cfg = CwsConfig.from_env()
        storage = FileStorage(_STATE_DIR)
        tokens = TokenManager(cfg, storage=storage)
        http = CwsHttpClient(cfg, tokens)
        try:
            svc = {
                "tm": TmService(http),
                "kb": KbService(http),
                "core": CoreService(http),
                "comm": CommService(http),
            }
            return await coro_factory(svc)
        finally:
            await http.aclose()
            await tokens.aclose()

    try:
        result = asyncio.run(go())
        return json.dumps(result, ensure_ascii=False, default=str)[:20000]
    except Exception as exc:  # noqa: BLE001 — tool errors go back to the model as text
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


# -- workspace_tasks ---------------------------------------------------------

TASKS_SCHEMA = {
    "name": "workspace_tasks",
    "description": (
        "Operate OpenMax workspace projects/issues/tasks (cws-work). Actions: "
        "list_projects, list_issues(project_id?), get_issue(issue_id), "
        "create_issue(project_id, body{title,owner_member_id,...}), "
        "update_issue(issue_id, body), "
        "issue_action(issue_id, name=activate|submit-plan|accept-plan|deliver|resume|terminate|accept-delivered, body?), "
        "list_tasks(issue_id?), create_task(project_id, issue_id, body), "
        "task_action(task_id, name=transition|claim|start|reassign, body?)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "project_id": {"type": "string"},
            "issue_id": {"type": "string"},
            "task_id": {"type": "string"},
            "name": {"type": "string", "description": "sub-action name for issue_action/task_action"},
            "body": {"type": "object"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
    },
}


def handle_tasks(args: dict, **kw: Any) -> str:
    async def go(svc):
        tm = svc["tm"]
        a = args.get("action")
        limit = int(args.get("limit") or 20)
        body = args.get("body") or {}
        if a == "list_projects":
            return await tm.list_projects(limit=limit)
        if a == "list_issues":
            return await tm.list_issues(args.get("project_id"), limit=limit)
        if a == "get_issue":
            return await tm.get_issue(args["issue_id"])
        if a == "create_issue":
            return await tm.create_issue(args["project_id"], body)
        if a == "update_issue":
            return await tm.update_issue(args["issue_id"], body)
        if a == "issue_action":
            return await tm.issue_action(args["issue_id"], args["name"], body)
        if a == "list_tasks":
            return await tm.list_tasks(args.get("issue_id"), limit=limit)
        if a == "create_task":
            return await tm.create_task(args["project_id"], args["issue_id"], body)
        if a == "task_action":
            return await tm.task_action(args["task_id"], args["name"], body)
        return {"error": f"unknown action {a!r}"}

    return _run(go)


# -- workspace_kb -------------------------------------------------------------

KB_SCHEMA = {
    "name": "workspace_kb",
    "description": (
        "OpenMax workspace knowledge base (cws-kb). Actions: list_kbs, "
        "search(query, kb_id?), get_page(page_id), get_page_content(page_id), "
        "put_page_content(page_id, body), create_page(kb_id, body), tree(kb_id)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "kb_id": {"type": "string"},
            "page_id": {"type": "string"},
            "query": {"type": "string"},
            "body": {"type": "object"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
    },
}


def handle_kb(args: dict, **kw: Any) -> str:
    async def go(svc):
        kb = svc["kb"]
        a = args.get("action")
        limit = int(args.get("limit") or 20)
        if a == "list_kbs":
            return await kb.list_kbs(limit=limit)
        if a == "search":
            return await kb.search_pages(args["query"], kb_id=args.get("kb_id"), limit=limit)
        if a == "get_page":
            return await kb.get_page(args["page_id"])
        if a == "get_page_content":
            return await kb.get_page_content(args["page_id"])
        if a == "put_page_content":
            return await kb.put_page_content(args["page_id"], args.get("body") or {})
        if a == "create_page":
            return await kb.create_page(args["kb_id"], args.get("body") or {})
        if a == "tree":
            return await kb.get_tree(args["kb_id"])
        return {"error": f"unknown action {a!r}"}

    return _run(go)


# -- workspace_members ---------------------------------------------------------

MEMBERS_SCHEMA = {
    "name": "workspace_members",
    "description": (
        "OpenMax workspace directory. Actions: me, list(kind=human|agent, search?), "
        "get(member_id), create_dm(peer_member_id)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "member_id": {"type": "string"},
            "peer_member_id": {"type": "string"},
            "kind": {"type": "string"},
            "search": {"type": "string"},
        },
        "required": ["action"],
    },
}


def handle_members(args: dict, **kw: Any) -> str:
    async def go(svc):
        core, comm = svc["core"], svc["comm"]
        a = args.get("action")
        if a == "me":
            return await core.me()
        if a == "list":
            return await core.list_members(kind=args.get("kind"), search=args.get("search"))
        if a == "get":
            return await core.get_member(args["member_id"])
        if a == "create_dm":
            return await comm.create_dm(args["peer_member_id"])
        return {"error": f"unknown action {a!r}"}

    return _run(go)


ALL_TOOLS = (
    ("workspace_tasks", TASKS_SCHEMA, handle_tasks, "📋"),
    ("workspace_kb", KB_SCHEMA, handle_kb, "📚"),
    ("workspace_members", MEMBERS_SCHEMA, handle_members, "👥"),
)

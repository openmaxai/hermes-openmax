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
        "task_action(task_id, name=transition|claim|start|reassign, body?), "
        "comment_create(work_type=issue|task, work_id, text), "
        "comment_list(work_type, work_id), "
        "attempt_create(task_id), attempt_list(task_id), "
        "attempt_finish(attempt_id, status=done|failed|blocked|cancelled, reason?), "
        "blueprint_create(issue_id, body{steps[{temp_id,description,depends_on_temp_ids?}],notes?}), "
        "blueprint_get(blueprint_id), blueprint_list(issue_id), blueprint_submit(blueprint_id), "
        "work_refs(query? | project_id?) -> proj://... issue://... references, "
        "binding_create(body{cron_expr,lead_member_id,owner_member_id,spec{project_id,title}}), "
        "binding_list, binding_delete(binding_id)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "project_id": {"type": "string"},
            "issue_id": {"type": "string"},
            "task_id": {"type": "string"},
            "attempt_id": {"type": "string"},
            "blueprint_id": {"type": "string"},
            "binding_id": {"type": "string"},
            "work_type": {"type": "string"},
            "work_id": {"type": "string"},
            "text": {"type": "string"},
            "status": {"type": "string"},
            "reason": {"type": "string"},
            "query": {"type": "string"},
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
        if a == "comment_create":
            return await tm.create_comment(args["work_type"], args["work_id"], args["text"])
        if a == "comment_list":
            return await tm.list_comments(args["work_type"], args["work_id"], limit=limit)
        if a == "attempt_create":
            return await tm.create_attempt(args["task_id"])
        if a == "attempt_list":
            return await tm.list_attempts(args["task_id"])
        if a == "attempt_finish":
            return await tm.transition_attempt(
                args["attempt_id"], args["status"], failure_reason=args.get("reason", "")
            )
        if a == "blueprint_create":
            return await tm.create_blueprint(
                args["issue_id"], body.get("steps") or [], notes=body.get("notes", "")
            )
        if a == "blueprint_get":
            return await tm.get_blueprint(args["blueprint_id"])
        if a == "blueprint_list":
            return await tm.list_blueprints(args["issue_id"])
        if a == "blueprint_submit":
            return await tm.submit_blueprint(args["blueprint_id"])
        if a == "work_refs":
            return await tm.work_references(
                query=args.get("query", ""), project_id=args.get("project_id", ""), limit=limit
            )
        if a == "binding_create":
            return await tm.create_event_binding(
                body["cron_expr"], body["lead_member_id"], body.get("spec") or {},
                owner_member_id=body.get("owner_member_id", ""),
            )
        if a == "binding_list":
            return await tm.list_event_bindings()
        if a == "binding_delete":
            await tm.delete_event_binding(args["binding_id"])
            return {"ok": True}
        return {"error": f"unknown action {a!r}"}

    return _run(go)


# -- workspace_kb -------------------------------------------------------------

KB_SCHEMA = {
    "name": "workspace_kb",
    "description": (
        "OpenMax workspace knowledge base (cws-kb). Actions: list_kbs, "
        "search(query, kb_id?), get_page(page_id), get_page_content(page_id), "
        "put_page_content(page_id, body), create_page(kb_id, body), tree(kb_id), "
        "revisions(page_id), revision_diff(page_id, from_rev, to_rev), "
        "revision_restore(page_id, revision_id), trash(page_id), restore(page_id), "
        "trashed, create_folder(kb_id, name, parent_id?), rename_node(kb_id, node_id, name), "
        "move_node(kb_id, node_id, parent_id?), delete_node(kb_id, node_id), "
        "download_node(kb_id, node_id) -> presigned URL"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "kb_id": {"type": "string"},
            "page_id": {"type": "string"},
            "node_id": {"type": "string"},
            "parent_id": {"type": "string"},
            "name": {"type": "string"},
            "revision_id": {"type": "integer"},
            "from_rev": {"type": "integer"},
            "to_rev": {"type": "integer"},
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
        if a == "revisions":
            return await kb.list_revisions(args["page_id"], limit=limit)
        if a == "revision_diff":
            return await kb.diff_revisions(args["page_id"], int(args["from_rev"]), int(args["to_rev"]))
        if a == "revision_restore":
            return await kb.restore_revision(args["page_id"], int(args["revision_id"]))
        if a == "trash":
            return await kb.trash_page(args["page_id"])
        if a == "restore":
            return await kb.restore_page(args["page_id"])
        if a == "trashed":
            return await kb.list_trashed(limit=limit)
        if a == "create_folder":
            return await kb.create_folder(args["kb_id"], args["name"], args.get("parent_id", ""))
        if a == "rename_node":
            await kb.rename_node(args["kb_id"], args["node_id"], args["name"])
            return {"ok": True}
        if a == "move_node":
            await kb.move_node(args["kb_id"], args["node_id"], args.get("parent_id", ""))
            return {"ok": True}
        if a == "delete_node":
            await kb.delete_node(args["kb_id"], args["node_id"])
            return {"ok": True}
        if a == "download_node":
            return await kb.download_node(args["kb_id"], args["node_id"])
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


# -- workspace_comm ------------------------------------------------------------

COMM_SCHEMA = {
    "name": "workspace_comm",
    "description": (
        "OpenMax workspace conversations. Actions: list (conversations with "
        "unread_count/unread_mention), history(conversation_id, limit?), "
        "send(conversation_id, text, reply_to?), "
        "create_group(name, member_ids[], description?). Use this to "
        "proactively message any conversation (e.g. report to your owner)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "conversation_id": {"type": "string"},
            "text": {"type": "string"},
            "reply_to": {"type": "string"},
            "name": {"type": "string"},
            "member_ids": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
    },
}


def handle_comm(args: dict, **kw: Any) -> str:
    async def go(svc):
        comm = svc["comm"]
        a = args.get("action")
        limit = int(args.get("limit") or 20)
        if a == "list":
            return await comm.list_conversations(limit=limit)
        if a == "history":
            return await comm.list_messages(args["conversation_id"], limit=limit)
        if a == "send":
            r = await comm.send_message(
                args["conversation_id"], args["text"], reply_to=args.get("reply_to")
            )
            return {"sent": True, "message_id": r.message_id}
        if a == "create_group":
            return await comm.create_group(
                args["name"], list(args.get("member_ids") or []),
                description=args.get("description", ""),
            )
        return {"error": f"unknown action {a!r}"}

    return _run(go)


# -- workspace_artifacts --------------------------------------------------------

ARTIFACTS_SCHEMA = {
    "name": "workspace_artifacts",
    "description": (
        "OpenMax workspace files/artifacts. Actions: resolve(uris[]) -> "
        "download URLs (for YOUR OWN downloading/reading only — presigned "
        "URLs expire in minutes and must NEVER be pasted into chat); "
        "upload(conversation_id, local_path); kb_upload(parent_id, "
        "local_path). NOTE: to SHOW an image/file to the user in chat, do "
        "NOT use this tool — instead include MEDIA:/absolute/path in your "
        "reply text, which delivers it as a native image message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "uris": {"type": "array", "items": {"type": "string"}},
            "conversation_id": {"type": "string"},
            "parent_id": {"type": "string"},
            "local_path": {"type": "string"},
        },
        "required": ["action"],
    },
}


def handle_artifacts(args: dict, **kw: Any) -> str:
    async def go(svc):
        import os

        import httpx

        from cws_agent_sdk.services import AsService

        artifacts = AsService(svc["tm"]._http)  # share the http client
        a = args.get("action")
        if a == "resolve":
            return await artifacts.resolve_uris(list(args.get("uris") or []))
        if a in ("upload", "kb_upload"):
            path = args.get("local_path") or ""
            if not os.path.isfile(path):
                return {"error": f"file not found: {path}"}
            size = os.path.getsize(path)
            fname = os.path.basename(path)
            import mimetypes

            ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            if a == "upload":
                prep = await artifacts.prepare_conversation_upload(
                    args["conversation_id"], fname, ctype, size
                )
            else:
                prep = await artifacts.prepare_kb_upload(
                    args["parent_id"], fname, ctype, size
                )
            if not prep.get("instant_upload"):
                with open(path, "rb") as fh:
                    async with httpx.AsyncClient(timeout=300) as up:
                        resp = await up.put(
                            prep["upload_url"], content=fh.read(),
                            headers=prep.get("headers") or {},
                        )
                        resp.raise_for_status()
            if a == "upload":
                return await artifacts.finalize_conversation_upload(prep["upload_token"])
            return await artifacts.finalize_kb_upload(prep["upload_token"])
        return {"error": f"unknown action {a!r}"}

    return _run(go)


ALL_TOOLS = (
    ("workspace_tasks", TASKS_SCHEMA, handle_tasks, "📋"),
    ("workspace_kb", KB_SCHEMA, handle_kb, "📚"),
    ("workspace_members", MEMBERS_SCHEMA, handle_members, "👥"),
    ("workspace_comm", COMM_SCHEMA, handle_comm, "💬"),
    ("workspace_artifacts", ARTIFACTS_SCHEMA, handle_artifacts, "📎"),
)

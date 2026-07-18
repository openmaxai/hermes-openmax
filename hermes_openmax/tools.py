"""Native Hermes tools exposing workspace services (tm / kb / members).

Handlers are SYNC (Hermes tool contract: fn(args, **kw) -> str) and run on
agent worker threads, so each call uses a short-lived SDK client driven by
asyncio.run(). Tokens are shared with the platform adapter via the same
FileStorage state dir, so calls reuse the cached JWT instead of re-exchanging.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

_STATE_DIR = "~/.hermes/platforms/cws"

# State actions can be replayed by the model/runtime.  Keep notification
# deduplication local to the process: the task operation remains authoritative
# and notification delivery is deliberately best effort.
_PROGRESS_NOTIFICATION_KEYS: set[str] = set()
_PROGRESS_NOTIFICATION_LOCK = threading.Lock()
_STATE_ACTIONS = {
    "activate",
    "submit-plan",
    "accept-plan",
    "deliver",
    "resume",
    "terminate",
    "accept-delivered",
    "transition",
    "claim",
    "start",
}


def _progress_notification_text(kind: str, item_id: str, result: Any) -> str:
    result = result if isinstance(result, dict) else {}
    title = str(result.get("title") or result.get("name") or item_id)
    status = str(result.get("status") or result.get("state") or "updated")
    return f"📌 {kind} **{title}** 状态已更新为 **{status}**。"


async def _notify_progress_once(
    comm: Any,
    *,
    source_conversation_id: str,
    kind: str,
    item_id: str,
    action: str,
    result: Any,
) -> None:
    """Notify the originating conversation once, without affecting the action."""
    if not source_conversation_id or action not in _STATE_ACTIONS:
        return
    result_dict = result if isinstance(result, dict) else {}
    status = str(result_dict.get("status") or result_dict.get("state") or "updated")
    key = f"{source_conversation_id}:{kind}:{item_id}:{action}:{status}"
    with _PROGRESS_NOTIFICATION_LOCK:
        if key in _PROGRESS_NOTIFICATION_KEYS:
            return
        _PROGRESS_NOTIFICATION_KEYS.add(key)
    try:
        await comm.send_message(
            source_conversation_id,
            _progress_notification_text(kind, item_id, result),
            metadata={
                "progress_notification": True,
                "work_type": kind,
                "work_id": item_id,
                "action": action,
            },
        )
    except Exception:
        # A notification must never turn a successful task operation into a
        # failed tool call. Remove the key so a later retry can notify.
        with _PROGRESS_NOTIFICATION_LOCK:
            _PROGRESS_NOTIFICATION_KEYS.discard(key)


def _run(coro_factory) -> str:
    async def go():
        from cws_agent_sdk import CwsConfig
        from cws_agent_sdk.http import CwsHttpClient
        from cws_agent_sdk.providers import FileStorage
        from cws_agent_sdk.services import (
            AccessPolicyService,
            CommService,
            CoreService,
            KbService,
            TmService,
        )
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
                "policy": AccessPolicyService(storage),
            }
            return await coro_factory(svc)
        finally:
            await http.aclose()
            await tokens.aclose()

    try:
        result = asyncio.run(go())
        return json.dumps(result, ensure_ascii=False, default=str)[:20000]
    except Exception as exc:  # noqa: BLE001 — tool errors go back to the model as text
        payload: dict[str, Any] = {"error": str(exc)}
        status, body = getattr(exc, "status", None), getattr(exc, "body", None)
        if status is not None:
            payload["status"] = status
        if body is not None:
            payload["body"] = body
            errors = (
                body.get("error", {}).get("errors") if isinstance(body, dict) else None
            )
            if errors:
                payload["errors"] = errors
        return json.dumps(payload, ensure_ascii=False, default=str)


# -- workspace_tasks ---------------------------------------------------------

TASKS_SCHEMA = {
    "name": "workspace_tasks",
    "description": (
        "Operate OpenMax workspace projects/issues/tasks (cws-work). Actions: "
        "list_projects, project_create(body{name,slug?,lead_member_id,description?,knowledge_base_id?,member_ids?}), "
        "project_get/project_update(body)/project_archive(project_id), "
        "project_members(project_id), project_member_add/remove(project_id, member_id), "
        "task_get(task_id), blueprint_set_steps(blueprint_id, body{steps}), "
        "binding_get(binding_id), list_issues(project_id?), get_issue(issue_id), "
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
        "blueprint_get(blueprint_id), blueprint_list(issue_id), "
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
            "name": {
                "type": "string",
                "description": "sub-action name for issue_action/task_action",
            },
            "member_id": {"type": "string"},
            "role": {"type": "string"},
            "page": {"type": "integer"},
            "page_size": {"type": "integer"},
            "order_by": {"type": "string"},
            "statuses": {"type": "array", "items": {"type": "string"}},
            "priority": {"type": "string"},
            "include_archived": {"type": "boolean"},
            "blocked_on_approval_request_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "body": {"type": "object"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
    },
}


def handle_tasks(args: dict, **kw: Any) -> str:
    async def go(svc):
        tm = svc["tm"]
        comm = svc["comm"]
        source_conversation_id = str(
            kw.get("source_conversation_id") or kw.get("source_chat_id") or ""
        )
        a = args.get("action")
        limit = int(args.get("limit") or 20)
        body = args.get("body") or {}
        if a == "list_projects":
            return await tm.list_projects(
                limit=limit,
                status=args.get("status"),
                query=args.get("query"),
                page=args.get("page"),
                page_size=args.get("page_size"),
                order_by=args.get("order_by"),
            )
        if a == "project_create":
            return await tm.create_project(body)
        if a == "project_get":
            return await tm.get_project(args["project_id"])
        if a == "project_update":
            return await tm.update_project(args["project_id"], body)
        if a == "project_archive":
            return await tm.archive_project(args["project_id"])
        if a == "project_members":
            return await tm.list_project_members(args["project_id"])
        if a == "project_member_add":
            return await tm.add_project_member(
                args["project_id"], args["member_id"], role=args.get("role", "member")
            )
        if a == "project_member_remove":
            await tm.remove_project_member(args["project_id"], args["member_id"])
            return {"ok": True}
        if a == "task_get":
            return await tm.get_task(args["task_id"])
        if a == "blueprint_set_steps":
            return await tm.set_blueprint_steps(
                args["blueprint_id"], body.get("steps") or []
            )
        if a == "binding_get":
            return await tm.get_event_binding(args["binding_id"])
        if a == "list_issues":
            return await tm.list_issues(
                args.get("project_id"),
                limit=limit,
                status=args.get("status"),
                statuses=args.get("statuses"),
                priority=args.get("priority"),
                include_archived=args.get("include_archived"),
                query=args.get("query"),
                page=args.get("page"),
                page_size=args.get("page_size"),
                order_by=args.get("order_by"),
            )
        if a == "get_issue":
            return await tm.get_issue(args["issue_id"])
        if a == "create_issue":
            return await tm.create_issue(args["project_id"], body)
        if a == "update_issue":
            return await tm.update_issue(args["issue_id"], body)
        if a == "issue_action":
            result = await tm.issue_action(args["issue_id"], args["name"], body)
            await _notify_progress_once(
                comm,
                source_conversation_id=source_conversation_id,
                kind="issue",
                item_id=args["issue_id"],
                action=args["name"],
                result=result,
            )
            return result
        if a == "list_tasks":
            return await tm.list_tasks(
                args.get("issue_id"),
                limit=limit,
                project_id=args.get("project_id"),
                status=args.get("status"),
                include_archived=args.get("include_archived"),
                page=args.get("page"),
                page_size=args.get("page_size"),
                order_by=args.get("order_by"),
            )
        if a == "create_task":
            return await tm.create_task(args["project_id"], args["issue_id"], body)
        if a == "task_action":
            result = await tm.task_action(args["task_id"], args["name"], body)
            await _notify_progress_once(
                comm,
                source_conversation_id=source_conversation_id,
                kind="task",
                item_id=args["task_id"],
                action=args["name"],
                result=result,
            )
            return result
        if a == "comment_create":
            return await tm.create_comment(
                args["work_type"], args["work_id"], args["text"]
            )
        if a == "comment_list":
            return await tm.list_comments(
                args["work_type"], args["work_id"], limit=limit
            )
        if a == "comment_get":
            return await tm.get_comment(args["work_id"])
        if a == "attempt_create":
            return await tm.create_attempt(args["task_id"])
        if a == "attempt_list":
            return await tm.list_attempts(args["task_id"])
        if a == "attempt_get":
            return await tm.get_attempt(args["attempt_id"])
        if a == "attempt_finish":
            return await tm.transition_attempt(
                args["attempt_id"],
                args["status"],
                failure_reason=args.get("reason", ""),
                blocked_on_approval_request_ids=args.get(
                    "blocked_on_approval_request_ids"
                ),
            )
        if a == "blueprint_create":
            return await tm.create_blueprint(
                args["issue_id"],
                body.get("steps") or [],
                estimated_budget=body.get("estimated_budget"),
                notes=body.get("notes", ""),
            )
        if a == "blueprint_get":
            return await tm.get_blueprint(args["blueprint_id"])
        if a == "blueprint_list":
            return await tm.list_blueprints(args["issue_id"])

        if a == "work_refs":
            return await tm.work_references(
                query=args.get("query", ""),
                project_id=args.get("project_id", ""),
                limit=limit,
            )
        if a == "binding_create":
            return await tm.create_event_binding(
                body["cron_expr"],
                body["lead_member_id"],
                body.get("spec") or {},
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
        "download_node(kb_id, node_id) -> presigned URL, kb_create(body{name}), "
        "page_update(page_id, body), page_delete(page_id, PERMANENT), freeze(page_id), "
        "references(page_id), create_file(kb_id, name, artifact_id, parent_id?), "
        "batch_download(kb_id, node_ids[])"
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
            "artifact_id": {"type": "string"},
            "node_ids": {"type": "array", "items": {"type": "string"}},
            "query": {"type": "string"},
            "body": {"type": "object"},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
            "cursor": {"type": "string"},
            "sort": {"type": "string"},
            "inline": {"type": "boolean"},
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
        if a == "kb_init":
            return await kb.init_kb()
        if a == "kb_get":
            return await kb.get_kb(args["kb_id"])
        if a == "kb_update":
            return await kb.update_kb(args["kb_id"], args.get("body") or {})
        if a == "kb_delete":
            await kb.delete_kb(args["kb_id"])
            return {"ok": True}
        if a == "kb_archive":
            return await kb.archive_kb(args["kb_id"])
        if a == "kb_unarchive":
            return await kb.unarchive_kb(args["kb_id"])
        if a == "search":
            return await kb.search_pages(
                args["query"],
                kb_id=args.get("kb_id"),
                limit=limit,
                offset=args.get("offset"),
                sort=args.get("sort"),
            )
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
        if a == "node_get":
            return await kb.get_node(args["kb_id"], args["node_id"])
        if a == "breadcrumb":
            return await kb.get_breadcrumb(args["kb_id"], args["node_id"])
        if a == "children":
            return await kb.list_children(
                args["kb_id"], args.get("parent_id") or args["node_id"]
            )
        if a == "preview":
            return await kb.preview_node(args["kb_id"], args["node_id"])
        if a == "pages":
            return await kb.list_pages(
                cursor=args.get("cursor"), limit=limit, offset=args.get("offset")
            )
        if a == "revisions":
            return await kb.list_revisions(args["page_id"], limit=limit)
        if a == "page_revision":
            return await kb.get_revision(args["page_id"], int(args["revision_id"]))
        if a == "revision_diff":
            return await kb.diff_revisions(
                args["page_id"], int(args["from_rev"]), int(args["to_rev"])
            )
        if a == "revision_restore":
            return await kb.restore_revision(args["page_id"], int(args["revision_id"]))
        if a == "trash":
            return await kb.trash_page(args["page_id"])
        if a == "restore":
            return await kb.restore_page(args["page_id"])
        if a == "trashed":
            return await kb.list_trashed(limit=limit)
        if a == "create_folder":
            return await kb.create_folder(
                args["kb_id"], args["name"], args.get("parent_id", "")
            )
        if a == "rename_node":
            await kb.rename_node(args["kb_id"], args["node_id"], args["name"])
            return {"ok": True}
        if a == "move_node":
            await kb.move_node(
                args["kb_id"], args["node_id"], args.get("parent_id", "")
            )
            return {"ok": True}
        if a == "delete_node":
            await kb.delete_node(args["kb_id"], args["node_id"])
            return {"ok": True}
        if a == "download_node":
            return await kb.download_node(
                args["kb_id"], args["node_id"], inline=bool(args.get("inline"))
            )
        if a == "kb_create":
            return await kb.create_kb(
                args.get("body") or {"name": args.get("name", "")}
            )
        if a == "page_update":
            return await kb.update_page(args["page_id"], args.get("body") or {})
        if a == "page_delete":
            await kb.delete_page_permanently(args["page_id"])
            return {"ok": True, "warning": "permanently deleted"}
        if a == "freeze":
            return await kb.freeze_page(args["page_id"])
        if a == "references":
            return await kb.list_page_references(args["page_id"])
        if a == "create_file":
            return await kb.create_file_node(
                args["kb_id"],
                args["name"],
                args["artifact_id"],
                args.get("parent_id", ""),
            )
        if a == "batch_download":
            return await kb.batch_download(
                args["kb_id"],
                list(args.get("node_ids") or []),
                inline=bool(args.get("inline")),
            )
        return {"error": f"unknown action {a!r}"}

    return _run(go)


# -- workspace_members ---------------------------------------------------------

MEMBERS_SCHEMA = {
    "name": "workspace_members",
    "description": (
        "OpenMax workspace directory. Actions: me, list(kind=human|agent, search?), "
        "get(member_id), create_dm(peer_member_id), "
        "dm_policy(policy?), dm_list, dm_allow(member_ids), dm_revoke(member_ids), "
        "agent_profiles(project_id?) -> capability profiles (skills/tags/online) — "
        "MUST consult before assigning work to agents, rename(name), orgs, roles, "
        "frontend_url(path e.g. 'projects?project=X') -> clickable workspace link"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "member_id": {"type": "string"},
            "member_ids": {"type": "array", "items": {"type": "string"}},
            "include": {"type": "array", "items": {"type": "string"}},
            "capabilities": {"type": "boolean"},
            "org_id": {"type": "string"},
            "invitation_id": {"type": "string"},
            "identity_id": {"type": "string"},
            "token": {"type": "string"},
            "status": {"type": "string"},
            "page": {"type": "integer"},
            "page_size": {"type": "integer"},
            "order_by": {"type": "string"},
            "body": {"type": "object"},
            "peer_member_id": {"type": "string"},
            "kind": {"type": "string"},
            "search": {"type": "string"},
            "name": {"type": "string"},
            "project_id": {"type": "string"},
            "path": {"type": "string"},
            "policy": {"type": "string", "enum": ["open", "allowlist", "owner"]},
        },
        "required": ["action"],
    },
}


def handle_members(args: dict, **kw: Any) -> str:
    async def go(svc):
        core, comm, policy = svc["core"], svc["comm"], svc["policy"]
        a = args.get("action")
        if a == "me":
            return await core.me()
        if a == "list":
            return await core.list_members(
                kind=args.get("kind"), search=args.get("search")
            )
        if a == "get":
            return await core.get_member(args["member_id"])
        if a == "create_dm":
            return await comm.create_dm(args["peer_member_id"])
        if a == "dm_policy":
            return (
                policy.set_dm_policy(args["policy"])
                if args.get("policy")
                else policy.get_dm_access()
            )
        if a == "dm_list":
            return policy.get_dm_access()
        if a == "dm_allow":
            return policy.allow_dm_members(
                list(
                    args.get("member_ids")
                    or ([args["member_id"]] if args.get("member_id") else [])
                )
            )
        if a == "dm_revoke":
            return policy.revoke_dm_members(
                list(
                    args.get("member_ids")
                    or ([args["member_id"]] if args.get("member_id") else [])
                )
            )
        if a == "orgs":
            return await core.list_organizations()
        if a == "roles":
            return await core.list_roles()
        if a == "org_get":
            return await core.get_organization(args["org_id"])
        if a == "org_create":
            return await core.create_organization(args.get("body") or {})
        if a == "org_switch":
            return await core.switch_organization(args["org_id"])
        if a == "invitation_create":
            return await core.create_invitation(args.get("body") or {})
        if a == "invitation_list":
            return await core.list_invitations(
                status=args.get("status"),
                page=args.get("page"),
                page_size=args.get("page_size"),
                order_by=args.get("order_by"),
            )
        if a == "invitation_accept":
            return await core.accept_invitation(args["invitation_id"], args["token"])
        if a == "invitation_revoke":
            await core.revoke_invitation(args["invitation_id"])
            return {"ok": True}
        if a == "onboarding_session":
            return await core.get_onboarding_session()
        if a == "onboarding_event":
            return await core.report_onboarding_event(args.get("body") or {})
        if a == "platform_agent_create":
            return await core.create_platform_agent(args.get("body") or {})
        if a == "platform_agent_delete":
            await core.delete_platform_agent(args["member_id"])
            return {"ok": True}
        if a == "agent_domain":
            return await core.get_agent_domain(args["identity_id"])
        if a == "frontend_url":
            import os

            base = os.getenv("CWS_BFF_URL", "").rstrip("/")
            path = args.get("path", "").lstrip("/")
            return {"url": f"{base}/workspace/{path}"}
        if a == "agent_profiles":
            return await core.list_agent_profiles(
                project_id=args.get("project_id", ""),
                member_id=args.get("member_id", ""),
                member_ids=args.get("member_ids"),
                include=args.get("include"),
                capabilities=bool(args.get("capabilities")),
            )
        if a == "rename":
            return await core.set_display_name(args["name"])
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
        "proactively message any conversation (e.g. report to your owner). "
        "send_attachment(conversation_id, local_path, caption?, reply_to?) uploads, "
        "finalizes, and sends a native attachment; upload URLs stay internal."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "conversation_id": {"type": "string"},
            "text": {"type": "string"},
            "reply_to": {"type": "string"},
            "message_id": {"type": "string"},
            "seq": {"type": "integer"},
            "cursor": {"type": "string"},
            "after_seq": {"type": "integer"},
            "before_seq": {"type": "integer"},
            "include_archived": {"type": "boolean"},
            "name": {"type": "string"},
            "member_ids": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
            "local_path": {"type": "string"},
            "caption": {"type": "string"},
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
            return await comm.list_conversations(
                limit=limit,
                cursor=args.get("cursor"),
                include_archived=bool(args.get("include_archived")),
            )
        if a == "history":
            return await comm.list_messages(
                args["conversation_id"],
                limit=limit,
                after_seq=args.get("after_seq"),
                before_seq=args.get("before_seq"),
            )
        if a == "get_conversation":
            return await comm.get_conversation(args["conversation_id"])
        if a == "get_message":
            return await comm.get_message(args["conversation_id"], args["message_id"])
        if a == "unread":
            return await comm.get_unread(args["conversation_id"])
        if a == "mark_read":
            return {
                "read_until_seq": await comm.mark_read(
                    args["conversation_id"], args["seq"]
                )
            }
        if a == "send":
            r = await comm.send_message(
                args["conversation_id"], args["text"], reply_to=args.get("reply_to")
            )
            return {"sent": True, "message_id": r.message_id}
        if a == "send_attachment":
            return await comm.send_local_attachment(
                args["conversation_id"],
                args["local_path"],
                caption=args.get("caption", ""),
                reply_to=args.get("reply_to"),
            )
        if a == "create_group":
            return await comm.create_group(
                args["name"],
                list(args.get("member_ids") or []),
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
            "artifact_id": {"type": "string"},
            "uri": {"type": "string"},
            "inline": {"type": "boolean"},
            "filename": {"type": "string"},
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
            return await artifacts.resolve_uris(
                list(args.get("uris") or []), inline=bool(args.get("inline"))
            )
        if a == "url":
            return await artifacts.get_url(
                args.get("uri") or args["artifact_id"], inline=bool(args.get("inline"))
            )
        if a == "download":
            meta = await artifacts.get_url(args.get("uri") or args["artifact_id"])
            filename = args.get("filename") or meta.get("name") or "artifact"
            return {
                "local_path": await artifacts.download(
                    meta["url"], filename, storage=kw.get("storage")
                )
            }
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
                    fname, ctype, size, parent_id=args.get("parent_id", "")
                )
            if not prep.get("instant_upload"):
                with open(path, "rb") as fh:
                    async with httpx.AsyncClient(timeout=300) as up:
                        resp = await up.put(
                            prep["upload_url"],
                            content=fh.read(),
                            headers=prep.get("headers") or {},
                        )
                        resp.raise_for_status()
            if a == "upload":
                return await artifacts.finalize_conversation_upload(
                    prep["upload_token"]
                )
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

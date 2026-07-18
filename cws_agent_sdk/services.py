"""REST service clients over the cws-core BFF.

MVP scope: the comm surface (messages / read / sync). tm/kb/as clients can be
added behind the same CwsHttpClient later.
"""

from __future__ import annotations

from typing import Any, Optional

from .codec import new_client_msg_id
from .http import CwsHttpClient
from .types import SendReceipt


def _params(**values: Any) -> dict[str, Any]:
    return {
        key: value for key, value in values.items() if value is not None and value != ""
    }


class AccessPolicyService:
    """Local DM policy/allowlist management, matching zylos-openmax."""

    _KEY = "policy.json"
    _POLICIES = ("open", "allowlist", "owner")

    def __init__(self, storage):
        self._storage = storage

    def _state(self) -> dict:
        state = self._storage.read_json(self._KEY)
        return dict(state) if isinstance(state, dict) else {}

    def _write(self, state: dict) -> dict:
        state["dm_policy"] = str(state.get("dm_policy") or "owner")
        state["dm_allowlist"] = [str(v) for v in state.get("dm_allowlist") or []]
        self._storage.write_json(self._KEY, state)
        return {"dm_policy": state["dm_policy"], "dm_allowlist": state["dm_allowlist"]}

    def get_dm_access(self) -> dict:
        state = self._state()
        return {
            "dm_policy": str(state.get("dm_policy") or "owner"),
            "dm_allowlist": [str(v) for v in state.get("dm_allowlist") or []],
        }

    def set_dm_policy(self, policy: str) -> dict:
        policy = str(policy or "").lower()
        if policy not in self._POLICIES:
            raise ValueError("policy must be one of: open, allowlist, owner")
        state = self._state()
        state["dm_policy"] = policy
        return self._write(state)

    def allow_dm_members(self, member_ids: list[str]) -> dict:
        ids = [str(v) for v in member_ids if str(v)]
        if not ids:
            raise ValueError("member_ids must contain at least one member")
        state = self._state()
        state["dm_allowlist"] = list(
            dict.fromkeys([str(v) for v in state.get("dm_allowlist") or []] + ids)
        )
        return self._write(state)

    def revoke_dm_members(self, member_ids: list[str]) -> dict:
        ids = {str(v) for v in member_ids if str(v)}
        if not ids:
            raise ValueError("member_ids must contain at least one member")
        state = self._state()
        state["dm_allowlist"] = [
            str(v) for v in state.get("dm_allowlist") or [] if str(v) not in ids
        ]
        return self._write(state)


class CommService:
    def __init__(self, http: CwsHttpClient):
        self._http = http

    # -- messages --------------------------------------------------------

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        priority: int = 3,
        client_msg_id: Optional[str] = None,
    ) -> SendReceipt:
        from .codec import looks_like_markdown

        # zylos-openmax parity: agent outbound text is AGENT_TEXT, and the FE
        # renders markdown only when content_type says so.
        content_type = "markdown" if looks_like_markdown(text) else "text"
        body: dict[str, Any] = {
            "client_msg_id": client_msg_id or new_client_msg_id(),
            "type": "AGENT_TEXT",
            "content": {
                "content_type": content_type,
                "body": {"text": text},
                "attachments": [],
            },
            "priority": priority,
        }
        if reply_to:
            body["parent_id"] = str(reply_to)
        if metadata:
            body["metadata"] = metadata
        data = await self._http.post(
            f"/api/v1/conversations/{conversation_id}/messages", json=body
        )
        return SendReceipt(
            message_id=str(data.get("id", "")),
            conversation_id=str(data.get("conversation_id", conversation_id)),
            raw=data,
        )

    async def get_message(self, conversation_id: str, message_id: str | int) -> dict:
        """Returns {message: {...}, content?: {...}, inbox_seq?}."""
        return await self._http.get(
            f"/api/v1/conversations/{conversation_id}/messages/{message_id}"
        )

    async def list_messages(
        self,
        conversation_id: str,
        *,
        after_seq: Optional[int] = None,
        before_seq: Optional[int] = None,
        limit: int = 20,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if after_seq is not None:
            params["after_seq"] = after_seq
        if before_seq is not None:
            params["before_seq"] = before_seq
        items, _ = await self._http.get_page(
            f"/api/v1/conversations/{conversation_id}/messages", params=params
        )
        return items

    async def send_image_message(
        self,
        conversation_id: str,
        *,
        artifact_id: str,
        file_name: str,
        content_type: str,
        size_bytes: int,
        caption: str = "",
        reply_to: Optional[str] = None,
        client_msg_id: Optional[str] = None,
    ) -> SendReceipt:
        """Send a native IMAGE message referencing a finalized upload."""
        # zylos-openmax hard-won contract: body MUST carry file_name (an empty
        # body renders as a blank bubble in cws-fe); caption goes in body.text
        # so image + caption arrive as ONE message.
        content_body: dict[str, Any] = {"file_name": file_name}
        if caption:
            content_body["text"] = caption
        body: dict[str, Any] = {
            "client_msg_id": client_msg_id or new_client_msg_id(),
            "type": "IMAGE",
            "content": {
                "content_type": "image",
                "body": content_body,
                "attachments": [
                    {
                        "artifact_id": artifact_id,
                        "file_name": file_name,
                        "content_type": content_type,
                        "size_bytes": int(size_bytes),
                    }
                ],
            },
            "priority": 3,
        }
        if reply_to:
            body["parent_id"] = str(reply_to)
        data = await self._http.post(
            f"/api/v1/conversations/{conversation_id}/messages", json=body
        )
        return SendReceipt(
            message_id=str(data.get("id", "")),
            conversation_id=str(data.get("conversation_id", conversation_id)),
            raw=data,
        )

    async def edit_message(self, message_id: str | int, text: str) -> dict:
        """Replace own message content (15-min server-side edit window)."""
        from .codec import looks_like_markdown

        return await self._http.request(
            "PUT",
            f"/api/v1/messages/{message_id}",
            json={
                "content": {
                    "content_type": "markdown" if looks_like_markdown(text) else "text",
                    "body": {"text": text},
                }
            },
        )

    # -- reactions ---------------------------------------------------------

    async def add_reaction(self, message_id: str | int, reaction_code: str) -> None:
        """Idempotent. Codes from the server registry: thumbs_up, smile, heart,
        tada, eyes, joy, fire, white_check_mark, ok_hand, x, hourglass, warning."""
        await self._http.post(
            f"/api/v1/messages/{message_id}/reactions",
            json={"reaction_code": reaction_code},
        )

    async def remove_reaction(self, message_id: str | int, reaction_code: str) -> None:
        await self._http.request(
            "DELETE", f"/api/v1/messages/{message_id}/reactions/{reaction_code}"
        )

    # -- cursors -----------------------------------------------------------

    async def mark_read(self, conversation_id: str, read_until_seq: int) -> int:
        data = await self._http.post(
            f"/api/v1/conversations/{conversation_id}/read",
            json={"read_until_seq": int(read_until_seq)},
        )
        return int(data.get("read_until_seq", read_until_seq))

    async def get_unread(self, conversation_id: str) -> dict:
        return await self._http.get(f"/api/v1/conversations/{conversation_id}/unread")

    async def send_attachment(
        self,
        conversation_id: str,
        *,
        artifact_id: str,
        file_name: str,
        content_type: str,
        size_bytes: int,
        caption: str = "",
        reply_to: Optional[str] = None,
        client_msg_id: Optional[str] = None,
    ) -> SendReceipt:
        kind = "IMAGE" if content_type.startswith("image/") else "FILE"
        body: dict[str, Any] = {
            "client_msg_id": client_msg_id or new_client_msg_id(),
            "type": kind,
            "content": {
                "content_type": kind.lower(),
                "body": {
                    "file_name": file_name,
                    **({"text": caption} if caption else {}),
                },
                "attachments": [
                    {
                        "artifact_id": artifact_id,
                        "file_name": file_name,
                        "content_type": content_type,
                        "size_bytes": int(size_bytes),
                    }
                ],
            },
            "priority": 3,
        }
        if reply_to:
            body["parent_id"] = str(reply_to)
        data = await self._http.post(
            f"/api/v1/conversations/{conversation_id}/messages", json=body
        )
        return SendReceipt(
            str(data.get("id", "")),
            str(data.get("conversation_id", conversation_id)),
            data,
        )

    async def send_local_attachment(
        self,
        conversation_id: str,
        local_path: str,
        *,
        caption: str = "",
        reply_to: Optional[str] = None,
    ) -> dict:
        """Upload, finalize, and send a local file; hide upload credentials."""
        import mimetypes
        import os
        import httpx

        if not os.path.isfile(local_path):
            raise ValueError(f"file not found: {local_path}")
        file_name = os.path.basename(local_path)
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        size_bytes = os.path.getsize(local_path)
        prep = await self._http.post(
            f"/api/v1/conversations/{conversation_id}/uploads/prepare",
            json={
                "filename": file_name,
                "content_type": content_type,
                "size_bytes": size_bytes,
            },
        )
        if not prep.get("instant_upload"):
            upload_url = prep.get("upload_url")
            if not upload_url:
                raise ValueError("uploads/prepare returned no upload_url")
            with open(local_path, "rb") as fh:
                async with httpx.AsyncClient(timeout=300) as upload:
                    response = await upload.put(
                        upload_url, content=fh.read(), headers=prep.get("headers") or {}
                    )
                    response.raise_for_status()
        finalized = await self._http.post(
            "/api/v1/conversations/uploads/finalize",
            json={"upload_token": prep["upload_token"]},
        )
        artifact_id = str(
            finalized.get("artifact_id") or finalized.get("media_id") or ""
        )
        receipt = await self.send_attachment(
            conversation_id,
            artifact_id=artifact_id,
            file_name=file_name,
            content_type=content_type,
            size_bytes=size_bytes,
            caption=caption,
            reply_to=reply_to,
        )
        return {
            "sent": True,
            "message_id": receipt.message_id,
            "conversation_id": receipt.conversation_id,
            "artifact_id": artifact_id,
            "file_name": file_name,
            "content_type": content_type,
            "size_bytes": size_bytes,
        }

    # -- sync (offline compensation) ----------------------------------------

    async def sync(self, since_seq: int, device_id: str, limit: int = 100) -> dict:
        """Returns {events: [{seq, conversation_id, message_id, timestamp}],
        next_cursor, has_more}. BFF returns next_cursor as a decimal string."""
        return await self._http.post(
            "/api/v1/sync",
            json={"since_seq": int(since_seq), "device_id": device_id, "limit": limit},
        )

    async def sync_ack(self, device_id: str, seq: int, app_version: str = "") -> None:
        await self._http.post(
            "/api/v1/sync/ack",
            json={
                "device_id": device_id,
                "seq": int(seq),
                "platform": "agent",
                "app_version": app_version,
            },
        )

    # -- conversations -------------------------------------------------------

    async def get_conversation(self, conversation_id: str) -> dict:
        return await self._http.get(f"/api/v1/conversations/{conversation_id}")

    async def create_dm(self, peer_member_id: str) -> dict:
        """Get-or-create the DM with another member (idempotent per pair)."""
        return await self._http.post(
            "/api/v1/conversations/dm", json={"peer_member_id": peer_member_id}
        )

    async def list_conversations(
        self,
        limit: int = 50,
        *,
        cursor: Optional[str] = None,
        include_archived: bool = False,
        search_name: str = "",
    ) -> list[dict]:
        """Rows are userConversationItem: {conversation, unread_count,
        unread_mention, is_pinned, is_muted, last_read_seq, ...}."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if include_archived:
            params["include_archived"] = True
        if search_name:
            params["search_name"] = search_name
        items, _ = await self._http.get_page("/api/v1/conversations", params=params)
        return items

    async def create_group(
        self,
        name: str,
        member_ids: list[str],
        *,
        description: str = "",
        metadata: Optional[dict] = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name, "member_ids": member_ids}
        if description:
            body["description"] = description
        if metadata:
            body["metadata"] = metadata
        return await self._http.post("/api/v1/conversations/groups", json=body)


class CoreService:
    """Directory / identity (cws-core)."""

    def __init__(self, http: CwsHttpClient):
        self._http = http

    async def me(self) -> dict:
        """{identity_id, kind, member_id, org_id, display_name, role, ...}"""
        return await self._http.get("/api/v1/me")

    async def get_member(self, member_id: str) -> dict:
        """Includes owner_member_id / agent_origin / online_status for agents."""
        return await self._http.get(f"/api/v1/members/{member_id}")

    async def list_members(
        self,
        *,
        kind: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if kind:
            params["kind"] = kind
        if search:
            params["search"] = search
        items, _ = await self._http.get_page("/api/v1/members", params=params)
        return items

    async def set_display_name(self, display_name: str) -> dict:
        return await self._http.request(
            "PATCH", "/api/v1/me", json={"display_name": display_name}
        )

    async def list_agent_profiles(
        self,
        *,
        project_id: str = "",
        member_id: str = "",
        member_ids: Optional[list[str]] = None,
        include: Optional[list[str]] = None,
        capabilities: bool = False,
        limit: Optional[int] = None,
    ) -> list[dict]:
        ids = list(member_ids or ([] if not member_id else [member_id]))
        if not project_id and not ids:
            raise ValueError("agent_profiles requires an explicit project/member scope")
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        if ids:
            params["member_id"] = ids
        includes = list(include or [])
        if capabilities and "capabilities" not in includes:
            includes.append("capabilities")
        if includes:
            params["include"] = includes
        if limit is not None:
            params["limit"] = limit
        items, _ = await self._http.get_page("/api/v1/agent-profiles", params=params)
        return items

    async def list_organizations(self) -> list[dict]:
        items, _ = await self._http.get_page("/api/v1/organizations")
        return items

    async def get_organization(self, org_id: str) -> dict:
        return await self._http.get(f"/api/v1/organizations/{org_id}")

    async def create_organization(self, body: dict) -> dict:
        return await self._http.post("/api/v1/organizations", json=body)

    async def switch_organization(self, org_id: str) -> dict:
        return await self._http.post(f"/api/v1/organizations/{org_id}/switch", json={})

    async def list_roles(self) -> list[dict]:
        items, _ = await self._http.get_page("/api/v1/roles")
        return items

    async def create_invitation(self, body: dict) -> dict:
        return await self._http.post("/api/v1/invitations", json=body)

    async def list_invitations(
        self, *, status=None, page=None, page_size=None, order_by=None
    ) -> list[dict]:
        items, _ = await self._http.get_page(
            "/api/v1/invitations",
            params=_params(
                status=status, page=page, page_size=page_size, order_by=order_by
            ),
        )
        return items

    async def accept_invitation(self, invitation_id: str, token: str) -> dict:
        return await self._http.post(
            f"/api/v1/invitations/{invitation_id}/accept", json={"token": token}
        )

    async def revoke_invitation(self, invitation_id: str) -> None:
        await self._http.request("DELETE", f"/api/v1/invitations/{invitation_id}")

    async def get_onboarding_session(self) -> dict:
        return await self._http.get("/api/v1/onboarding/session")

    async def report_onboarding_event(self, body: dict) -> dict:
        return await self._http.post("/api/v1/onboarding/events", json=body)

    async def create_platform_agent(self, body: dict) -> dict:
        return await self._http.post("/api/v1/platform-agents", json=body)

    async def delete_platform_agent(self, member_id: str) -> None:
        await self._http.request("DELETE", f"/api/v1/platform-agents/{member_id}")

    async def get_agent_domain(self, identity_id: str) -> dict:
        return await self._http.get(f"/api/v1/platform-agents/{identity_id}/domain")


class TmService:
    """Projects / issues / tasks (cws-work via BFF)."""

    def __init__(self, http: CwsHttpClient):
        self._http = http

    async def list_projects(
        self,
        limit: Optional[int] = None,
        *,
        status=None,
        query=None,
        page=None,
        page_size=None,
        order_by=None,
    ) -> list[dict]:
        if page_size is None:
            page_size = limit
        items, _ = await self._http.get_page(
            "/api/v1/projects",
            params=_params(
                status=status,
                query=query,
                page=page,
                page_size=page_size,
                order_by=order_by,
            ),
        )
        return items

    async def create_project(self, body: dict) -> dict:
        return await self._http.post("/api/v1/projects", json=body)

    async def get_project(self, project_id: str) -> dict:
        return await self._http.get(f"/api/v1/projects/{project_id}")

    async def update_project(self, project_id: str, body: dict) -> dict:
        return await self._http.request(
            "PATCH", f"/api/v1/projects/{project_id}", json=body
        )

    async def archive_project(self, project_id: str) -> dict:
        return await self._http.post(f"/api/v1/projects/{project_id}/archive")

    async def list_project_members(self, project_id: str) -> list[dict]:
        items, _ = await self._http.get_page(f"/api/v1/projects/{project_id}/members")
        return items

    async def add_project_member(
        self, project_id: str, member_id: str, *, role: str = "member"
    ) -> dict:
        return await self._http.post(
            f"/api/v1/projects/{project_id}/members",
            json={"member_id": member_id, "role": role},
        )

    async def remove_project_member(self, project_id: str, member_id: str) -> None:
        await self._http.request(
            "DELETE", f"/api/v1/projects/{project_id}/members/{member_id}"
        )

    async def get_task(self, task_id: str) -> dict:
        return await self._http.get(f"/api/v1/tasks/{task_id}")

    async def get_event_binding(self, event_binding_id: str) -> dict:
        return await self._http.get(f"/api/v1/event-bindings/{event_binding_id}")

    async def list_issues(
        self, project_id: Optional[str] = None, limit: Optional[int] = None, **filters
    ) -> list[dict]:
        path = (
            f"/api/v1/projects/{project_id}/issues" if project_id else "/api/v1/issues"
        )
        if filters.get("page_size") is None:
            filters["page_size"] = limit
        items, _ = await self._http.get_page(path, params=_params(**filters))
        return items

    async def get_issue(self, issue_id: str) -> dict:
        return await self._http.get(f"/api/v1/issues/{issue_id}")

    async def create_issue(self, project_id: str, body: dict) -> dict:
        """body requires owner_member_id (only that owner can accept delivery)."""
        return await self._http.post(f"/api/v1/projects/{project_id}/issues", json=body)

    async def update_issue(self, issue_id: str, body: dict) -> dict:
        return await self._http.request(
            "PATCH", f"/api/v1/issues/{issue_id}", json=body
        )

    async def issue_action(
        self, issue_id: str, action: str, body: Optional[dict] = None
    ) -> dict:
        """action: activate | submit-plan | accept-plan | deliver | resume |
        terminate | accept-delivered | reassign-owner | move"""
        return await self._http.post(
            f"/api/v1/issues/{issue_id}/{action}", json=body or {}
        )

    async def list_tasks(
        self, issue_id: Optional[str] = None, limit: Optional[int] = None, **filters
    ) -> list[dict]:
        # Keep the flat collection endpoint so project_id + issue_id + status
        # filters can be combined exactly like zylos tm.js.
        path = "/api/v1/tasks"
        if filters.get("page_size") is None:
            filters["page_size"] = limit
        if issue_id:
            filters["issue_id"] = issue_id
        items, _ = await self._http.get_page(path, params=_params(**filters))
        return items

    async def create_task(self, project_id: str, issue_id: str, body: dict) -> dict:
        return await self._http.post(
            f"/api/v1/projects/{project_id}/issues/{issue_id}/tasks", json=body
        )

    async def task_action(
        self, task_id: str, action: str, body: Optional[dict] = None
    ) -> dict:
        """action: transition | claim | start | reassign"""
        return await self._http.post(
            f"/api/v1/tasks/{task_id}/{action}", json=body or {}
        )

    # -- comments (work_type: issue|task) -----------------------------------

    async def create_comment(
        self, work_type: str, work_id: str, body_markdown: str
    ) -> dict:
        return await self._http.post(
            "/api/v1/comments",
            json={
                "work_type": work_type,
                "work_id": work_id,
                "body_markdown": body_markdown,
            },
        )

    async def list_comments(
        self, work_type: str, work_id: str, limit: int = 50
    ) -> list[dict]:
        items, _ = await self._http.get_page(
            "/api/v1/comments",
            params={"work_type": work_type, "work_id": work_id, "limit": limit},
        )
        return items

    async def get_comment(self, comment_id: str) -> dict:
        return await self._http.get(f"/api/v1/comments/{comment_id}")

    # -- work references (proj://<id> / issue://<id>) -------------------------

    async def work_references(
        self, *, query: str = "", project_id: str = "", limit: int = 20
    ) -> list[dict]:
        """Search projects/issues; rows: {kind, id, label, resource_uri, status, ...}."""
        params: dict[str, Any] = {"limit": limit}
        if query:
            params["query"] = query
        if project_id:
            params["project_id"] = project_id
        data = await self._http.get("/api/v1/work-references", params=params)
        return data if isinstance(data, list) else data.get("items", [])

    # -- blueprints -----------------------------------------------------------

    async def create_blueprint(
        self,
        issue_id: str,
        steps: list[dict],
        *,
        estimated_budget: Any = None,
        notes: str = "",
    ) -> dict:
        body: dict[str, Any] = {"steps": steps}
        if notes:
            body["notes"] = notes
        if estimated_budget is not None:
            body["estimated_budget"] = estimated_budget
        return await self._http.post(f"/api/v1/issues/{issue_id}/blueprints", json=body)

    async def get_blueprint(
        self, blueprint_id: str, *, include_steps: bool = True
    ) -> dict:
        return await self._http.get(
            f"/api/v1/blueprints/{blueprint_id}",
            params={"include_steps": include_steps},
        )

    async def list_blueprints(self, issue_id: str) -> list[dict]:
        items, _ = await self._http.get_page(f"/api/v1/issues/{issue_id}/blueprints")
        return items

    async def submit_blueprint(self, blueprint_id: str) -> dict:
        return await self._http.post(
            f"/api/v1/blueprints/{blueprint_id}/submit-for-approval"
        )

    async def set_blueprint_steps(self, blueprint_id: str, steps: list[dict]) -> dict:
        return await self._http.request(
            "PUT", f"/api/v1/blueprints/{blueprint_id}/steps", json={"steps": steps}
        )

    # -- attempts --------------------------------------------------------------

    async def create_attempt(self, task_id: str) -> dict:
        """Assignee = the authenticated caller; body is empty by contract."""
        return await self._http.post(f"/api/v1/tasks/{task_id}/attempts")

    async def list_attempts(self, task_id: str) -> list[dict]:
        items, _ = await self._http.get_page(f"/api/v1/tasks/{task_id}/attempts")
        return items

    async def get_attempt(self, attempt_id: str) -> dict:
        return await self._http.get(f"/api/v1/attempts/{attempt_id}")

    async def transition_attempt(
        self,
        attempt_id: str,
        target_status: str,
        *,
        failure_reason: str = "",
        blocked_on_approval_request_ids: Optional[list[str]] = None,
    ) -> dict:
        """target_status: done | failed | blocked | cancelled (terminal only)."""
        body: dict[str, Any] = {"target_status": target_status}
        if failure_reason:
            body["failure_reason"] = failure_reason
        if blocked_on_approval_request_ids is not None:
            body["blocked_on_approval_request_ids"] = blocked_on_approval_request_ids
        return await self._http.post(
            f"/api/v1/attempts/{attempt_id}/transition", json=body
        )

    # -- event bindings (v0.7: timer -> create issue from template) -------------

    async def create_event_binding(
        self,
        cron_expr: str,
        lead_member_id: str,
        spec: dict,
        *,
        owner_member_id: str = "",
    ) -> dict:
        body: dict[str, Any] = {
            "cron_expr": cron_expr,
            "lead_member_id": lead_member_id,
            "spec": spec,
        }
        if owner_member_id:
            body["owner_member_id"] = owner_member_id  # required for agent callers
        return await self._http.post("/api/v1/event-bindings", json=body)

    async def list_event_bindings(self) -> list[dict]:
        data = await self._http.get("/api/v1/event-bindings")
        return data if isinstance(data, list) else data.get("items", [])

    async def delete_event_binding(self, event_binding_id: str) -> None:
        await self._http.request("DELETE", f"/api/v1/event-bindings/{event_binding_id}")


class KbService:
    """Knowledge base (cws-kb via BFF)."""

    def __init__(self, http: CwsHttpClient):
        self._http = http

    async def init_kb(self) -> dict:
        return await self._http.post("/api/v1/kbs/init")

    async def get_kb(self, kb_id: str) -> dict:
        return await self._http.get(f"/api/v1/kbs/{kb_id}")

    async def update_kb(self, kb_id: str, body: dict) -> dict:
        return await self._http.request("PATCH", f"/api/v1/kbs/{kb_id}", json=body)

    async def delete_kb(self, kb_id: str) -> None:
        await self._http.request("DELETE", f"/api/v1/kbs/{kb_id}")

    async def archive_kb(self, kb_id: str) -> dict:
        return await self._http.post(f"/api/v1/kbs/{kb_id}/archive")

    async def unarchive_kb(self, kb_id: str) -> dict:
        return await self._http.post(f"/api/v1/kbs/{kb_id}/unarchive")

    async def list_kbs(self, limit: int = 50) -> list[dict]:
        items, _ = await self._http.get_page("/api/v1/kbs", params={"limit": limit})
        return items

    async def create_kb(self, body: dict) -> dict:
        return await self._http.post("/api/v1/kbs", json=body)

    async def update_page(self, page_id: str, body: dict) -> dict:
        """PATCH page metadata (title/parent/etc.)."""
        return await self._http.request("PATCH", f"/api/v1/pages/{page_id}", json=body)

    async def delete_page_permanently(self, page_id: str) -> None:
        await self._http.request("DELETE", f"/api/v1/pages/{page_id}")

    async def list_page_references(self, page_id: str) -> list[dict]:
        items, _ = await self._http.get_page(f"/api/v1/pages/{page_id}/references")
        return items

    async def create_file_node(
        self, kb_id: str, name: str, artifact_id: str, parent_id: str = ""
    ) -> dict:
        body: dict[str, Any] = {"name": name, "artifact_id": artifact_id}
        if parent_id:
            body["parent_id"] = parent_id
        return await self._http.post(f"/api/v1/kbs/{kb_id}/tree/files", json=body)

    async def batch_download(
        self, kb_id: str, node_ids: list[str], *, inline: bool = False
    ) -> list[dict]:
        data = await self._http.post(
            f"/api/v1/kbs/{kb_id}/tree/files/batch-download",
            json={"node_ids": node_ids, "inline": inline},
        )
        return data if isinstance(data, list) else data.get("items", [])

    async def search_pages(
        self,
        query: str,
        *,
        kb_id: Optional[str] = None,
        limit: int = 20,
        offset=None,
        sort=None,
    ) -> list[dict]:
        params: dict[str, Any] = _params(
            query=query, limit=limit, offset=offset, sort=sort
        )
        if kb_id:
            params["kb_id"] = kb_id
        items, _ = await self._http.get_page("/api/v1/search/pages", params=params)
        return items

    async def create_page(self, kb_id: str, body: dict) -> dict:
        return await self._http.post(f"/api/v1/kbs/{kb_id}/pages", json=body)

    async def get_page(self, page_id: str) -> dict:
        return await self._http.get(f"/api/v1/pages/{page_id}")

    async def get_page_content(self, page_id: str) -> dict:
        return await self._http.get(f"/api/v1/pages/{page_id}/content")

    async def put_page_content(self, page_id: str, body: dict) -> dict:
        return await self._http.request(
            "PUT", f"/api/v1/pages/{page_id}/content", json=body
        )

    async def get_tree(self, kb_id: str) -> dict:
        return await self._http.get(f"/api/v1/kbs/{kb_id}/tree/roots")

    async def get_node(self, kb_id: str, node_id: str) -> dict:
        return await self._http.get(f"/api/v1/kbs/{kb_id}/tree/nodes/{node_id}")

    async def get_breadcrumb(self, kb_id: str, node_id: str) -> dict:
        return await self._http.get(
            f"/api/v1/kbs/{kb_id}/tree/nodes/{node_id}/breadcrumb"
        )

    async def list_children(self, kb_id: str, node_id: str) -> dict:
        return await self._http.get(
            f"/api/v1/kbs/{kb_id}/tree/nodes/{node_id}/children"
        )

    async def preview_node(self, kb_id: str, node_id: str) -> dict:
        return await self._http.get(f"/api/v1/kbs/{kb_id}/tree/nodes/{node_id}/preview")

    async def list_pages(self, *, cursor=None, limit=20, offset=None) -> list[dict]:
        items, _ = await self._http.get_page(
            "/api/v1/pages", params=_params(cursor=cursor, limit=limit, offset=offset)
        )
        return items

    # -- revisions --------------------------------------------------------------

    async def list_revisions(self, page_id: str, limit: int = 20) -> list[dict]:
        items, _ = await self._http.get_page(
            f"/api/v1/pages/{page_id}/revisions", params={"limit": limit}
        )
        return items

    async def get_revision(self, page_id: str, revision_id: int) -> dict:
        return await self._http.get(f"/api/v1/pages/{page_id}/revisions/{revision_id}")

    async def diff_revisions(
        self, page_id: str, from_revision: int, to_revision: int
    ) -> dict:
        return await self._http.get(
            f"/api/v1/pages/{page_id}/revisions/diff",
            params={"from_revision": from_revision, "to_revision": to_revision},
        )

    async def restore_revision(self, page_id: str, revision_id: int) -> dict:
        return await self._http.post(
            f"/api/v1/pages/{page_id}/revisions/{revision_id}/restore"
        )

    # -- trash / lifecycle -------------------------------------------------------

    async def trash_page(self, page_id: str) -> dict:
        return await self._http.post(f"/api/v1/pages/{page_id}/trash")

    async def restore_page(self, page_id: str) -> dict:
        return await self._http.post(f"/api/v1/pages/{page_id}/restore")

    async def list_trashed(self, limit: int = 50) -> list[dict]:
        items, _ = await self._http.get_page(
            "/api/v1/pages/trashed", params={"limit": limit}
        )
        return items

    async def freeze_page(self, page_id: str) -> dict:
        return await self._http.post(f"/api/v1/pages/{page_id}/freeze")

    # -- tree node ops ------------------------------------------------------------

    async def create_folder(self, kb_id: str, name: str, parent_id: str = "") -> dict:
        body: dict[str, Any] = {"name": name}
        if parent_id:
            body["parent_id"] = parent_id
        return await self._http.post(f"/api/v1/kbs/{kb_id}/tree/folders", json=body)

    async def rename_node(self, kb_id: str, node_id: str, name: str) -> None:
        await self._http.request(
            "PATCH",
            f"/api/v1/kbs/{kb_id}/tree/nodes/{node_id}/rename",
            json={"name": name},
        )

    async def move_node(self, kb_id: str, node_id: str, parent_id: str = "") -> None:
        body = {"parent_id": parent_id} if parent_id else {}
        await self._http.post(
            f"/api/v1/kbs/{kb_id}/tree/nodes/{node_id}/move", json=body
        )

    async def delete_node(self, kb_id: str, node_id: str) -> None:
        await self._http.request("DELETE", f"/api/v1/kbs/{kb_id}/tree/nodes/{node_id}")

    async def download_node(
        self, kb_id: str, node_id: str, *, inline: bool = False
    ) -> dict:
        """Returns {node_id, name, download_url(presigned), content_type, ...}."""
        return await self._http.get(
            f"/api/v1/kbs/{kb_id}/tree/nodes/{node_id}/download",
            params={"inline": inline},
        )


class AsService:
    """Artifacts: presigned two-phase upload + URI resolution (cws-as/kb/comm)."""

    def __init__(self, http: CwsHttpClient):
        self._http = http

    async def resolve_uris(self, uris: list[str], *, inline: bool = False) -> dict:
        """Returns {resolved: {uri: {download_url, expires_at, ...}}, failed: [...]}."""
        return await self._http.post(
            "/api/v1/artifacts/resolve", json={"uris": uris, "inline": inline}
        )

    async def get_url(self, artifact_id_or_uri: str, *, inline: bool = False) -> dict:
        uri = (
            artifact_id_or_uri
            if artifact_id_or_uri.startswith("artifact://")
            else f"artifact://{artifact_id_or_uri}"
        )
        data = await self.resolve_uris([uri], inline=inline)
        entry = (data.get("resolved") or {}).get(uri)
        if not entry or not entry.get("download_url"):
            raise ValueError(f"artifact not resolvable: {uri}")
        return {
            "url": entry["download_url"],
            "expires_at": entry.get("expires_at"),
            "content_type": entry.get("content_type"),
            "content_length": entry.get("content_length"),
            "name": entry.get("name"),
        }

    async def prepare_kb_upload(
        self, filename: str, content_type: str, size_bytes: int, parent_id: str = ""
    ) -> dict:
        """Returns {upload_token, upload_url, headers, expires_at, instant_upload}."""
        return await self._http.post(
            "/api/v1/uploads/prepare",
            json={
                **({"parent_id": parent_id} if parent_id else {}),
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
            },
        )

    async def finalize_kb_upload(self, upload_token: str) -> dict:
        return await self._http.post(
            "/api/v1/uploads/finalize", json={"upload_token": upload_token}
        )

    async def prepare_conversation_upload(
        self, conversation_id: str, filename: str, content_type: str, size_bytes: int
    ) -> dict:
        return await self._http.post(
            f"/api/v1/conversations/{conversation_id}/uploads/prepare",
            json={
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
            },
        )

    async def finalize_conversation_upload(self, upload_token: str) -> dict:
        return await self._http.post(
            "/api/v1/conversations/uploads/finalize",
            json={"upload_token": upload_token},
        )

    async def download(self, url: str, filename: str, *, storage=None) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url)
            response.raise_for_status()
        if storage is None:
            return ""
        relative = f"media/{filename}"
        storage.write(relative, response.content)
        return storage.path_for(relative) if hasattr(storage, "path_for") else ""


class ConnService:
    """Channel/tool credentials (cws-connect via BFF)."""

    def __init__(self, http: CwsHttpClient):
        self._http = http

    async def list_agent_connections(self, agent_member_id: str) -> list[dict]:
        items, _ = await self._http.get_page(
            f"/api/v1/connect/agents/{agent_member_id}/connections"
        )
        return items

    async def acquire_credential(
        self, connection_id: str, agent_member_id: str
    ) -> dict:
        """Returns {credential_mode, access_token, token_type, expires_at, proxy_*, toolkits}."""
        return await self._http.post(
            f"/api/v1/connect/connections/{connection_id}/credential",
            json={"agent_member_id": agent_member_id},
        )

    async def execute_action(self, connection_id: str, body: dict) -> dict:
        return await self._http.post(
            f"/api/v1/connect/connections/{connection_id}/actions/execute", json=body
        )

    async def pull_binding_credential(self, binding_id: str, pull_token: str) -> dict:
        return await self._http.get(
            f"/api/v1/connect/channel-bindings/{binding_id}/credential",
            params={"pull_token": pull_token},
        )

    async def report_binding_result(self, binding_id: str, body: dict) -> dict:
        return await self._http.post(
            f"/api/v1/connect/channel-bindings/{binding_id}/result", json=body
        )

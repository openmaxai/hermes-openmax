"""REST service clients over the cws-core BFF.

MVP scope: the comm surface (messages / read / sync). tm/kb/as clients can be
added behind the same CwsHttpClient later.
"""
from __future__ import annotations

from typing import Any, Optional

from .codec import new_client_msg_id
from .http import CwsHttpClient
from .types import SendReceipt


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
        body: dict[str, Any] = {
            "client_msg_id": client_msg_id or new_client_msg_id(),
            "type": "TEXT",
            # NOTE: body shape pending verification against a live env — the
            # BFF schema only constrains {content_type, body(map)}; adjust the
            # inner key here if the workspace FE expects a different one.
            "content": {"content_type": "text", "body": {"text": text}},
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

    # -- cursors -----------------------------------------------------------

    async def mark_read(self, conversation_id: str, read_until_seq: int) -> int:
        data = await self._http.post(
            f"/api/v1/conversations/{conversation_id}/read",
            json={"read_until_seq": int(read_until_seq)},
        )
        return int(data.get("read_until_seq", read_until_seq))

    # -- sync (offline compensation) ----------------------------------------

    async def sync(self, since_seq: int, device_id: str, limit: int = 100) -> dict:
        """Returns {events: [{seq, conversation_id, message_id, timestamp}],
        next_cursor, has_more}. BFF returns next_cursor as a decimal string."""
        return await self._http.post(
            "/api/v1/sync",
            json={"since_seq": int(since_seq), "device_id": device_id, "limit": limit},
        )

    async def sync_ack(self, device_id: str, seq: int) -> None:
        await self._http.post(
            "/api/v1/sync/ack",
            json={"device_id": device_id, "seq": int(seq), "platform": "agent"},
        )

    # -- conversations -------------------------------------------------------

    async def get_conversation(self, conversation_id: str) -> dict:
        return await self._http.get(f"/api/v1/conversations/{conversation_id}")

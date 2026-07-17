"""Token lifecycle: api_key -> JWT (15 min) + refresh token (7 d) + ws-ticket (30 s).

Contract (cws-core internal/transport/http/auth.go):
  POST /auth/register/agent            {}                -> {identity_id, api_key}
  POST /auth/agent/token   Bearer cwsk {org_id?}         -> {access_token, refresh_token, *_expires_at}
  POST /auth/refresh                   {refresh_token, org_id?} -> same shape
  POST /auth/ws-ticket     Bearer JWT  {}                -> {ticket, expires_at}   (one-shot, 30 s)
All responses are D8 envelopes; payload lives under "data".
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

from .config import CwsConfig
from .errors import CwsApiError, CwsAuthError
from .providers import FileStorage, Logger, StdLogger

_TOKENS_KEY = "tokens.json"
# Refresh the access token this many seconds before its 15-min expiry.
_EARLY_REFRESH_S = 60


def _unwrap_d8(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code // 100 == 2:
        return (body or {}).get("data", body)
    err = (body or {}).get("error") or {}
    msg = err.get("detail") or err.get("title") or resp.text[:200]
    code = err.get("code") or ""
    if resp.status_code in (401, 403):
        raise CwsAuthError(f"{resp.status_code} {code}: {msg}", status=resp.status_code, body=body)
    raise CwsApiError(f"{resp.status_code} {code}: {msg}", status=resp.status_code, body=body)


class TokenManager:
    """Owns the JWT/refresh pair for one (api_key, org) and persists it."""

    def __init__(
        self,
        cfg: CwsConfig,
        storage: Optional[FileStorage] = None,
        logger: Optional[Logger] = None,
        http: Optional[httpx.AsyncClient] = None,
    ):
        self._cfg = cfg
        self._storage = storage
        self._log = logger or StdLogger("[cws-token]")
        self._http = http or httpx.AsyncClient(timeout=cfg.request_timeout_s)
        self._lock = asyncio.Lock()
        self._access_token: str = ""
        self._access_expires_at: float = 0.0
        self._refresh_token: str = ""
        self._load_persisted()

    def _load_persisted(self) -> None:
        if not self._storage:
            return
        saved = self._storage.read_json(_TOKENS_KEY)
        if isinstance(saved, dict) and saved.get("org_id") == self._cfg.org_id:
            self._access_token = saved.get("access_token", "")
            self._access_expires_at = float(saved.get("access_expires_at", 0))
            self._refresh_token = saved.get("refresh_token", "")

    def _persist(self) -> None:
        if not self._storage:
            return
        self._storage.write_json(
            _TOKENS_KEY,
            {
                "org_id": self._cfg.org_id,
                "access_token": self._access_token,
                "access_expires_at": self._access_expires_at,
                "refresh_token": self._refresh_token,
            },
        )

    # -- public API ------------------------------------------------------

    async def get_access_token(self) -> str:
        async with self._lock:
            if self._access_token and time.time() < self._access_expires_at - _EARLY_REFRESH_S:
                return self._access_token
            if self._refresh_token:
                try:
                    await self._refresh()
                    return self._access_token
                except (CwsAuthError, CwsApiError) as exc:
                    self._log.warn("refresh failed, falling back to api-key exchange:", exc)
            await self._exchange()
            return self._access_token

    async def get_ws_ticket(self) -> str:
        token = await self.get_access_token()
        resp = await self._http.post(
            f"{self._cfg.bff_url}/auth/ws-ticket",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = _unwrap_d8(resp)
        return data["ticket"]

    def invalidate(self) -> None:
        """Drop the cached access token (e.g. after WS close 4003)."""
        self._access_token = ""
        self._access_expires_at = 0.0
        self._persist()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- internals ---------------------------------------------------------

    async def _exchange(self) -> None:
        body: dict = {}
        if self._cfg.org_id:
            body["org_id"] = self._cfg.org_id
        resp = await self._http.post(
            f"{self._cfg.bff_url}/auth/agent/token",
            headers={"Authorization": f"Bearer {self._cfg.api_key}"},
            json=body,
        )
        self._store(_unwrap_d8(resp))
        self._log.log("exchanged api key for JWT")

    async def _refresh(self) -> None:
        body: dict = {"refresh_token": self._refresh_token}
        if self._cfg.org_id:
            body["org_id"] = self._cfg.org_id
        resp = await self._http.post(f"{self._cfg.bff_url}/auth/refresh", json=body)
        self._store(_unwrap_d8(resp))

    def _store(self, data: dict) -> None:
        self._access_token = data["access_token"]
        expires_at = data.get("access_token_expires_at")
        # Server returns RFC3339; keep a conservative local clock instead of
        # parsing timezone edge cases — access TTL is a fixed 15 min.
        self._access_expires_at = time.time() + 15 * 60
        if expires_at:
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                self._access_expires_at = dt.timestamp()
            except ValueError:
                pass
        if data.get("refresh_token"):
            self._refresh_token = data["refresh_token"]
        self._persist()


async def register_agent(bff_url: str, *, timeout_s: float = 30.0) -> dict:
    """One-shot self-registration. Returns {identity_id, api_key(cwsk_)}.

    The api_key is only ever returned once — persist it immediately.
    """
    async with httpx.AsyncClient(timeout=timeout_s) as http:
        resp = await http.post(f"{bff_url.rstrip('/')}/auth/register/agent", json={})
        return _unwrap_d8(resp)


async def accept_invitation(
    bff_url: str, access_token: str, invitation_id: str, invite_token: str, *, timeout_s: float = 30.0
) -> dict:
    """Accept an org invitation. Returns {member_id, org_id, role_slug}."""
    async with httpx.AsyncClient(timeout=timeout_s) as http:
        resp = await http.post(
            f"{bff_url.rstrip('/')}/api/v1/invitations/{invitation_id}/accept",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"token": invite_token},
        )
        return _unwrap_d8(resp)

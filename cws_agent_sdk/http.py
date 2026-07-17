"""Authenticated REST client for the cws-core BFF (D8 envelope aware).

- Adds Authorization: Bearer <JWT> from TokenManager.
- On 401: invalidates the token, re-acquires, retries the request once.
- Unwraps the D8 envelope ({data, request_id, server_time} / problem+json error).
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from .config import CwsConfig
from .errors import CwsApiError, CwsAuthError
from .providers import Logger, StdLogger
from .token import TokenManager, _unwrap_d8


class CwsHttpClient:
    def __init__(
        self,
        cfg: CwsConfig,
        tokens: TokenManager,
        logger: Optional[Logger] = None,
        http: Optional[httpx.AsyncClient] = None,
    ):
        self._cfg = cfg
        self._tokens = tokens
        self._log = logger or StdLogger("[cws-http]")
        self._http = http or httpx.AsyncClient(timeout=cfg.request_timeout_s)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict] = None,
        unwrap: bool = True,
    ) -> Any:
        url = f"{self._cfg.bff_url}{path}"
        for attempt in (1, 2):
            token = await self._tokens.get_access_token()
            resp = await self._http.request(
                method,
                url,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {token}", **self._cfg.extra_headers},
            )
            if resp.status_code == 401 and attempt == 1:
                self._log.warn("401 on", path, "— refreshing token and retrying")
                self._tokens.invalidate()
                continue
            if not unwrap:
                if resp.status_code // 100 != 2:
                    raise CwsApiError(f"{resp.status_code}: {resp.text[:200]}", status=resp.status_code)
                return resp
            return _unwrap_d8(resp)
        raise CwsAuthError("unreachable", status=401)  # pragma: no cover

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: Any = None) -> Any:
        return await self.request("POST", path, json=json)

    async def get_page(self, path: str, params: Optional[dict] = None) -> tuple[list, dict]:
        """GET a CursorListResponse: returns (items, pagination)."""
        url = f"{self._cfg.bff_url}{path}"
        for attempt in (1, 2):
            token = await self._tokens.get_access_token()
            resp = await self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 401 and attempt == 1:
                self._tokens.invalidate()
                continue
            if resp.status_code // 100 != 2:
                _unwrap_d8(resp)  # raises with parsed D8 error
            body = resp.json()
            return body.get("data") or [], body.get("pagination") or {}
        raise CwsAuthError("unreachable", status=401)  # pragma: no cover

    async def aclose(self) -> None:
        await self._http.aclose()

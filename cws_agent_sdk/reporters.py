"""Reporters & gates: online-report, runtime metrics, billing suspension.

All three follow the same discipline as zylos-openmax: reporter failures must
never break the message path — they log and move on.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from .errors import CwsApiError, CwsAuthError
from .http import CwsHttpClient
from .providers import Logger, StdLogger


@runtime_checkable
class RuntimeStateProvider(Protocol):
    """Optional source for runtime metrics. Return None to skip a report."""

    async def read(self) -> Optional[dict]: ...


class OnlineReporter:
    """POST /api/v1/agents/{member_id}/online-report — once per boot, idempotent.

    Server treats repeat calls as expected input; `triggered` is only true for
    the org's first active agent when onboarding trigger is enabled.
    """

    def __init__(self, http: CwsHttpClient, logger: Optional[Logger] = None):
        self._http = http
        self._log = logger or StdLogger("[cws-online]")

    async def report(self, member_id: str) -> Optional[dict]:
        try:
            data = await self._http.post(f"/api/v1/agents/{member_id}/online-report")
            self._log.log(
                "online-report ok:",
                data.get("reason", "?"),
                "triggered=",
                data.get("triggered"),
            )
            return data
        except (CwsApiError, CwsAuthError) as exc:
            self._log.warn("online-report failed (non-fatal):", exc)
            return None


class MetricsReporter:
    """PUT /api/v1/agents/{member_id}/runtime-metrics on an interval.

    Degrades per the SDK design: without a RuntimeStateProvider only `version`
    is reported; provider errors skip the tick.
    """

    def __init__(
        self,
        http: CwsHttpClient,
        member_id_provider: Callable[[], str],
        *,
        version: str = "",
        runtime_state: Optional[RuntimeStateProvider] = None,
        logger: Optional[Logger] = None,
    ):
        self._http = http
        self._member_id = member_id_provider
        self._version = version
        self._state = runtime_state
        self._log = logger or StdLogger("[cws-metrics]")

    async def report_once(self) -> bool:
        member_id = self._member_id()
        if not member_id:
            return False
        body: dict[str, Any] = {}
        if self._version:
            body["version"] = self._version
        if self._state is not None:
            try:
                snapshot = await self._state.read()
            except Exception as exc:  # noqa: BLE001 — provider must not break reporting
                self._log.warn("runtime-state provider failed, skipping tick:", exc)
                return False
            if snapshot:
                for key in ("resources", "runtime", "cost", "rate_limit_pct"):
                    if snapshot.get(key) is not None:
                        body[key] = snapshot[key]
        if not body:
            return False
        try:
            await self._http.request(
                "PUT", f"/api/v1/agents/{member_id}/runtime-metrics", json=body
            )
            return True
        except (CwsApiError, CwsAuthError) as exc:
            self._log.warn("runtime-metrics report failed (non-fatal):", exc)
            return False


class ChannelLivenessReporter:
    """Report this Hermes OpenMax channel's actual bridge health."""

    def __init__(
        self,
        http: CwsHttpClient,
        member_id_provider: Callable[[], str],
        health_provider: Callable[[], Optional[bool]],
        *,
        logger: Optional[Logger] = None,
    ):
        self._http = http
        self._member_id = member_id_provider
        self._health = health_provider
        self._log = logger or StdLogger("[cws-liveness]")

    async def report_once(self) -> bool:
        member_id = self._member_id()
        online = self._health()
        if not member_id or online is None:
            return False
        try:
            await self._http.request(
                "PUT",
                f"/api/v1/agents/{member_id}/channel-liveness",
                json={
                    "channels": [{"channel_type": "openmax", "online": bool(online)}]
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001 — liveness is best-effort
            self._log.warn("channel-liveness report failed (non-fatal):", exc)
            return False


class BillingGate:
    """GET /api/v1/billing/plan-state → usage_snapshot.enforcement_suspended.

    Cached (default 60 s). Fail-open: if the check itself errors we allow the
    message through — billing outages must not silence the agent.
    """

    def __init__(
        self,
        http: CwsHttpClient,
        *,
        cache_ttl_s: float = 60.0,
        notice_throttle_s: float = 3600.0,
        logger: Optional[Logger] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._http = http
        self._ttl = cache_ttl_s
        self._notice_throttle = notice_throttle_s
        self._log = logger or StdLogger("[cws-billing]")
        self._clock = clock
        self._cached: Optional[bool] = None
        self._cached_at = 0.0
        self._last_notice_at: dict[str, float] = {}

    async def is_suspended(self) -> bool:
        now = self._clock()
        if self._cached is not None and now - self._cached_at < self._ttl:
            return self._cached
        try:
            data = await self._http.get("/api/v1/billing/plan-state")
            snapshot = data.get("usage_snapshot") or {}
            self._cached = bool(snapshot.get("enforcement_suspended"))
        except (CwsApiError, CwsAuthError) as exc:
            self._log.warn("plan-state check failed, failing open:", exc)
            self._cached = False
        self._cached_at = now
        return self._cached

    def should_send_overdue_notice(self, conversation_id: str) -> bool:
        """At most one notice per conversation per throttle window."""
        now = self._clock()
        last = self._last_notice_at.get(conversation_id, 0.0)
        if now - last < self._notice_throttle:
            return False
        self._last_notice_at[conversation_id] = now
        return True


OVERDUE_NOTICE = (
    "⚠️ 本组织的 LLM 服务因账务原因已暂停,我暂时无法处理消息。"
    "请联系组织管理员前往账单页面处理。"
)

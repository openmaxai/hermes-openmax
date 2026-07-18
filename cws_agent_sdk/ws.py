"""cws-comm WebSocket client: one-shot ticket auth, reconnect with backoff,
close-code semantics, frame dispatch.

Contract highlights (cws-comm ws/):
- Connect: GET {ws_url}/ws?ticket=<one-shot 30s>&device_id=<id>
- Protocol-level ping/pong (the `websockets` library answers pings
  automatically); server closes 4001 after 2 missed pongs.
- Close codes: 4001 heartbeat, 4002 auth, 4003 session expired,
  4004 rate limited, 4005 org suspended, 4006 duplicate device.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from .codec import (
    CLOSE_AUTH_FAILURE,
    CLOSE_DUPLICATE_DEVICE,
    CLOSE_ORG_SUSPENDED,
    CLOSE_RATE_LIMITED,
    CLOSE_SESSION_EXPIRED,
    FRAME_PING,
    FRAME_PONG,
    Frame,
    decode_frame,
    encode_ping,
    encode_pong,
)
from .config import CwsConfig
from .errors import CwsWsFatal
from .providers import Logger, StdLogger

# Closes we never auto-recover from: somebody must fix account/deployment state.
_FATAL_CLOSES = {CLOSE_AUTH_FAILURE, CLOSE_ORG_SUSPENDED, CLOSE_DUPLICATE_DEVICE}
# Closes that invalidate the auth state before reconnecting.
_REAUTH_CLOSES = {CLOSE_SESSION_EXPIRED}


class CwsWsClient:
    def __init__(
        self,
        cfg: CwsConfig,
        *,
        ticket_provider: Callable[[], Awaitable[str]],
        on_frame: Callable[[Frame], Awaitable[None]],
        on_reconnected: Optional[Callable[[], Awaitable[None]]] = None,
        on_auth_reset: Optional[Callable[[], None]] = None,
        on_fatal: Optional[Callable[[int, str], None]] = None,
        logger: Optional[Logger] = None,
    ):
        self._cfg = cfg
        self._ticket_provider = ticket_provider
        self._on_frame = on_frame
        self._on_reconnected = on_reconnected
        self._on_auth_reset = on_auth_reset
        self._on_fatal = on_fatal
        self._log = logger or StdLogger("[cws-ws]")
        self._task: Optional[asyncio.Task] = None
        self._conn: Optional[websockets.WebSocketClientProtocol] = None
        self._stopping = False
        self._connected = asyncio.Event()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="cws-ws")

    async def stop(self) -> None:
        self._stopping = True
        if self._conn:
            try:
                await self._conn.close(code=1000)
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def is_open(self) -> bool:
        return self._connected.is_set()

    async def send_text(self, text: str) -> None:
        if not self._conn:
            raise ConnectionError("cws ws not connected")
        await self._conn.send(text)

    def _apply_close_semantics(self, close) -> None:
        """Apply zylos close policy, raising when reconnect must stop."""
        code = close.code or 0
        reason = close.reason or ""
        if code in _FATAL_CLOSES:
            if self._on_fatal:
                self._on_fatal(code, reason)
            raise CwsWsFatal(code, reason)
        if code in _REAUTH_CLOSES and self._on_auth_reset:
            self._on_auth_reset()

    async def _json_keepalive(self, conn) -> None:
        """Client-initiated app-level JSON ping (zylos ws.js parity)."""
        try:
            while True:
                await asyncio.sleep(self._cfg.ws_ping_interval_s)
                await conn.send(encode_ping())
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            return

    # -- run loop ----------------------------------------------------------

    async def _run(self) -> None:
        backoff = 1.0
        first = True
        while not self._stopping:
            try:
                ticket = await self._ticket_provider()
                base = self._cfg.ws_url.rstrip("/")
                if not base.endswith("/ws"):
                    base = (
                        f"{base}/ws"  # accept ws_url given with or without the /ws path
                    )
                url = f"{base}?ticket={ticket}&device_id={self._cfg.device_id}"
                async with websockets.connect(
                    url,
                    ping_interval=self._cfg.ws_ping_interval_s,
                    ping_timeout=self._cfg.ws_ping_interval_s,
                    max_size=64 * 1024,
                ) as conn:
                    self._conn = conn
                    self._connected.set()
                    backoff = 1.0
                    self._log.log("connected")
                    keepalive = asyncio.create_task(self._json_keepalive(conn))
                    if not first and self._on_reconnected:
                        await self._on_reconnected()
                    first = False
                    try:
                        async for raw in conn:
                            frame = decode_frame(raw)
                            if frame is None:
                                continue
                            if frame.type == FRAME_PING:
                                # zylos parity: answer app-level JSON pings —
                                # proxies may strip protocol ping/pong.
                                try:
                                    await conn.send(encode_pong())
                                except Exception:  # noqa: BLE001
                                    pass
                                continue
                            if frame.type == FRAME_PONG:
                                continue
                            try:
                                await self._on_frame(frame)
                            except Exception as exc:  # noqa: BLE001
                                # Frame handler errors must not kill the socket;
                                # undelivered messages are replayed via /sync.
                                self._log.warn("frame handler error:", exc)
                    finally:
                        keepalive.cancel()
            except ConnectionClosed as exc:
                code = exc.code or 0
                self._log.warn(f"closed code={code} reason={exc.reason!r}")
                self._apply_close_semantics(exc)
                if code == CLOSE_RATE_LIMITED:
                    backoff = max(backoff, 10.0)
            except CwsWsFatal:
                raise
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 — any transport error → backoff+retry
                self._log.warn("connect error:", exc)
            finally:
                self._conn = None
                self._connected.clear()
            if self._stopping:
                return
            sleep_s = backoff + random.uniform(0, backoff / 4)
            self._log.log(f"reconnecting in {sleep_s:.1f}s")
            await asyncio.sleep(sleep_s)
            backoff = min(backoff * 2, self._cfg.ws_reconnect_max_s)

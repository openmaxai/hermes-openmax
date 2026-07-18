"""Runtime-only parity regressions for websocket close handling."""

import pytest

from cws_agent_sdk.errors import CwsWsFatal
from cws_agent_sdk.ws import CwsWsClient


class _Close:
    def __init__(self, code: int, reason: str = "test"):
        self.code = code
        self.reason = reason


def _client(*, auth_resets=None, fatals=None):
    async def ticket_provider():
        return "ticket"

    async def on_frame(_frame):
        return None

    return CwsWsClient(
        cfg=object(),
        ticket_provider=ticket_provider,
        on_frame=on_frame,
        on_auth_reset=(lambda: auth_resets.append(True))
        if auth_resets is not None
        else None,
        on_fatal=(lambda code, reason: fatals.append((code, reason)))
        if fatals is not None
        else None,
    )


def test_close_4002_is_fatal_and_does_not_reset_auth():
    auth_resets = []
    fatals = []
    client = _client(auth_resets=auth_resets, fatals=fatals)

    with pytest.raises(CwsWsFatal) as exc_info:
        client._apply_close_semantics(_Close(4002, "invalid credentials"))

    assert exc_info.value.code == 4002
    assert auth_resets == []
    assert fatals == [(4002, "invalid credentials")]


def test_close_4003_resets_auth_and_remains_recoverable():
    auth_resets = []
    fatals = []
    client = _client(auth_resets=auth_resets, fatals=fatals)

    client._apply_close_semantics(_Close(4003, "session expired"))

    assert auth_resets == [True]
    assert fatals == []

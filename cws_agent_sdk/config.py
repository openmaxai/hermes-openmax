"""SDK configuration model."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CwsConfig:
    """Connection settings for one agent identity in one org.

    Values mirror the onboarding contract for external agents
    (bff_url / ws_url / api_key / org_id).
    """

    bff_url: str  # cws-core BFF, e.g. https://api.<env>.coco.xyz
    ws_url: str  # cws-comm WebSocket, e.g. wss://comm.<env>.coco.xyz
    api_key: str  # agent api key (cwsk_...)
    org_id: str = ""
    identity_id: str = ""
    member_id: str = ""
    device_id: str = "hermes-openmax"
    client_version: str = "0.1.0"
    # Networking knobs
    request_timeout_s: float = 30.0
    ws_ping_interval_s: float = 20.0  # client keepalive ping (feeds server watchdog)
    ws_reconnect_max_s: float = 30.0  # exponential backoff cap
    extra_headers: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "CwsConfig":
        e = env or os.environ
        return cls(
            bff_url=e.get("CWS_BFF_URL", "").rstrip("/"),
            ws_url=e.get("CWS_WS_URL", "").rstrip("/"),
            api_key=e.get("CWS_API_KEY", ""),
            org_id=e.get("CWS_ORG_ID", ""),
            identity_id=e.get("CWS_IDENTITY_ID", ""),
            member_id=e.get("CWS_MEMBER_ID", ""),
            device_id=e.get("CWS_DEVICE_ID", "hermes-openmax"),
        )

    def validate(self) -> list[str]:
        missing = []
        if not self.bff_url:
            missing.append("CWS_BFF_URL")
        if not self.ws_url:
            missing.append("CWS_WS_URL")
        if not self.api_key:
            missing.append("CWS_API_KEY")
        return missing

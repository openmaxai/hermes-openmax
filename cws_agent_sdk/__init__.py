"""cws-agent-sdk (Python) — CWS/OpenMax workspace protocol layer for agent runtimes.

Speaks only the CWS HTTP/WS contract (cws-core BFF + cws-comm WebSocket).
Host concerns (storage, logging) are injected via providers.
"""
from .access_policy import AccessDecision, AccessPolicyConfig, decide_inbound
from .bridge import CwsBridge
from .config import CwsConfig
from .errors import CwsApiError, CwsAuthError, CwsError, CwsWsFatal
from .providers import FileStorage, Logger, StdLogger, StorageProvider
from .reporters import BillingGate, MetricsReporter, OnlineReporter, RuntimeStateProvider
from .services import AsService, CommService, ConnService, CoreService, KbService, TmService
from .token import TokenManager, accept_invitation, register_agent
from .types import InboundMessage, SendReceipt

__all__ = [
    "CwsBridge",
    "CwsConfig",
    "CwsApiError",
    "CwsAuthError",
    "CwsError",
    "CwsWsFatal",
    "FileStorage",
    "Logger",
    "StdLogger",
    "StorageProvider",
    "TokenManager",
    "register_agent",
    "accept_invitation",
    "InboundMessage",
    "SendReceipt",
]

__version__ = "0.1.0"

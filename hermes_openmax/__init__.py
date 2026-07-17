"""hermes-openmax — CWS/OpenMax Workspace platform plugin for Hermes Agent."""
from __future__ import annotations

import os


def check_requirements() -> bool:
    try:
        import httpx  # noqa: F401
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config() -> list[str]:
    """Return a list of human-readable config problems (empty = ok)."""
    problems = []
    for var in ("CWS_BFF_URL", "CWS_WS_URL", "CWS_API_KEY"):
        if not os.getenv(var, "").strip():
            problems.append(f"{var} is not set")
    return problems


def is_connected() -> bool:
    from .adapter import CwsAdapter

    return CwsAdapter.last_instance_connected()


def _env_enablement():
    """Seed PlatformConfig.extra from env so env-only setups appear in status."""
    if not os.getenv("CWS_API_KEY", "").strip():
        return None
    extra = {
        "bff_url": os.getenv("CWS_BFF_URL", ""),
        "ws_url": os.getenv("CWS_WS_URL", ""),
        "org_id": os.getenv("CWS_ORG_ID", ""),
    }
    home = os.getenv("CWS_HOME_CHANNEL", "").strip()
    if home:
        return {"extra": extra, "home_channel": {"chat_id": home}}
    return {"extra": extra}


def register(ctx):
    """Hermes plugin entry point."""
    from .adapter import CwsAdapter

    ctx.register_platform(
        name="cws",
        label="OpenMax Workspace",
        adapter_factory=lambda cfg: CwsAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["CWS_BFF_URL", "CWS_WS_URL", "CWS_API_KEY"],
        install_hint="pip install hermes-openmax (deps: httpx, websockets)",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="CWS_HOME_CHANNEL",
        allowed_users_env="CWS_ALLOWED_USERS",
        allow_all_env="CWS_ALLOW_ALL_USERS",
        emoji="🏢",
        pii_safe=True,
        platform_hint=(
            "You are chatting inside an OpenMax workspace. Markdown is "
            "supported. Conversations may be DMs or group channels with "
            "humans and other agents; mention people with @name. Keep "
            "workspace etiquette: answer in the conversation's language."
        ),
    )

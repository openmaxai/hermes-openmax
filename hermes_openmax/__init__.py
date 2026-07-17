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


def _config_problems() -> list[str]:
    problems = []
    for var in ("CWS_BFF_URL", "CWS_WS_URL", "CWS_API_KEY"):
        if not os.getenv(var, "").strip():
            problems.append(f"{var} is not set")
    return problems


def validate_config(config=None) -> bool:
    """Registry contract: truthy = config valid (see platform_registry.py:304).

    Accepts an optional PlatformConfig positional (the registry passes one)."""
    problems = _config_problems()
    if problems:
        import logging

        logging.getLogger(__name__).warning("[cws] config problems: %s", problems)
    return not problems


def is_connected(config=None) -> bool:
    """Gateway enablement gate: 'has the user configured credentials?'.

    Called by the registry enable pass WITH a probe PlatformConfig argument.
    Must NOT mean 'is the adapter currently connected' — this runs BEFORE any
    adapter exists to decide whether to enable the platform (same semantics
    as the Discord/IRC plugins: env-var presence).
    """
    return not _config_problems()


def _env_enablement():
    """Seed PlatformConfig.extra from env so env-only setups appear in status."""
    if not os.getenv("CWS_API_KEY", "").strip():
        return None
    seed = {
        "bff_url": os.getenv("CWS_BFF_URL", ""),
        "ws_url": os.getenv("CWS_WS_URL", ""),
        "org_id": os.getenv("CWS_ORG_ID", ""),
    }
    home = os.getenv("CWS_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home}
    return seed


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id=None,
                           media_files=None, force_document=False) -> dict:
    """Out-of-process delivery for deliver=cws cron jobs (gateway not running).

    CWS needs no live socket to send — a short-lived REST client suffices."""
    from cws_agent_sdk import CwsConfig
    from cws_agent_sdk.http import CwsHttpClient
    from cws_agent_sdk.providers import FileStorage
    from cws_agent_sdk.services import CommService
    from cws_agent_sdk.token import TokenManager

    cfg = CwsConfig.from_env()
    tokens = TokenManager(cfg, storage=FileStorage("~/.hermes/platforms/cws"))
    http = CwsHttpClient(cfg, tokens)
    try:
        receipt = await CommService(http).send_message(chat_id, message)
        return {"success": True, "message_id": receipt.message_id}
    except Exception as exc:  # noqa: BLE001 — cron caller expects a dict, not a raise
        return {"success": False, "error": str(exc)}
    finally:
        await http.aclose()
        await tokens.aclose()


def register(ctx):
    """Hermes plugin entry point."""
    from pathlib import Path

    from .adapter import CwsAdapter
    from .tools import ALL_TOOLS

    skills_dir = Path(__file__).parent / "skills"
    _skills = [
        ("workspace", skills_dir / "workspace.md",
         "OpenMax workspace 工作纪律(Guided Autonomy):任务流/状态机/护栏/礼仪"),
        ("tm-ops", skills_dir / "ops" / "tm.md",
         "TM 操作手册:Project/Issue/Task/Attempt/Blueprint/评论/定时绑定"),
        ("kb-ops", skills_dir / "ops" / "kb.md",
         "KB 操作手册:页面/目录树/版本/回收站/搜索"),
        ("as-ops", skills_dir / "ops" / "as.md",
         "文件/附件操作手册:上传/下载/MEDIA 纪律"),
        ("comm-ops", skills_dir / "ops" / "comm.md",
         "会话操作手册:主动 DM/建群/历史"),
        ("core-ops", skills_dir / "ops" / "core.md",
         "目录/身份操作手册:成员/能力画像/改名"),
        ("conn-ops", skills_dir / "ops" / "conn.md",
         "第三方连接能力说明(暂无工具)"),
    ]
    for skill_name, skill_path, desc in _skills:
        if skill_path.exists():
            try:
                ctx.register_skill(name=skill_name, path=skill_path, description=desc)
            except Exception:  # noqa: BLE001 — skill registration is an enhancement
                pass

    for name, schema, handler, emoji in ALL_TOOLS:
        ctx.register_tool(
            name=name,
            toolset="workspace",
            schema=schema,
            handler=handler,
            check_fn=is_connected,
            emoji=emoji,
        )

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
        standalone_sender_fn=_standalone_send,
        allowed_users_env="CWS_ALLOWED_USERS",
        allow_all_env="CWS_ALLOW_ALL_USERS",
        emoji="🏢",
        pii_safe=True,
        max_message_length=3000,  # matches zylos splitMessage maxLen / FE expectations
        platform_hint=(
            "You are chatting inside an OpenMax workspace. Markdown is "
            "supported. Conversations may be DMs or group channels with "
            "humans and other agents; mention people with @name. Keep "
            "workspace etiquette: answer in the conversation's language. "
            "You can send media files natively. To send an image WITH a "
            "caption as ONE message, use markdown image syntax with a "
            "file URI: ![your caption here](file:///absolute/path.png) — the "
            "alt text becomes the image caption. Bare MEDIA:/absolute/path "
            "also works but sends the image without a caption. NEVER paste "
            "presigned storage URLs "
            "(storage.googleapis.com/...X-Amz-...) into chat: they are "
            "enormous, expire in minutes, and render as raw text. For files "
            "already in the workspace, reference them by artifact_id or a "
            "KB page link instead."
        ),
    )

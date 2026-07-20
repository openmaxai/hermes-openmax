"""Generate the copyable Buy Agent onboarding prompt for Hermes/OpenMax."""

from __future__ import annotations

import argparse
import textwrap


def build_prompt(
    *,
    bff_url: str = "",
    ws_url: str = "",
    org_id: str = "",
    invitation_id: str = "",
    invitation_token: str = "",
    organization_name: str = "",
    display_name: str = "",
    owner_name: str = "",
    owner_member_id: str = "",
    expires_at: str = "",
) -> str:
    """Render a self-contained onboarding prompt.

    Invitation tokens are intentionally accepted only as an in-memory rendering
    value. Callers must deliver the returned prompt directly to the target agent
    and must not persist it in the repository or logs.
    """
    values = {
        "CWS_BFF_URL": bff_url or "<CWS_BFF_URL>",
        "CWS_WS_URL": ws_url or "<CWS_WS_URL>",
        "ORG_ID": org_id or "<ORG_ID>",
        "INVITATION_ID": invitation_id or "<INVITATION_ID>",
        "INVITATION_TOKEN": invitation_token or "<INVITATION_TOKEN>",
        "ORGANIZATION_NAME": organization_name or "<ORGANIZATION_NAME>",
        "DISPLAY_NAME": display_name or "<DISPLAY_NAME>",
        "OWNER_NAME": owner_name or "<OWNER_NAME>",
        "OWNER_MEMBER_ID": owner_member_id or "<OWNER_MEMBER_ID>",
        "EXPIRES_AT": expires_at or "<EXPIRES_AT>",
    }
    prompt = textwrap.dedent(
        """
        # OpenMax Agent Onboarding for Hermes

        请实际完成 Hermes 的 OpenMax 插件安装、Agent 注册、邀请接受、Gateway 启动和连接验证。不要只解释步骤，也不要安装或配置 `zylos-openmax`。

        ## 安全与执行要求

        - 不得输出 API key、access token、refresh token、邀请 token、JWT、WebSocket ticket 或完整 `.env`。
        - 不得把任何密钥写入日志、代码仓库、测试文件或最终报告。
        - 所有配置只能写入 `hermes config env-path` 返回的当前 Hermes profile 环境文件。
        - 不得修改其他 Hermes profile。
        - 如果当前 profile 已有 `CWS_API_KEY`，先验证有效性；有效则复用，无效才重新注册。
        - OpenMax 使用 D8 response envelope。直接 HTTP API 的结果从 `data` 读取；Hermes OpenMax SDK 的 `register_agent()` 和 `accept_invitation()` 返回已经解包后的 D8 `data`。
        - 不得调用 `/channel-liveness` 验证 OpenMax 连接。它是完整 catalog-backed IM 渠道快照，不代表 CWS/OpenMax WebSocket 健康状态。
        - 不得仅凭 `hermes gateway status` 为 running 就报告连接成功。

        ## Onboarding Parameters

        | Setting | Value |
        |---|---|
        | `CWS_BFF_URL` | `__BFF__` |
        | `CWS_WS_URL` | `__WS__` |
        | `CWS_ORG_ID` | `__ORG__` |
        | `CWS_DEVICE_ID` | `hermes-openmax` |
        | Invitation ID | `__INV_ID__` |
        | Invitation Token | `__INV_TOKEN__` |
        | Expected Display Name | `__DISPLAY__` |
        | Expected Owner Member ID | `__OWNER_ID__` |
        | Expires | `__EXPIRES__` |

        `CWS_IDENTITY_ID`、`CWS_API_KEY` 和 `CWS_MEMBER_ID` 由 onboarding 流程生成或解析，不需要预先提供。

        ## Step 1: Install and Enable

        ```bash
        hermes plugins install openmaxai/hermes-openmax --enable
        ```

        如果仓库简写不可用：

        ```bash
        hermes plugins install https://github.com/openmaxai/hermes-openmax.git --enable
        ```

        确认安装结果：

        ```bash
        hermes plugins list --plain --no-bundled
        ```

        必须确认 `hermes-openmax` 存在且状态为 `enabled`。如果已安装，先运行 `hermes plugins update hermes-openmax`。不要安装 `zylos-openmax`。

        ## Step 2: Register and Configure

        ```bash
        ENV_PATH="$(hermes config env-path)"
        touch "$ENV_PATH"
        chmod 600 "$ENV_PATH"
        ```

        不得打印完整环境文件或任何敏感值。

        如果当前 profile 的 API key 通过 `POST __BFF__/auth/agent/token` 验证成功，记录 `reused-existing`，不要重复注册。仅在没有有效 API key 时调用：

        ```python
        from cws_agent_sdk import register_agent
        result = await register_agent("__BFF__")
        ```

        SDK 返回的 `result` 已经是解包后的 D8 `data`，直接读取 `identity_id` 和一次性 `api_key`，立即安全写入当前 profile `.env`，不得打印。

        合并写入：

        ```dotenv
        CWS_BFF_URL=__BFF__
        CWS_WS_URL=__WS__
        CWS_IDENTITY_ID=<REGISTERED_IDENTITY_ID>
        CWS_API_KEY=<REGISTERED_API_KEY>
        CWS_ORG_ID=__ORG__
        CWS_DEVICE_ID=hermes-openmax
        ```

        保留环境文件中的其他配置，并再次执行 `chmod 600 "$(hermes config env-path)"`。

        ## Step 3: Accept Invitation

        使用当前 API key 调用 `POST __BFF__/auth/agent/token`，请求体 `{}`。直接 HTTP 响应的 access token 位于 `data.access_token`，不得保存或打印。

        优先使用 SDK：

        ```python
        from cws_agent_sdk import accept_invitation
        result = await accept_invitation(
            "__BFF__",
            access_token,
            "__INV_ID__",
            "__INV_TOKEN__",
        )
        ```

        SDK 返回的 `result` 已经是解包后的 D8 `data`。读取 `member_id` 和 `org_id`，必须确认 `org_id == "__ORG__"`，然后将 `member_id` 写入当前 profile 的 `CWS_MEMBER_ID`。如果邀请已经接受，记录 `already-accepted`，不要重复接受，并通过 `/me` 或成员查询解析身份。

        Owner Member ID 由 OpenMax 邀请关系自动应用，不得手动绑定其他成员。

        ## Step 4: Start or Restart Gateway

        ```bash
        hermes gateway status
        ```

        Gateway 尚未安装时执行：

        ```bash
        hermes gateway install --start-now
        ```

        Gateway 已安装时执行：

        ```bash
        hermes gateway restart
        ```

        只重启一次，不创建重启循环、永久 launchd helper 或定时重启任务。

        ## Step 5: Verify Real Connection and Message Delivery

        ```bash
        hermes config check
        hermes plugins list --plain --no-bundled
        hermes gateway status
        ```

        检查当前 profile 的 Gateway 日志，确认 `cws connected`，并确认 `online-report ok` 或服务端成员查询的 `online_status=online`。同时确认没有 missing configuration、HTTP 401/403 或 WebSocket authentication error。

        证据边界必须保持清晰：`Gateway running` 仅表示进程运行；`cws connected` 表示 WebSocket 传输连接；`online-report` 表示 Agent 在线上报；`online_status=online` 表示服务端状态。CWS WebSocket 是传输连接，不是 IM channel，不得上报 `channel_type: openmax`，也不得调用 `/channel-liveness`。

        若做消息测试，必须按顺序确认：日志出现 `inbound message: platform=cws`、`response ready: platform=cws`、发送没有 `send failed`，并且 OpenMax 会话历史存在 Agent 回复。群组 smart mode 下，如果模型判断无需回复，必须只返回 `[SKIP]`，由桥接层保持静默。

        ## Step 6: Current Session Reload

        Gateway 重启和当前 Hermes session 重置不同：Gateway 重启重新加载后台插件和 `.env`；`/reset` 只重新加载当前交互 session 的插件工具和 prompt。连接不依赖 `/reset`。只有当前 session 没识别新插件时才执行 `/reset`，否则记录 `not-needed`。

        ## Invitation Details

        - Invitation ID: `__INV_ID__`
        - Organization ID: `__ORG__`
        - Organization: `__ORGANIZATION__`
        - Display Name: `__DISPLAY__`
        - Owner: `__OWNER__`
        - Owner Member ID: `__OWNER_ID__`
        - Expires: `__EXPIRES__`

        ## Final Report

        只报告以下格式，不得泄露密钥、token 或完整配置：

        ```text
        hermes-openmax installed: yes/no
        hermes-openmax enabled: yes/no
        Agent registration: passed/failed/reused-existing
        Invitation acceptance: passed/failed/already-accepted
        CWS connected: yes/no
        Online report: passed/failed/unknown
        OpenMax online status: online/offline/unknown
        Message E2E: passed/failed/not-tested
        identity_id: <ID or unavailable>
        member_id: <ID or unavailable>
        org_id: <ID or unavailable>
        Gateway: running/stopped/failed
        Current session reload: completed/not-needed/required/failed
        Errors: <redacted error or none>
        ```
        """
    ).strip()
    replacements = {
        "__BFF__": values["CWS_BFF_URL"],
        "__WS__": values["CWS_WS_URL"],
        "__ORG__": values["ORG_ID"],
        "__INV_ID__": values["INVITATION_ID"],
        "__INV_TOKEN__": values["INVITATION_TOKEN"],
        "__ORGANIZATION__": values["ORGANIZATION_NAME"],
        "__DISPLAY__": values["DISPLAY_NAME"],
        "__OWNER__": values["OWNER_NAME"],
        "__OWNER_ID__": values["OWNER_MEMBER_ID"],
        "__EXPIRES__": values["EXPIRES_AT"],
    }
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)
    return prompt + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Hermes/OpenMax onboarding prompt")
    for name in (
        "bff-url", "ws-url", "org-id", "invitation-id", "invitation-token",
        "organization-name", "display-name", "owner-name", "owner-member-id", "expires-at",
    ):
        parser.add_argument(f"--{name}", default="")
    args = parser.parse_args()
    kwargs = {key.replace("-", "_"): value for key, value in vars(args).items()}
    print(build_prompt(**kwargs), end="")


if __name__ == "__main__":
    main()

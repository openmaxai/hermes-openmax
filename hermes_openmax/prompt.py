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
    language: str = "en",
) -> str:
    """Render a self-contained onboarding prompt.

    Invitation tokens are accepted only as an in-memory rendering value. The
    caller must deliver the result directly to the target agent and must not
    persist it in the repository or logs.
    """
    if language not in {"en", "zh"}:
        raise ValueError("language must be 'en' or 'zh'")

    values = {
        "BFF": bff_url or "<CWS_BFF_URL>",
        "WS": ws_url or "<CWS_WS_URL>",
        "ORG": org_id or "<ORG_ID>",
        "INV_ID": invitation_id or "<INVITATION_ID>",
        "INV_TOKEN": invitation_token or "<INVITATION_TOKEN>",
        "ORGANIZATION": organization_name or "<ORGANIZATION_NAME>",
        "DISPLAY": display_name or "<DISPLAY_NAME>",
        "OWNER": owner_name or "<OWNER_NAME>",
        "OWNER_ID": owner_member_id or "<OWNER_MEMBER_ID>",
        "EXPIRES": expires_at or "<EXPIRES_AT>",
    }

    if language == "en":
        template = """
        # OpenMax Agent Onboarding for Hermes

        Actually complete the Hermes OpenMax plugin installation, Agent registration, invitation acceptance, Gateway startup, and connection verification. Do not merely explain the steps. Do not install or configure `zylos-openmax`.

        ## Security and Execution Requirements

        - Never output an API key, access token, refresh token, invitation token, JWT, WebSocket ticket, or complete `.env` file.
        - Never write secrets to logs, the repository, test files, or the final report.
        - Write configuration only to the environment file returned by `hermes config env-path` for the current Hermes profile.
        - Do not modify any other Hermes profile.
        - If the current profile already has a valid `CWS_API_KEY`, reuse it. Do not register a duplicate identity.
        - OpenMax uses a D8 response envelope. For direct HTTP calls, read results from `data`. The SDK functions `register_agent()` and `accept_invitation()` already return the unwrapped D8 `data` object.
        - Do not call `/channel-liveness` to verify OpenMax connectivity. It reports a complete catalog-backed IM-channel snapshot, not CWS/OpenMax WebSocket health.
        - Do not report success merely because `hermes gateway status` shows `running`.

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

        `CWS_IDENTITY_ID`, `CWS_API_KEY`, and `CWS_MEMBER_ID` are generated or resolved during onboarding; they do not need to be provided in advance.

        ## Step 1: Install and Enable

        ```bash
        hermes plugins install openmaxai/hermes-openmax --enable
        ```

        If the repository shorthand is unavailable:

        ```bash
        hermes plugins install https://github.com/openmaxai/hermes-openmax.git --enable
        ```

        Verify the installation:

        ```bash
        hermes plugins list --plain --no-bundled
        ```

        Confirm that `hermes-openmax` is present and enabled. If it is already installed, run `hermes plugins update hermes-openmax` first. Do not install `zylos-openmax`.

        ## Step 2: Register and Configure

        ```bash
        ENV_PATH="$(hermes config env-path)"
        touch "$ENV_PATH"
        chmod 600 "$ENV_PATH"
        ```

        Do not print the complete environment file or any sensitive value.

        First validate an existing API key by calling `POST __BFF__/auth/agent/token` with `{}`. If the request succeeds and `data.access_token` exists, record `reused-existing` and do not register again. Do not print the token.

        If there is no valid API key, use the SDK:

        ```python
        from cws_agent_sdk import register_agent
        result = await register_agent("__BFF__")
        ```

        The SDK return value `result` is already the unwrapped D8 `data` object. Read `identity_id` and the one-time `api_key`, then immediately write the key securely to the current profile `.env`. Never print it.

        Merge these values into the current profile environment file while preserving unrelated configuration:

        ```dotenv
        CWS_BFF_URL=__BFF__
        CWS_WS_URL=__WS__
        CWS_IDENTITY_ID=<REGISTERED_IDENTITY_ID>
        CWS_API_KEY=<REGISTERED_API_KEY>
        CWS_ORG_ID=__ORG__
        CWS_DEVICE_ID=hermes-openmax
        ```

        Then run `chmod 600 "$(hermes config env-path)"` again.

        ## Step 3: Accept Invitation

        Exchange the current API key at `POST __BFF__/auth/agent/token` with `{}`. For a direct HTTP response, the access token is at `data.access_token`. Do not save or print it.

        Prefer the SDK:

        ```python
        from cws_agent_sdk import accept_invitation
        result = await accept_invitation(
            "__BFF__",
            access_token,
            "__INV_ID__",
            "__INV_TOKEN__",
        )
        ```

        The SDK return value `result` is already the unwrapped D8 `data` object. Read `member_id` and `org_id`, and verify `org_id == "__ORG__"`. Then write `CWS_MEMBER_ID` to the current profile. If the invitation is already accepted, record `already-accepted`, do not accept it again, and resolve the identity through `/me` or a member lookup.

        The Owner Member ID is applied automatically by the OpenMax invitation relationship. Do not manually bind the Agent to another member.

        ## Step 4: Start or Restart Gateway

        ```bash
        hermes gateway status
        ```

        If the Gateway service is not installed:

        ```bash
        hermes gateway install --start-now
        ```

        If it is already installed:

        ```bash
        hermes gateway restart
        ```

        Restart only once. Do not create restart loops, permanent launchd helpers, or scheduled restart jobs.

        ## Step 5: Verify Real Connection and Message Delivery

        ```bash
        hermes config check
        hermes plugins list --plain --no-bundled
        hermes gateway status
        ```

        Inspect the current profile's Gateway log and confirm `cws connected`, plus `online-report ok` or a service-side member query showing `online_status=online`. Confirm there is no missing configuration, HTTP 401/403, or WebSocket authentication error.

        Keep the evidence boundaries explicit: `Gateway running` means only that the process is running; `cws connected` means the WebSocket transport is connected; `online-report` means the Agent online report was sent or accepted; `online_status=online` is the service-side status. CWS WebSocket is transport, not an IM channel. Do not report `channel_type: openmax` and do not call `/channel-liveness`.

        If message testing is possible, verify: `inbound message: platform=cws` → `response ready: platform=cws` → send succeeds without `send failed` → the OpenMax conversation history contains the Agent reply. In group smart mode, if no response is needed, return exactly `[SKIP]` and remain silent.

        ## Step 6: Current Session Reload

        Gateway restart and current Hermes session reload are separate. Gateway restart reloads the background plugin and `.env`; `/reset` reloads the current interactive session's plugins, tools, and prompt. The OpenMax connection does not depend on `/reset`. Use `/reset` only if the current session does not recognize the newly installed plugin; otherwise report `not-needed`.

        ## Invitation Details

        - Invitation ID: `__INV_ID__`
        - Organization ID: `__ORG__`
        - Organization: `__ORGANIZATION__`
        - Display Name: `__DISPLAY__`
        - Owner: `__OWNER__`
        - Owner Member ID: `__OWNER_ID__`
        - Expires: `__EXPIRES__`

        ## Final Report

        Report only the following format. Do not disclose any key, token, JWT, WebSocket ticket, or complete configuration:

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
    else:
        template = """
        # OpenMax Agent Onboarding for Hermes

        请实际完成 Hermes 的 OpenMax 插件安装、Agent 注册、邀请接受、Gateway 启动和连接验证。不要只解释步骤，也不要安装或配置 `zylos-openmax`。

        ## 安全与执行要求

        - 不得输出 API key、access token、refresh token、邀请 token、JWT、WebSocket ticket 或完整 `.env`。
        - 不得把任何密钥写入日志、代码仓库、测试文件或最终报告。
        - 所有配置只能写入 `hermes config env-path` 返回的当前 Hermes profile 环境文件。
        - 不得修改其他 Hermes profile。
        - 如果当前 profile 已有有效 `CWS_API_KEY`，复用它；不要重复注册。
        - OpenMax 使用 D8 response envelope。直接 HTTP API 的结果从 `data` 读取；SDK 的 `register_agent()` 和 `accept_invitation()` 已经返回解包后的 D8 `data`。
        - 不得调用 `/channel-liveness` 验证 OpenMax 连接。它报告完整 IM channel 清单，不代表 CWS/OpenMax WebSocket 健康状态。
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

        确认 `hermes-openmax` 存在且已启用。如果已安装，先运行 `hermes plugins update hermes-openmax`。不要安装 `zylos-openmax`。

        ## Step 2: Register and Configure

        ```bash
        ENV_PATH="$(hermes config env-path)"
        touch "$ENV_PATH"
        chmod 600 "$ENV_PATH"
        ```

        不得打印完整环境文件或任何敏感值。先验证已有 API key；验证成功则记录 `reused-existing`，不要重复注册。没有有效 key 时使用 `register_agent()`，读取已解包的 `identity_id` 和一次性 `api_key`，立即安全写入当前 profile `.env`，不得打印。

        ## Step 3: Accept Invitation

        使用当前 API key 获取 `data.access_token`，优先调用 `accept_invitation()` 接受 `__INV_ID__` / `__INV_TOKEN__`。SDK 返回已经解包的 `data`；读取 `member_id` 和 `org_id`，确认 `org_id == "__ORG__"`，再写入 `CWS_MEMBER_ID`。如果已经接受，记录 `already-accepted`，不要重复接受。

        ## Step 4: Start or Restart Gateway

        Gateway 未安装时执行 `hermes gateway install --start-now`；已安装时执行 `hermes gateway restart`。只重启一次，不创建重启循环或定时重启任务。

        ## Step 5: Verify

        运行 `hermes config check`、`hermes plugins list --plain --no-bundled` 和 `hermes gateway status`。确认日志有 `cws connected`，并确认 `online-report ok` 或服务端 `online_status=online`。不要调用 `/channel-liveness`，也不要把 CWS 报告为 `channel_type: openmax`。

        如进行消息测试，确认 `inbound message: platform=cws` → `response ready: platform=cws` → 发送成功 → 会话历史有 Agent 回复。群组 smart mode 无需回复时只返回 `[SKIP]`。`/reset` 只在当前 session 未识别新插件时使用。

        ## Final Report

        只报告安装、启用、注册/邀请、CWS connected、online-report、OpenMax online status、Message E2E、identity/member/org ID、Gateway 和脱敏错误，不得泄露任何 secret。
        """

    prompt = textwrap.dedent(template).strip()
    for placeholder, value in {
        "__BFF__": values["BFF"],
        "__WS__": values["WS"],
        "__ORG__": values["ORG"],
        "__INV_ID__": values["INV_ID"],
        "__INV_TOKEN__": values["INV_TOKEN"],
        "__ORGANIZATION__": values["ORGANIZATION"],
        "__DISPLAY__": values["DISPLAY"],
        "__OWNER__": values["OWNER"],
        "__OWNER_ID__": values["OWNER_ID"],
        "__EXPIRES__": values["EXPIRES"],
    }.items():
        prompt = prompt.replace(placeholder, value)
    return prompt + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Hermes/OpenMax onboarding prompt")
    for name in (
        "bff-url", "ws-url", "org-id", "invitation-id", "invitation-token",
        "organization-name", "display-name", "owner-name", "owner-member-id", "expires-at",
    ):
        parser.add_argument(f"--{name}", default="")
    parser.add_argument("--language", choices=("en", "zh"), default="en")
    args = parser.parse_args()
    kwargs = {key.replace("-", "_"): value for key, value in vars(args).items()}
    print(build_prompt(**kwargs), end="")


if __name__ == "__main__":
    main()

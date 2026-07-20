# hermes-openmax

CWS / OpenMax Workspace channel for [Hermes Agent] — a pure-Python integration
that makes a coco workspace org a first-class Hermes chat platform, alongside
Lark, Telegram, etc.

One repo, two layers (mirrors the org's SDK + thin-adapter pattern):

| Layer | Package | Depends on |
|---|---|---|
| CWS protocol SDK | `cws_agent_sdk/` | only the CWS HTTP/WS contract (httpx, websockets) |
| Hermes platform adapter | `hermes_openmax/` | Hermes gateway plugin surface + the SDK |

The SDK layer is designed to be extracted into a standalone `cws-agent-sdk-py`
package later; nothing in it imports Hermes.

## Architecture

```
CWS Server (cws-core BFF + cws-comm WS)
   │  REST (D8 envelope) + WebSocket (one-shot 30s ticket)
   ▼
cws_agent_sdk.CwsBridge          # token · thin-frame refetch · dedupe · /sync
   │                             # replay · read+ack watermarks
   ▼  on_message(InboundMessage) / send()
hermes_openmax.CwsAdapter        # BasePlatformAdapter: handle_message() / send()
   ▼
Hermes Gateway                   # sessions, queueing, interrupts, cron, auth
```

Delivery invariant: the SDK advances its read/sync watermarks **only after**
the adapter's `handle_message` returns without raising. A failed delivery is
replayed via `POST /api/v1/sync` on the next connect.

## Install

```bash
uv pip install 'git+https://github.com/openmaxai/hermes-openmax.git'
# Or from a checkout:
uv pip install -e /path/to/hermes-openmax
# The plugin is discovered through the Hermes entry point. For a directory
# checkout, this symlink is also supported:
ln -s /path/to/hermes-openmax/hermes_openmax ~/.hermes/plugins/hermes-openmax
# enable hermes-openmax in ~/.hermes/config.yaml, then restart:
hermes gateway restart
```

Put the secret in `~/.hermes/.env`:

```dotenv
CWS_API_KEY=<your OpenMax agent API key>
```

Configure these non-secret connection values in the environment used by the
Gateway (or your service manager):

```bash
export CWS_BFF_URL=https://<openmax-bff>
export CWS_WS_URL=wss://<openmax-comm>/ws
export CWS_ORG_ID=<org_id>
export CWS_MEMBER_ID=<agent_member_id> # optional; resolved from /me when absent
```

Enable the plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-openmax
```

Check the connection:

```bash
hermes gateway status
grep -i "cws connected" ~/.hermes/logs/gateway.log
```

### OpenMax Agent onboarding prompt template

The OpenMax **Buy Agent** flow can place a copyable prompt like the following
in the invitation. This is a documentation template: replace only the
placeholder values when the platform renders the invitation. It intentionally
contains no real organization, invitation, or credential data.

~~~markdown
# OpenMax Agent Onboarding for Hermes

请实际完成 Hermes 的 OpenMax 插件安装、Agent 注册、邀请接受、Gateway 启动和连接验证。不要只解释步骤，也不要安装或配置 `zylos-openmax`。

## 安全与执行要求

- 不得输出 API key、access token、refresh token、邀请 token、JWT、WebSocket ticket 或完整 `.env`。
- 不得把任何密钥写入日志、代码仓库、测试文件或最终报告。
- 所有配置只能写入 `hermes config env-path` 返回的当前 Hermes profile 环境文件。
- 如果当前 profile 已有有效 `CWS_API_KEY`，复用它；不要重复注册。
- 直接调用 OpenMax HTTP API 时从 D8 `data` 读取结果；Hermes OpenMax SDK 返回的 `result` 已经是解包后的 D8 `data`。
- 不得调用 `/channel-liveness` 验证 OpenMax 连接。CWS WebSocket 是传输连接，不是 IM channel。

## Install → Register → Accept → Start → Verify

```bash
hermes plugins install openmaxai/hermes-openmax --enable
# 仓库简写不可用时：
# hermes plugins install https://github.com/openmaxai/hermes-openmax.git --enable
hermes plugins list --plain --no-bundled
ENV_PATH="$(hermes config env-path)"
touch "$ENV_PATH"
chmod 600 "$ENV_PATH"
```

配置并注册 `<CWS_BFF_URL>`、`<CWS_WS_URL>`、`<ORG_ID>`。若没有有效 `CWS_API_KEY`，使用 `register_agent()` 注册并立即把一次性 API key 安全写入当前 profile；然后通过 `data.access_token` 交换短期 token，使用 `accept_invitation()` 接受 `<INVITATION_ID>` / `<INVITATION_TOKEN>`，确认返回的 `org_id`，并写入 `CWS_MEMBER_ID`。不要打印任何 secret。

Gateway 未安装时执行 `hermes gateway install --start-now`；已安装时执行 `hermes gateway restart`。确认日志中有 `cws connected` 和 `online-report ok`，并检查服务端 `online_status=online`。不要仅凭 Gateway running 判断连接成功，也不要调用 `/channel-liveness`。

消息验证必须确认 `inbound message: platform=cws` → `response ready: platform=cws` → 发送成功 → 会话历史存在 Agent 回复。群组 smart mode 无需回复时只返回 `[SKIP]`。`/reset` 只在当前 session 未识别新插件时使用，不是建立 Gateway 连接的必要步骤。

最终只报告安装、启用、注册/邀请、CWS connected、online-report、OpenMax online status、Message E2E、identity/member/org ID、Gateway 和脱敏错误；不得报告任何密钥或 token。
~~~


The Buy Agent/onboarding service must substitute the placeholders when
rendering the invitation. Invitation tokens are delivered only to the target
onboarding session. The generated API key belongs in Hermes' local `.env`,
never in this README.

## Development

```bash
uv sync --extra dev
uv run pytest -q
# Exclude live network tests (the default test run skips them):
uv run pytest -m 'not live' -q
```

Live group smoke tests are explicit opt-in and require a real OpenMax
environment plus a running Gateway. Set the documented live-test variables in
your local environment; never paste tokens into chat or commit them. See
`tests/smoke/test_live_group.py` for the exact variables and command.

## Current behavior

- Inbound media is hydrated into `InboundMessage.media`; local Markdown image
  delivery is restricted to trusted roots (`~/.hermes/media`, `~/.hermes/tmp`,
  plus `CWS_MEDIA_ROOTS`). Outbound local images support native upload.
- Workspace task/issue semantic state actions use `workspace_tasks`. When the
  tool call includes `source_conversation_id` (or `source_chat_id`), the
  completed action sends one best-effort progress notification back to that
  conversation. Notifications are marked with `metadata.progress_notification`
  and deduplicated by conversation, work item, action, and resulting status.
  Missing source context, read-only actions, and notification failures do not
  break the underlying task operation.
- `send(metadata=...)` passes causation/interaction metadata through unchanged;
  the server currently treats that metadata as opaque.
- The plugin surface is covered by unit tests; live deployment verification is
  environment-dependent and is not claimed here.

## zylos-openmax parity surfaces

Online reporting, access policy, DM allowlist management, hot updates, runtime
metrics, TM/KB/artifact/core/comm services, workspace_comm
attachment delivery, per-conversation progress notifications, and native
workspace tools are implemented and unit-tested. `workspace_kb` is available for
Knowledge Base CRUD, and `workspace_artifacts` handles uploads/downloads and
attachments. `workspace_members` provides directory, DM policy, and organization
management. Connection remains explicitly unsupported: hermes-openmax does not
register a Connection/`conn` tool and must not request credentials or simulate that surface.

OpenMax WebSocket connectivity is transport health, not an installed IM channel.
This adapter therefore does not call the channel-liveness snapshot endpoint. A
runtime that owns IM channel processes must report its complete catalog-backed
snapshot (for example, Feishu and Telegram) from their actual process health.

OpenMax group ingress is upstream-authorized by CWS. Group messages therefore
intentionally have no per-member Hermes `user_id`; this preserves one shared
session per group while OpenMax enforces group scope, group allowlist,
allow-from, mention, smart, and silent policy before delivery. DM sessions
remain user/conversation-scoped.

The bundled `hermes_openmax/skills/` docs preserve role boundaries,
Issue→Blueprint→Task lifecycle, assignment confirmation, dependency and
notification rules, human acceptance loop, System Member handling, media
safety, and `/workspace` frontend-link conventions.

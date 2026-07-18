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
/Users/you/.hermes/hermes-agent/venv/bin/pip install httpx websockets
ln -s /path/to/hermes-openmax/hermes_openmax ~/.hermes/plugins/hermes-openmax
# enable hermes-openmax in ~/.hermes/config.yaml, then restart:
hermes gateway restart
```

Configure `CWS_BFF_URL`, `CWS_WS_URL`, `CWS_API_KEY`, and optionally `CWS_ORG_ID`.

## Development

```bash
uv run pytest -q
```

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

Online reporting, channel-liveness reporting, access policy, DM allowlist management,
hot updates, runtime metrics, TM/KB/artifact/core/comm services, workspace_comm
attachment delivery, per-conversation progress notifications, and native
workspace tools are implemented and unit-tested. `workspace_kb` is available for
Knowledge Base CRUD, and `workspace_artifacts` handles uploads/downloads and
attachments. `workspace_members` provides directory, DM policy, and organization
management. Connection remains explicitly unsupported: hermes-openmax does not
register a Connection/`conn` tool and must not request credentials or simulate that surface.

The bundled `hermes_openmax/skills/` docs preserve role boundaries,
Issue→Blueprint→Task lifecycle, assignment confirmation, dependency and
notification rules, human acceptance loop, System Member handling, media
safety, and `/workspace` frontend-link conventions.

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

## Step 1: Install

```bash
uv pip install 'git+https://github.com/openmaxai/hermes-openmax.git'
```

Or install a local checkout:

```bash
uv pip install -e /path/to/hermes-openmax
```

## Step 2: Configure Hermes

Enable the plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-openmax
```

During onboarding, fill in the connection values as follows:

| Value | Hermes setting |
|---|---|
| cws-core REST URL (`bff_url`) | `<CWS_BFF_URL>` |
| cws-comm WebSocket URL (`ws_url`) | `<CWS_WS_URL>` |
| `identity_id` | _(leave blank — auto-register)_ |
| `api_key` | _(leave blank — auto-register; store the generated key in Hermes `.env`)_ |
| `member_id` | _(leave blank — resolve after registration)_ |
| `org_id` | `<ORG_ID>` |

The onboarding/installer flow is responsible for registering the external
agent identity and writing the generated API key to the local Hermes secret
store. The API key must never be printed, pasted into chat, or committed.

The resulting local environment is conceptually:

```dotenv
CWS_BFF_URL=<CWS_BFF_URL>
CWS_WS_URL=<CWS_WS_URL>
CWS_ORG_ID=<ORG_ID>
CWS_API_KEY=<GENERATED_API_KEY>
# Optional after registration:
# CWS_IDENTITY_ID=<IDENTITY_ID>
# CWS_MEMBER_ID=<MEMBER_ID>
```

## Step 3: Accept Invitation

Exchange the generated API key for a short-lived access token, then accept
the invitation. The onboarding runtime must substitute these placeholders;
do not expose them in the final report:

```bash
ACCESS_TOKEN=$(curl -fsS -X POST `<CWS_BFF_URL>/auth/agent/token` \
  -H "Authorization: Bearer <GENERATED_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -fsS -X POST \
  "<CWS_BFF_URL>/api/v1/invitations/<INVITATION_ID>/accept" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token":"<INVITATION_TOKEN>"}'

unset ACCESS_TOKEN
```

## Step 4: Start and verify

```bash
hermes gateway restart
hermes gateway status
grep -i "cws connected" ~/.hermes/logs/gateway.log
```

Expected behavior:

- Each OpenMax DM uses its own Hermes DM session.
- Each OpenMax group uses one shared conversation-scoped Hermes session.
- OpenMax Agent Policy controls DM/group admission, mention, smart, silent,
  group scope, group allowlist, and allow-from.
- In group smart mode, reply with exactly `[SKIP]` when no response is needed.
- Never report API keys, access tokens, invitation tokens, WS tickets, or
  signed artifact URLs.
~~~

`<CWS_BFF_URL>`, `<CWS_WS_URL>`, `<ORG_ID>`, `<INVITATION_ID>`, and
`<INVITATION_TOKEN>` are placeholders only. The Buy Agent/onboarding service
must substitute them when generating an invitation. `api_key` is intentionally
blank in the template because the onboarding flow is expected to auto-register
the external identity and provision the generated key; the generated secret
belongs in Hermes' local `.env`, never in this README.

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

Online reporting, channel-liveness reporting, access policy, DM allowlist management,
hot updates, runtime metrics, TM/KB/artifact/core/comm services, workspace_comm
attachment delivery, per-conversation progress notifications, and native
workspace tools are implemented and unit-tested. `workspace_kb` is available for
Knowledge Base CRUD, and `workspace_artifacts` handles uploads/downloads and
attachments. `workspace_members` provides directory, DM policy, and organization
management. Connection remains explicitly unsupported: hermes-openmax does not
register a Connection/`conn` tool and must not request credentials or simulate that surface.

OpenMax group ingress is upstream-authorized by CWS. Group messages therefore
intentionally have no per-member Hermes `user_id`; this preserves one shared
session per group while OpenMax enforces group scope, group allowlist,
allow-from, mention, smart, and silent policy before delivery. DM sessions
remain user/conversation-scoped.

The bundled `hermes_openmax/skills/` docs preserve role boundaries,
Issue→Blueprint→Task lifecycle, assignment confirmation, dependency and
notification rules, human acceptance loop, System Member handling, media
safety, and `/workspace` frontend-link conventions.

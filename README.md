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
cws_agent_sdk.CwsBridge          # token 15min/refresh 7d · thin-frame refetch ·
   │                             # dedupe · /sync replay · read+ack watermarks
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
# 1. Install into the Hermes runtime env (or pip install hermes-openmax once published)
/Users/you/.hermes/hermes-agent/venv/bin/pip install httpx websockets
ln -s /path/to/hermes-openmax/hermes_openmax ~/.hermes/plugins/hermes-openmax

# 2. Enable the plugin
#    ~/.hermes/config.yaml:
#      plugins:
#        enabled:
#          - hermes-openmax

# 3. Configure (env or `hermes config`)
export CWS_BFF_URL=https://api.<env>.coco.xyz
export CWS_WS_URL=wss://comm.<env>.coco.xyz
export CWS_API_KEY=cwsk_...        # from POST /auth/register/agent
export CWS_ORG_ID=<org uuid>

# 4. Restart the gateway
hermes gateway restart
```

First-time onboarding (no api key yet): use the SDK helpers —
`cws_agent_sdk.register_agent(bff_url)` → save the one-shot `cwsk_` key →
`accept_invitation(bff_url, jwt, invitation_id, invite_token)`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install pytest pytest-asyncio httpx websockets
PYTHONPATH=. .venv/bin/python -m pytest -q
```

## Live-env verification (2026-07-17, openmax.com)

Verified end-to-end against a real workspace org with a freshly registered
agent (`hermes_1`): register → token exchange → invitation accept → ws-ticket
→ WS connect → DM create → outbound send → REST readback → inbound delivery
(both /sync replay and WS thin-frame paths, including a real human reply).

Confirmed contract details:
- Outbound content shape `{"content_type": "text", "body": {"text": ...}}`
  is what the server stores and returns. ✅
- DM create (BFF) takes `peer_member_id` (NOT `peer_user_id` — that's the
  cws-comm direct surface) and returns `{conversation, created}`. ✅
- `ws_url` may be given with or without the `/ws` path; the SDK normalizes.

## zylos-openmax parity surfaces (aligned 2026-07-17, live-verified)

| Surface | Implementation | Live check |
|---|---|---|
| online-report | `OnlineReporter` — POST `/agents/{member}/online-report` at bridge start | ✅ `triggered=True` |
| access policy | `access_policy.decide_inbound` — group @mention gate, agent-loop guards, DM allowlist; per-conv `group_mode` overrides | ✅ live `policy skip [system_sender]` |
| billing gate | `BillingGate` — `/billing/plan-state` → `usage_snapshot.enforcement_suspended`, 60s cache, fail-open, throttled overdue notice | ✅ suspended=False |
| owner sync | `/me` auto-resolves member_id; `/members/{id}` → `owner_member_id`; `agent.config.owner_changed` hot-update | ✅ owner resolved |
| config hot-update | WS system frames `agent.config.*` (allowlist/group-mode/owner interpreted; rest → adapter callback) | unit-tested |
| runtime metrics | `MetricsReporter` — PUT `/agents/{member}/runtime-metrics` on interval; degrades to version-only without a RuntimeStateProvider | ✅ PUT ok |
| services | `TmService` / `KbService` / `AsService` (presigned two-phase upload) / `CoreService` / `CommService` | ✅ tm/kb/core smoke |
| native tools | `workspace_tasks` / `workspace_kb` / `workspace_artifacts` / `workspace_comm` / `workspace_members` | unit-tested; tool docs mirror zylos non-Connection operations |

The bundled `hermes_openmax/skills/` docs preserve zylos-openmax's role boundaries,
Issue→Blueprint→Task lifecycle, project/KB and assignee confirmation, dependency and
notification rules, human acceptance loop, System Member handling, media safety,
and `/workspace` frontend-link conventions. All workspace operations must use the
native tools above rather than hand-built BFF REST calls.

**Connection is explicitly unsupported:** hermes-openmax does not register a
Connection/`conn` tool. Do not request credentials, ask users to paste tokens, or
simulate Connection through another tool; report that boundary to the owner.

Local Markdown image delivery is restricted to trusted directories. Configure
additional absolute roots with the platform path separator in `CWS_MEDIA_ROOTS`;
the defaults are `~/.hermes/media` and `~/.hermes/tmp`. A `file://` image outside
those roots remains plain text and is never read or uploaded.

Explicitly NOT ported (owner decision 2026-07-17 — zylos-adapter-only concerns):
channel-liveness reporter, channel-connector (IM install), auto-upgrade.

## Known open items

1. **Media** — inbound attachments are surfaced in `InboundMessage.media` but
   download/upload helpers (`AsService`) are not yet wired into the adapter's
   MessageEvent media path; outbound is text-only.
2. **Ops alignment** — reply causation metadata (`interaction_id`,
   `causation_message_id`) can be passed via `send(metadata=...)`; the server
   treats metadata as opaque today.
3. **Gateway smoke** — the plugin surface is validated against Hermes v0.18.2
   (registry/adapter/MessageEvent), but a full `hermes gateway run` session
   with the plugin enabled hasn't been exercised yet.

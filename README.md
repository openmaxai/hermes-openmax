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

## Known open items (verify against a live env before GA)

1. **Outbound content shape** — `CommService.send_message` sends
   `{"content_type": "text", "body": {"text": ...}}`; the BFF schema only
   constrains `{content_type, body(map)}`, so confirm the inner key the
   workspace FE renders.
2. **Media** — inbound attachments are surfaced in `InboundMessage.media` but
   download (cws-as) is not wired yet; outbound is text-only.
3. **Wire-contract parity** — endpoints/frames were derived from cws-core /
   cws-comm source (see `docs/` in those repos); record real frame fixtures in
   int and add them to the test suite.
4. **Ops alignment** — reply causation metadata (`interaction_id`,
   `causation_message_id`) can be passed via `send(metadata=...)`; the server
   treats metadata as opaque today.

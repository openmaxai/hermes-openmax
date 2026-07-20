"""Generate a copyable onboarding prompt for a new Hermes/OpenMax agent."""

from __future__ import annotations

import argparse
import textwrap


def build_prompt(*, bff_url: str = "", ws_url: str = "", org_id: str = "", member_id: str = "") -> str:
    values = {
        "CWS_BFF_URL": bff_url or "<CWS_BFF_URL>",
        "CWS_WS_URL": ws_url or "<CWS_WS_URL>",
        "CWS_ORG_ID": org_id or "<CWS_ORG_ID>",
        "CWS_MEMBER_ID": member_id or "<CWS_MEMBER_ID>",
    }
    env = "\n".join(f"export {key}={value!r}" for key, value in values.items())
    prompt = textwrap.dedent(
        """
        You are a Hermes Agent being connected to OpenMax Workspace (CWS).

        Install and enable the hermes-openmax plugin, then configure the CWS
        connection. Never print or commit the API key.

        Repository: https://github.com/openmaxai/hermes-openmax
        Install from the repository:
          uv pip install 'git+https://github.com/openmaxai/hermes-openmax.git'
        Or install the local checkout:
          uv pip install -e /path/to/hermes-openmax

        Put the secret in Hermes' .env (not config.yaml):
          CWS_API_KEY=<your OpenMax agent API key>
        Non-secret connection values:
        __CWS_ENV__

        Enable the plugin in ~/.hermes/config.yaml:
          plugins:
            enabled:
              - hermes-openmax

        Restart the Gateway:
          hermes gateway restart

        Verify the connection:
          hermes gateway status
          grep -i "cws connected" ~/.hermes/logs/gateway.log

        Behavior expectations:
        - OpenMax DM and group conversations use separate Hermes sessions.
        - Each group is one shared conversation-scoped session; members are not
          split into separate sessions.
        - OpenMax Agent Policy controls DM/group admission, mention, smart,
          silent, group scope, group allowlist, and allow-from.
        - Do not bypass OpenMax policy with a second local allowlist.
        - In group smart mode, reply with exactly [SKIP] when no response is
          needed.
        - Keep API keys, JWTs, WS tickets, and signed artifact URLs private.

        After connecting, report:
        1. Whether CWS is connected;
        2. The configured org_id and member_id (never the API key);
        3. Whether a DM and a group message each create the expected session;
        4. Any error from ~/.hermes/logs/gateway.log.
        """
    ).strip()
    return prompt.replace("__CWS_ENV__", env) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Hermes/OpenMax onboarding prompt")
    parser.add_argument("--bff-url", default="")
    parser.add_argument("--ws-url", default="")
    parser.add_argument("--org-id", default="")
    parser.add_argument("--member-id", default="")
    args = parser.parse_args()
    print(build_prompt(bff_url=args.bff_url, ws_url=args.ws_url, org_id=args.org_id, member_id=args.member_id), end="")


if __name__ == "__main__":
    main()

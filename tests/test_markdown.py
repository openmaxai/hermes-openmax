"""Outbound OpenMax Markdown message regressions."""

import pytest

from cws_agent_sdk.codec import looks_like_markdown
from cws_agent_sdk.services import CommService


class RecordingHttp:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, *, json):
        self.calls.append((method, path, json))
        return {}


def test_markdown_detection_matches_openmax_table_rows():
    assert looks_like_markdown("| Name | Status |\n| --- | --- |\n| A | Done |")


@pytest.mark.asyncio
async def test_edit_message_preserves_markdown_content_type():
    http = RecordingHttp()
    service = CommService(http)

    await service.edit_message("msg-1", "## 标题\n\n```python\nprint('ok')\n```")

    assert http.calls == [
        (
            "PUT",
            "/api/v1/messages/msg-1",
            {
                "content": {
                    "content_type": "markdown",
                    "body": {"text": "## 标题\n\n```python\nprint('ok')\n```"},
                }
            },
        )
    ]

"""Prompt and media behavior shared by the OpenMax Hermes adapter."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_LOCAL_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\((file://[^\s\)]+)\)")


def _trusted_media_roots() -> tuple[Path, ...]:
    configured = os.getenv("CWS_MEDIA_ROOTS", "")
    roots = [Path.home() / ".hermes" / "media", Path.home() / ".hermes" / "tmp"]
    roots.extend(
        Path(value).expanduser()
        for value in configured.split(os.pathsep)
        if value and Path(value).expanduser().is_absolute()
    )
    return tuple(root.resolve() for root in roots if root.is_dir())


def _is_under(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _fenced_code_spans(content: str) -> list[tuple[int, int]]:
    return [match.span() for match in re.finditer(r"```[\s\S]*?```", content)]


def extract_local_markdown_images(content: str) -> tuple[list[tuple[str, str]], str]:
    """Extract safe, existing local image file URIs and preserve unmatched text."""
    images: list[tuple[str, str]] = []
    matched_spans: list[tuple[int, int]] = []
    fenced_spans = _fenced_code_spans(content)
    trusted_roots = _trusted_media_roots()
    for match in _LOCAL_MARKDOWN_IMAGE.finditer(content):
        if any(start <= match.start() < end for start, end in fenced_spans):
            continue
        uri = match.group(2)
        parsed = urlparse(uri)
        try:
            path = Path(unquote(parsed.path)).resolve()
        except (ValueError, OSError):
            continue
        if (
            parsed.scheme == "file"
            and not parsed.netloc
            and path.is_absolute()
            and path.is_file()
            and path.suffix.lower() in _IMAGE_EXTENSIONS
            and _is_under(path, trusted_roots)
        ):
            images.append((path.as_uri(), match.group(1)))
            matched_spans.append(match.span())

    if not matched_spans:
        return [], content

    pieces: list[str] = []
    cursor = 0
    for start, end in matched_spans:
        pieces.append(content[cursor:start])
        cursor = end
    pieces.append(content[cursor:])
    cleaned = "".join(pieces)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return images, cleaned


def build_workspace_orientation(
    me: dict[str, Any], *, owner_name: str = "", owner_id: str = "", persona: str = ""
) -> str:
    """Build per-turn OpenMax instructions, including mandatory skill-first handling."""
    orientation = (
        "# OpenMax Workspace context\n"
        f"You are workspace member '{me.get('display_name')}' (agent, member_id "
        f"{me.get('member_id')}) in org '{me.get('org_name')}' ({me.get('org_slug')}). "
        f"Your responsible human owner is '{owner_name or 'unknown'}'"
        f"{f' (member_id {owner_id})' if owner_id else ''}.\n"
        "You have native workspace tools: workspace_tasks (projects/issues/tasks/"
        "comments/blueprints/attempts), workspace_kb, workspace_comm (proactive "
        "messaging), workspace_artifacts, workspace_members. Use them for any "
        "workspace request instead of guessing; never use built-in todo tools for "
        "workspace tasks.\n"
        "IMPORTANT: For EVERY user message received through OpenMax, FIRST load "
        "skill_view('hermes-openmax:workspace') and follow it; then classify it as "
        "task versus Q&A/chat. Task-shaped messages must follow the complete "
        "Issue→Blueprint→Task discipline, project+KB confirmation, and owner "
        "acceptance loop. Q&A/chat should be answered directly as the skill specifies.\n"
        "System Member DMs (scheduler) drive the task flow: act on them in the "
        "referenced Issue/Task context, never reply to them. In group smart-mode, "
        "reply exactly [SKIP] to stay silent. Reply in the conversation's language."
    )
    if persona.strip():
        orientation += f"\n# Workspace persona\n{persona.strip()}"
    return orientation

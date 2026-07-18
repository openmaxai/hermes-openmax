"""Hermes OpenMax prompt and media behavior regressions."""

from hermes_openmax.behavior import (
    build_workspace_orientation,
    extract_local_markdown_images,
)


def test_extract_local_markdown_images_accepts_existing_file_uri_with_caption(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CWS_MEDIA_ROOTS", str(tmp_path))
    image = tmp_path / "hello world.png"
    image.write_bytes(b"png")
    content = f"![图文说明]({image.as_uri()})"

    images, cleaned = extract_local_markdown_images(content)

    assert images == [(image.as_uri(), "图文说明")]
    assert cleaned == ""


def test_extract_local_markdown_images_rejects_missing_local_file(tmp_path):
    missing = (tmp_path / "missing.png").as_uri()
    content = f"![不存在]({missing})"

    images, cleaned = extract_local_markdown_images(content)

    assert images == []
    assert cleaned == content


def test_extract_local_markdown_images_rejects_file_outside_trusted_roots(
    tmp_path, monkeypatch
):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    private = tmp_path / "private.png"
    private.write_bytes(b"png")
    monkeypatch.setenv("CWS_MEDIA_ROOTS", str(trusted))
    content = f"![private]({private.as_uri()})"

    images, cleaned = extract_local_markdown_images(content)

    assert images == []
    assert cleaned == content


def test_extract_local_markdown_images_rejects_symlink_escape(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    private = tmp_path / "private.png"
    private.write_bytes(b"png")
    link = trusted / "linked.png"
    link.symlink_to(private)
    monkeypatch.setenv("CWS_MEDIA_ROOTS", str(trusted))
    content = f"![linked]({link.as_uri()})"

    images, cleaned = extract_local_markdown_images(content)

    assert images == []
    assert cleaned == content


def test_extract_local_markdown_images_ignores_examples_inside_code_fences(tmp_path):
    image = tmp_path / "example.png"
    image.write_bytes(b"png")
    example = f"正确示例：\n\n```markdown\n![说明]({image.as_uri()})\n```"

    images, cleaned = extract_local_markdown_images(example)

    assert images == []
    assert cleaned == example


def test_workspace_orientation_requires_skill_before_classification():
    orientation = build_workspace_orientation(
        {
            "display_name": "agent",
            "member_id": "agent-1",
            "org_name": "Acme",
            "org_slug": "acme",
        },
        owner_name="",
        owner_id="",
    )

    assert "For EVERY user message received through OpenMax" in orientation
    assert "FIRST load skill_view('hermes-openmax:workspace')" in orientation
    assert "then classify it as task versus Q&A/chat" in orientation

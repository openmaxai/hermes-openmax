"""OpenMax skill/tool surface must stay aligned with zylos-openmax semantics."""

from pathlib import Path

import pytest

from cws_agent_sdk.services import CoreService, TmService
from hermes_openmax.tools import KB_SCHEMA, MEMBERS_SCHEMA, TASKS_SCHEMA


ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "hermes_openmax" / "skills"


def _text(relative: str) -> str:
    return (SKILLS / relative).read_text()


def test_tm_skill_documents_exposed_project_and_blueprint_actions():
    text = _text("ops/tm.md")
    for action in (
        "project_create",
        "project_get",
        "project_update",
        "project_archive",
        "project_members",
        "project_member_add",
        "project_member_remove",
        "task_get",
        "blueprint_set_steps",
        "binding_get",
    ):
        assert action in text
    assert "blueprint_submit" not in TASKS_SCHEMA["description"]
    assert "`blueprint_submit` 动作" in text
    assert "计划确认走 `issue_action(submit-plan)`" in text


def test_kb_skill_documents_every_exposed_action_and_current_frontend_path():
    text = _text("ops/kb.md")
    for action in (
        "kb_create",
        "page_update",
        "page_delete",
        "freeze",
        "references",
        "create_file",
        "batch_download",
    ):
        assert action in text
    assert "/workspace/knowledge" in text
    assert "/cws/knowledge" not in text


def test_core_skill_documents_exposed_directory_actions():
    text = _text("ops/core.md")
    for action in ("agent_profiles", "rename", "orgs", "roles", "frontend_url"):
        assert action in text
    assert "agent_profiles" in MEMBERS_SCHEMA["description"]


def test_artifact_skill_distinguishes_captioned_image_from_bare_media():
    text = _text("ops/as.md")
    assert "![caption](file:///absolute/path.png)" in text
    assert "MEDIA:/absolute/path" in text
    assert "预签名 URL" in text


def test_workspace_skill_keeps_zylos_progress_notification_discipline():
    text = _text("workspace.md")
    assert "状态流转即通知" in text
    assert "完成即通知" in text
    assert "主动请 Issue owner" in text


def test_workspace_skill_documents_zylos_runtime_guardrails():
    text = _text("workspace.md")
    for rule in (
        "引用只建立本轮语境",
        "不启动工作、不授予权限",
        "强制确认 Project + KB",
        "不得单方面拍板",
        "一次性把所有 Step 实例化成 Task",
        "必须用上游 Task 的真实 task.id",
        "System Member 是**只写身份**",
        "不要回复这条系统 DM",
        "永不隐式创建 Project",
        "双向 DM 权限确认",
    ):
        assert rule in text


def test_tm_skill_documents_governance_actions_and_no_stale_surface_claims():
    text = _text("ops/tm.md")
    assert 'name="reassign-owner"' in text
    assert 'name="move"' in text
    for stale in (
        "Project 操作**当前工具未暴露",
        "task.get(单任务详情;可用",
        "blueprint.set_steps(整批全量替换步骤",
        "event-binding.get(单个定时任务详情",
    ):
        assert stale not in text


def test_kb_skill_documents_permanent_delete_guardrail_without_stale_claim():
    text = _text("ops/kb.md")
    assert "永久删除必须先 `trash`" in text
    assert "确认后用 `page_delete`" in text
    assert "当前工具未暴露永久删除" not in text


def test_connection_remains_explicitly_unsupported():
    text = _text("ops/conn.md")
    assert "当前 hermes-openmax 尚未提供 conn 工具" in text
    assert "不要尝试用其他工具伪造或绕行" in text


def test_readme_describes_native_tools_and_connection_boundary():
    text = (ROOT / "README.md").read_text()
    assert "workspace_tasks" in text
    assert "workspace_kb" in text
    assert "workspace_artifacts" in text
    assert "workspace_comm" in text
    assert "workspace_members" in text
    assert "Connection" in text and "unsupported" in text
    assert "could be exposed as native" not in text
    assert "`ConnService`" not in text


def test_schema_descriptions_match_documented_surfaces():
    assert "blueprint_set_steps" in TASKS_SCHEMA["description"]
    assert "kb_create" in KB_SCHEMA["description"]
    assert "agent_profiles" in MEMBERS_SCHEMA["description"]


class RecordingHttp:
    def __init__(self):
        self.calls = []

    async def post(self, path, *, json=None):
        self.calls.append(("POST", path, json))
        return {}

    async def get_page(self, path, *, params=None):
        self.calls.append(("GET", path, params))
        return [], None


@pytest.mark.asyncio
async def test_project_create_preserves_zylos_project_fields():
    http = RecordingHttp()
    service = TmService(http)
    body = {
        "name": "Growth",
        "slug": "growth",
        "lead_member_id": "lead-1",
        "knowledge_base_id": "kb-1",
        "member_ids": ["m-1"],
    }

    await service.create_project(body)

    assert http.calls == [("POST", "/api/v1/projects", body)]


@pytest.mark.asyncio
async def test_agent_profiles_requests_capabilities_like_zylos():
    http = RecordingHttp()
    service = CoreService(http)

    await service.list_agent_profiles(project_id="p-1", capabilities=True)

    assert http.calls == [
        (
            "GET",
            "/api/v1/agent-profiles",
            {"project_id": "p-1", "include": ["capabilities"]},
        )
    ]

"""团队分工与档案的关键路径（plan-20261123-agent-team）。"""
import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.core.config import settings
from app.gateway.routers.subagents import (
    SubagentProfileIn, SubagentProfilePatch, create_profile, delete_profile, get_profile, update_profile,
)
from app.models.providers.ta3_tool_aliases import disguise_args, restore_args
from app.models.providers.ta3_tool_schemas import disguise_tools
from app.orchestration.agent_team import load_team
from app.orchestration.approval_policy import VERDICT_DENY, decide
from app.orchestration.subagent import SubagentHandle, SubagentManager
from app.orchestration.subagent_tools import apply_team_tools
from app.orchestration.tools.base import ToolContext
from app.persistence.database import async_session_factory, engine
from app.persistence.seed import _seed_subagent_profiles


@pytest.mark.parametrize("explore,execute", [(True, False), (False, True), (True, True), (False, False)])
async def test_dispatch_settings_share_tool_and_runtime_policy(monkeypatch, explore, execute):
    monkeypatch.setattr(settings, "subagent_enabled", True)
    monkeypatch.setattr(settings, "subagent_explore_enabled", explore)
    monkeypatch.setattr(settings, "subagent_execute_enabled", execute)
    custom = await create_profile(SubagentProfileIn(name="架构审阅", execution_mode="readonly"))
    async with async_session_factory() as db:
        await _seed_subagent_profiles(db)
        team = await load_team(db, "agent")
        plan = await load_team(db, "plan")
        workflow = await load_team(db, "workflow")
    assert bool(team.select(custom["id"], "readonly")) is explore
    if explore:
        assert "浏览器、桌面操控或拓展工具（含 MCP）" in team.notice()
    assert bool(team.select(None, "agent")) is execute
    assert team.select(custom["id"], "agent") is None
    assert plan.select(None, "agent") is None
    assert workflow.workflow_member(None, "agent")[0].key == "general"
    assert workflow.workflow_member(custom["name"], "readonly")[0].id == custom["id"]
    assert workflow.workflow_member(custom["name"], "agent")[1]
    schemas = apply_team_tools([], team, manage_existing=True)
    names = {schema["function"]["name"] for schema in schemas}
    assert ("spawn_subagent" in names) is (explore or execute)
    assert {"collect_results", "cancel_subagent"} <= names
    if explore or execute:
        native = next(schema["function"] for schema in disguise_tools(schemas)
                      if schema["function"]["name"] == "SubAgent")
        inputs = native["parameters"]["properties"]
        assert (custom["id"] in inputs["profile_id"]["enum"]) is explore
        assert ("agent" in inputs["mode"]["enum"]) is execute
        assert "purpose" not in inputs
    args = {"task_title": "审阅", "profile_id": custom["id"], "mode": "readonly"}
    assert restore_args("SubAgent", disguise_args("spawn_subagent", args))["profile_id"] == custom["id"]


async def test_team_profile_revision_restores_default_and_preserves_custom_prompt():
    async with async_session_factory() as db:
        await _seed_subagent_profiles(db)
        await db.commit()
        team = await load_team(db)
    member = team.builtin("readonly")
    first = await update_profile(member.id, SubagentProfilePatch(
        revision=0, name="仓库探索", system_prompt="自定义提示词" * 1500,
    ))
    assert first["display_name"] == "仓库探索"
    assert first["revision"] == 1
    with pytest.raises(HTTPException, match="409"):
        await update_profile(member.id, SubagentProfilePatch(revision=0, name="旧窗口"))
    with pytest.raises(HTTPException, match="422"):
        await update_profile(member.id, SubagentProfilePatch(revision=1, execution_mode="agent"))
    with pytest.raises(HTTPException, match="422"):
        await delete_profile(member.id)
    async with async_session_factory() as db:
        detail = await get_profile(member.id, db)
    for preview in detail["preview"].values():
        assert "自定义提示词" in preview
        assert "审批由主会话控制" in preview
    restored = await update_profile(member.id, SubagentProfilePatch(revision=1, reset="all"))
    assert restored["display_name"] == "探索智能体"
    assert restored["execution_mode"] == "readonly"


async def test_team_snapshot_does_not_query_for_each_dispatch():
    async with async_session_factory() as db:
        await _seed_subagent_profiles(db)
        await db.commit()

        statements = []
        def count_config_reads(conn, cursor, statement, parameters, context, executemany):
            if "subagent_profiles" in statement and statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", count_config_reads)
        try:
            team = await load_team(db)
            for count in (1, 4):
                for _ in range(count):
                    member = team.select(None, "agent")
                    assert member
                    team.model_for(member, None, None)
                assert len(statements) == 1
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_config_reads)


def test_readonly_team_cannot_read_outside_even_with_full_approval(workspace):
    ctx = ToolContext(str(workspace), 1, 1, 1, "探索智能体", permission_mode="readonly",
                      approval_mode="full", workspace_read_only=True)
    decision = decide("fs_read", {"path": "../outside.py"}, ctx)
    assert decision.verdict == VERDICT_DENY


def test_writing_members_serialize_overlapping_paths_and_verification():
    manager = SubagentManager(1)
    manager._handles[1] = SubagentHandle(agent_id=1, status="running", run_spec={
        "subagent_mode": "agent", "dispatch_scope": ["client/src"],
        "validation_resources": ["port:3000"],
    })
    assert manager.dispatch_conflict(("client/src/App.tsx",), ())
    assert manager.dispatch_conflict(("server/app/",), ("port:3000",))
    assert manager.dispatch_conflict((), ())
    assert manager.dispatch_conflict(("server/app/",), ("port:4000",)) is None

"""子代理角色与模式持久化/广播/REST 回填回归。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import settings
from app.gateway.routers.turns import list_session_subagents
from app.orchestration import agent_events, agent_loop, context_manager, subagent_tools
from app.persistence.database import async_session_factory
from app.persistence.models.agent import Agent
from app.persistence.models.message import Session
from app.persistence.models.project import Project
from app.persistence.models.turn import Turn


def test_spawn_schema_requires_mode_only_when_unrestricted():
    full_req = subagent_tools.SPAWN_SUBAGENT_SCHEMA["function"]["parameters"]["required"]
    assert "mode" in full_req
    restricted = subagent_tools._spawn_schema(True)["function"]["parameters"]
    assert "mode" not in restricted["properties"]
    assert "mode" not in restricted["required"]


@pytest.mark.parametrize(
    "perm_mode,args,expected_mode,expected_role",
    [
        ("plan", {"task_title": "调研文件面板", "task_description": "只读调研"}, "readonly", "explore"),
        ("readonly", {"task_title": "调研浏览器", "task_description": "只读调研"}, "readonly", "explore"),
        ("agent", {"task_title": "调研设置页", "task_description": "只读调研", "mode": "readonly"}, "readonly", "explore"),
        ("agent", {"task_title": "实现样式优化", "task_description": "修改代码", "mode": "agent"}, "agent", "coder"),
        ("agent", {"task_title": "界面自测", "task_description": "点按界面", "mode": "agent", "purpose": "operator"}, "agent", "operator"),
    ],
)
async def test_spawn_persists_and_broadcasts_subagent_role_and_mode(
    monkeypatch, tmp_path, perm_mode, args, expected_mode, expected_role,
):
    monkeypatch.setattr(settings, "subagent_model_id", 0)
    monkeypatch.setattr(settings, "operator_subagent_enabled", True)
    monkeypatch.setattr(context_manager, "build_subagent_context", AsyncMock(return_value=SimpleNamespace(messages=[])))
    broadcast_mock = AsyncMock()
    monkeypatch.setattr(agent_events, "broadcast", broadcast_mock)
    monkeypatch.setattr(
        subagent_tools,
        "available_subagent_tools",
        AsyncMock(return_value=[{"type": "function", "function": {"name": "fs_read"}}]),
    )

    manager = Mock()
    manager.duplicate_of.return_value = None
    manager.pending_count.return_value = 0
    manager.spawn.side_effect = lambda _db, **kw: kw["agent"].id

    async with async_session_factory() as db:
        project = Project(name="测试项目", path=str(tmp_path))
        db.add(project)
        await db.flush()
        session = Session(project_id=project.id, permission_mode=perm_mode, approval_mode="full")
        db.add(session)
        await db.flush()
        turn = Turn(session_id=session.id, status="running")
        db.add(turn)
        await db.commit()

        result = await agent_loop._run_subagent_tool(
            db,
            tool_name="spawn_subagent",
            args=dict(args),
            session_id=session.id,
            turn_id=turn.id,
            agent=None,
            workspace=str(tmp_path),
            subagent_context={
                "manager": manager,
                "project": project,
                "session": session,
                "permission_mode": perm_mode,
                "approval_mode": "full",
            },
        )
        assert f"mode={expected_mode}" in result

        started_calls = [
            call.args[1]
            for call in broadcast_mock.call_args_list
            if len(call.args) >= 2 and call.args[1].get("event") == "agent.started"
        ]
        assert started_calls, "应广播 agent.started"
        payload = started_calls[-1]["payload"]
        assert payload["subagent_mode"] == expected_mode
        assert payload["subagent_role"] == expected_role
        member_name = "探索智能体" if expected_mode == "readonly" else "执行智能体"
        assert payload["subagent_profile_name"] == member_name

        agent_id = payload["agent_id"]
        row = await db.get(Agent, agent_id)
        assert row is not None
        assert row.subagent_mode == expected_mode
        assert row.subagent_role == expected_role

        rows = await list_session_subagents(session.id, db=db)
        matched = next(r for r in rows if r["agent_id"] == agent_id)
        assert matched["subagent_mode"] == expected_mode
        assert matched["subagent_role"] == expected_role
        assert matched["subagent_profile_name"] == member_name

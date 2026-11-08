"""plan-20261105-subagent-tools：真实工具矩阵、拓展重建与 Ta3 参数往返。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.orchestration import subagent_tools
from app.orchestration.tools.registry import tool_registry
from app.models.providers.ta3_tool_aliases import disguise_args, restore_args, FROM_TA3
from app.models.providers.ta3_tool_schemas import disguise_tools, native_schema
from app.models.providers.ta3_mcp import mcp_alias, resolve_mcp_alias


def names(schemas):
    return {s["function"]["name"] for s in schemas}


@pytest.fixture
def tool_setup(monkeypatch):
    from app.services import skill_service

    monkeypatch.setattr(tool_registry, "_tools", dict(tool_registry._tools))
    monkeypatch.setattr(settings, "desktop_enabled", True)
    monkeypatch.setattr(settings, "browser_enabled", True)
    server = SimpleNamespace(
        name="test-addon", display_name="测试拓展", transport="stdio",
        command="test", args=[], env={}, url=None, path=None,
        tools=[
            {"name": "query", "annotations": {"riskLevel": "low"},
             "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
            {"name": "update", "annotations": {"riskLevel": "high"}},
        ],
    )
    loader = AsyncMock(return_value=[server])
    monkeypatch.setattr(skill_service, "get_global_mcp_servers", loader)
    return loader


async def test_agent_has_gui_and_fresh_extensions(tool_setup):
    schemas = await subagent_tools.available_subagent_tools(None)
    available = names(schemas)
    assert {"browser_navigate", "browser_click", "browser_type", "desktop_click",
            "desktop_screenshot", "terminal_exec", "mcp_test_addon_query",
            "mcp_test_addon_update"} <= available
    assert not (available & subagent_tools.SUBAGENT_FORBIDDEN)
    assert tool_registry.get("mcp_test_addon_query") is not None
    for schema in disguise_tools(schemas):
        alias = schema["function"]["name"]
        if alias.startswith("mcp__"):
            real = FROM_TA3.get(alias) or resolve_mcp_alias(alias)
            assert real in available
            assert tool_registry.get(real) is not None


async def test_readonly_has_observation_and_low_risk_extensions(tool_setup):
    available = names(await subagent_tools.available_subagent_tools(None, readonly=True))
    assert {"browser_navigate", "browser_snapshot", "browser_get_text", "browser_screenshot", "desktop_snapshot",
            "desktop_screenshot", "mcp_test_addon_query"} <= available
    assert not ({"browser_click", "browser_type", "browser_evaluate", "desktop_click",
                 "desktop_type", "terminal_exec", "fs_write", "mcp_test_addon_update"} & available)


async def test_extension_load_failure_keeps_builtins_without_stale_mcp(tool_setup):
    await subagent_tools.available_subagent_tools(None)
    tool_setup.side_effect = RuntimeError("测试连接失败")
    available = names(await subagent_tools.available_subagent_tools(None))
    assert "browser_click" in available
    assert not any(n.startswith("mcp_") for n in available)


async def test_disabled_extensions_do_not_leak_from_registry(tool_setup, monkeypatch):
    await subagent_tools.available_subagent_tools(None)
    tool_setup.return_value = []
    monkeypatch.setattr(settings, "desktop_enabled", False)
    monkeypatch.setattr(settings, "browser_enabled", False)
    available = names(await subagent_tools.available_subagent_tools(None))
    assert not any(n.startswith(("mcp_", "desktop_", "browser_")) for n in available)


async def test_restore_rebuilds_mode_tools_despite_legacy_snapshot(tool_setup, monkeypatch):
    from app.orchestration import subagent

    class Database:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(subagent, "async_session_factory", Database)
    schemas = await subagent._restore_tool_schemas({
        "tool_names": ["mcp_test_addon_query", "report_to_leader", "browser_click", "spawn_subagent"],
        "subagent_mode": "readonly",
    })
    assert {"mcp_test_addon_query", "report_to_leader", "desktop_screenshot", "browser_snapshot"} <= names(schemas)
    assert not {"browser_click", "spawn_subagent", "mcp_test_addon_update"} & names(schemas)
    assert resolve_mcp_alias(mcp_alias("mcp_test_addon_query")) == "mcp_test_addon_query"
    tool_setup.return_value = []
    restored = names(await subagent._restore_tool_schemas({
        "tool_names": ["fs_read"], "subagent_mode": "agent",
    }))
    assert {"desktop_recipe", "browser_click", "terminal_exec", "report_to_leader"} <= restored
    assert not any(name.startswith("mcp_") for name in restored)


def test_ta3_operator_roundtrip_and_readonly_entry():
    original = {"task_title": "操作网页", "task_description": "填写测试页面", "mode": "agent", "purpose": "operator"}
    wire = disguise_args("spawn_subagent", original)
    assert wire["purpose"] == "operator"
    assert restore_args("SubAgent", wire) == original
    assert restore_args("SubAgent", {**wire, "subagent_type": "Explore"})["mode"] == "agent"
    full = disguise_tools([subagent_tools.SPAWN_SUBAGENT_SCHEMA])[0]["function"]
    assert {"purpose", "mode"} <= set(full["parameters"]["properties"])
    assert "purpose" not in native_schema("SubAgent")["function"]["parameters"]["properties"]
    restricted = disguise_tools([subagent_tools._spawn_schema(True)])[0]["function"]
    assert "purpose" not in restricted["parameters"]["properties"]
    assert "mode" not in restricted["parameters"]["properties"]
    assert restricted["parameters"]["properties"]["subagent_type"]["enum"] == ["Explore", "Plan"]


@pytest.mark.parametrize("mode,ta3", [("agent", False), ("readonly", False), ("agent", True), ("readonly", True)])
async def test_real_dispatch_ignores_legacy_profile_whitelist(tool_setup, monkeypatch, tmp_path, mode, ta3):
    """plan-20261107-subagent-mode-tools：真实旧档案经过派发入口后仍按模式提供 GUI/MCP。"""
    from unittest.mock import Mock
    from app.orchestration import agent_loop, context_manager, agent_events
    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Session
    from app.persistence.models.project import Project
    from app.persistence.models.subagent_profile import SubagentProfile

    monkeypatch.setattr(settings, "subagent_model_id", 0)
    monkeypatch.setattr(settings, "operator_subagent_enabled", True)
    monkeypatch.setattr(context_manager, "build_subagent_context", AsyncMock(return_value=SimpleNamespace(messages=[])))
    monkeypatch.setattr(agent_events, "broadcast", AsyncMock())
    manager = Mock()
    manager.duplicate_of.return_value = None
    manager.pending_count.return_value = 0
    manager.spawn.return_value = 900
    async with async_session_factory() as db:
        project = Project(name="测试项目", path=str(tmp_path))
        db.add(project)
        await db.flush()
        session = Session(project_id=project.id, permission_mode="agent", approval_mode="full")
        db.add(session)
        db.add(SubagentProfile(name="general" if mode == "agent" else "explore",
                               tools_whitelist=["no_such_legacy_tool"], is_active=True))
        await db.commit()
        args = {"task_title": "核实工具", "task_description": "核实桌面浏览器与拓展", "mode": mode}
        if mode == "agent":
            args["purpose"] = "operator"
        if ta3:
            args = restore_args("SubAgent", disguise_args("spawn_subagent", args))
        result = await agent_loop._run_subagent_tool(
            db, tool_name="spawn_subagent", args=args, session_id=session.id, turn_id=1,
            agent=None, workspace=str(tmp_path), subagent_context={
                "manager": manager, "project": project, "session": session,
                "permission_mode": "agent", "approval_mode": "full",
            },
        )
    assert "dispatched" in result
    call = manager.spawn.call_args.kwargs
    assert call["subagent_mode"] == mode
    assert call["context_snapshot"]["approval_mode"] == "full"
    available = names(call["tool_schemas"])
    assert {"desktop_screenshot", "browser_snapshot", "mcp_test_addon_query", "skill_view", "report_to_leader"} <= available
    assert not available & subagent_tools.SUBAGENT_FORBIDDEN
    interactive = {"desktop_recipe", "desktop_click", "browser_click", "terminal_exec", "mcp_test_addon_update"}
    if mode == "agent":
        assert interactive <= available
    else:
        assert not interactive & available
    wire = disguise_tools(call["tool_schemas"])
    restored = {FROM_TA3.get(s["function"]["name"]) or resolve_mcp_alias(s["function"]["name"]) for s in wire}
    assert restored == available

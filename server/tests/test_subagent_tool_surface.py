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


async def test_profile_whitelist_can_select_gui_and_extensions(tool_setup):
    schemas = await subagent_tools.available_subagent_tools(None)
    selected = subagent_tools.filter_tool_schemas(schemas, ["browser_click", "mcp_test_addon_query"])
    assert names(selected) == {"browser_click", "mcp_test_addon_query"}


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


async def test_restore_registers_cold_extensions_and_keeps_original_scope(tool_setup, monkeypatch):
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
    assert names(schemas) == {"mcp_test_addon_query", "report_to_leader"}
    assert resolve_mcp_alias(mcp_alias("mcp_test_addon_query")) == "mcp_test_addon_query"
    tool_setup.return_value = []
    assert names(await subagent._restore_tool_schemas({
        "tool_names": ["mcp_test_addon_query", "report_to_leader"], "subagent_mode": "agent",
    })) == {"report_to_leader"}


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

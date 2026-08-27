"""v3.0 (plan-88): 子代理工具权限——tools_whitelist 过滤 spawn schema。

验证 filter_tool_schemas：勾选=允许（模型只见白名单 schema），留空=全量。
"""
import pytest

from app.orchestration.subagent_tools import EXPLORE_TOOLS, filter_tool_schemas
from app.orchestration.tools.registry import tool_registry


def _names(schemas: list[dict]) -> list[str]:
    return [s.get("function", {}).get("name", "") for s in schemas]


def test_filter_empty_whitelist_returns_all():
    schemas = tool_registry.all_schemas()
    out = filter_tool_schemas(schemas, None)
    assert _names(out) == _names(schemas)
    out2 = filter_tool_schemas(schemas, [])
    assert _names(out2) == _names(schemas)


def test_filter_whitelist_keeps_only_allowed():
    whitelist = ["fs_read", "fs_list", "fs_grep"]
    out = filter_tool_schemas(tool_registry.all_schemas(), whitelist)
    assert set(_names(out)) == set(whitelist)


def test_filter_whitelist_on_explore_tools():
    """探索子代理：既有只读范围再叠加白名单（交集）。"""
    schemas = tool_registry.all_schemas(EXPLORE_TOOLS)
    out = filter_tool_schemas(schemas, ["fs_read", "web_search", "terminal_exec"])
    assert set(_names(out)) == {"fs_read", "web_search"}  # terminal_exec 不在探索只读集


def test_filter_unknown_tool_names_are_dropped():
    out = filter_tool_schemas(tool_registry.all_schemas(), ["fs_read", "no.such.tool"])
    assert _names(out) == ["fs_read"]

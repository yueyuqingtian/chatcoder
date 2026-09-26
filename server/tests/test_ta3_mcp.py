"""ta3 模式下 MCP 工具伪装与权限门控单测（plan-230-1144 M1.3 Phase 2）。

覆盖三类回归：
1. 可见性——MCP 工具（mcp_<server>_<tool>）此前被 disguise_tools 整体剔除，
   ta3 会话里模型看不到也调不动；
2. 可用性——出站改名、入站还原（含历史调用与 Anthropic/OpenAI 两条协议路径）；
3. 权限——设置页白名单勾选对 MCP 工具生效（此前无条件注入）。
"""
import json

from app.models.providers.ta3_mcp import (
    describe, disguise_schema, is_mcp_alias, is_mcp_tool, mcp_alias, parse_server_tool,
)
from app.services import permission_profile_service as pps

# 真实 codegraph MCP 工具的包装名（McpToolWrapper 生成规则：mcp_<server>_<tool>）
CG_EXPLORE = "mcp_codegraph_codegraph_explore"
CG_TOOL_DIR = "server/app/orchestration/tools/mcp_wrapper.py"


# ───────────────────────── 命名与 schema 伪装 ─────────────────────────


def test_mcp_alias_naming():
    """mcp_<server>_<tool> → ta3 风格 PascalCase 伪装名。"""
    assert is_mcp_tool(CG_EXPLORE) is True
    assert is_mcp_tool("fs_read") is False
    assert mcp_alias(CG_EXPLORE) == "McpCodegraphCodegraphExplore"
    assert mcp_alias("mcp_codegraph_call") == "McpCodegraphCall"
    assert is_mcp_alias("McpCodegraphCodegraphExplore") is True
    # 静态映射表里的名字不当作 MCP 伪装名
    assert is_mcp_alias("MultiFileEdit") is False
    assert is_mcp_alias("Read") is False


def test_parse_server_tool_prefers_wrapper_tag():
    """服务/工具名优先取 McpToolWrapper 标注（名字里的下划线边界不可靠）。"""
    raw = "Explore the codegraph.\n[MCP Server: codegraph, Tool: codegraph_explore]"
    assert parse_server_tool(CG_EXPLORE, raw) == ("codegraph", "codegraph_explore")
    # 无标注时按首个下划线切分，仅用于展示
    assert parse_server_tool(CG_EXPLORE, "") == ("codegraph", "codegraph_explore")


def test_describe_is_chinese_and_drops_english_tag():
    raw = "Explore an area.\n[MCP Server: codegraph, Tool: codegraph_explore]"
    desc = describe(CG_EXPLORE, raw)
    assert "codegraph" in desc
    assert "MCP" in desc
    assert "[MCP Server:" not in desc  # 英文标注行不再外泄给模型
    assert "Explore an area." in desc  # 原始说明保留（不臆造能力）


def test_disguise_schema_keeps_input_schema():
    """参数 schema 原样透传（MCP 自带 inputSchema，不做臆测改写）。"""
    params = {
        "type": "object",
        "required": ["query"],
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
    }
    out = disguise_schema({
        "type": "function",
        "function": {"name": CG_EXPLORE, "description": "Explore area.", "parameters": params},
    })
    assert out["function"]["name"] == "McpCodegraphCodegraphExplore"
    assert out["function"]["parameters"] == params


def test_disguise_tools_keeps_mcp_tools():
    """disguise_tools：MCP 工具不再被剔除，静态映射工具行为不变。"""
    from app.models.providers.ta3_tool_schemas import disguise_tools

    schemas = [
        {"type": "function", "function": {"name": "fs_read", "parameters": {}}},
        {"type": "function", "function": {
            "name": CG_EXPLORE,
            "description": "Explore area.\n[MCP Server: codegraph, Tool: codegraph_explore]",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }},
        # 无映射且非 MCP → 仍剔除
        # v43: 原样本 web_fetch 已补映射（→WebFetch），改用真正未映射的名字保持覆盖
        {"type": "function", "function": {"name": "mystery_tool", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert names == ["Read", "McpCodegraphCodegraphExplore"]


# ───────────────────────── 出站/入站往返 ─────────────────────────


def test_resolve_mcp_alias_by_registry(monkeypatch):
    """伪装名 → 真实名：按 registry 反查（与 executor 查找源一致）。"""
    from app.models.providers import ta3_mcp

    monkeypatch.setattr(ta3_mcp, "_registered_mcp_names", lambda: [CG_EXPLORE])
    assert ta3_mcp.resolve_mcp_alias("McpCodegraphCodegraphExplore") == CG_EXPLORE
    assert ta3_mcp.resolve_mcp_alias("McpOtherTool") is None


def test_disguise_message_keeps_mcp_history_call():
    """历史 MCP 调用不再降级成"不可用"文本，而是改名为伪装名保留。"""
    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    args = {"query": "symbol_search"}
    m = ChatMessage(role="assistant", content=None,
                    tool_calls=[{"id": "c1", "name": CG_EXPLORE, "arguments": args}])
    out = provider._disguise_message(m)
    assert "不可用" not in (out.get("content") or "")
    assert out["tool_calls"][0]["function"]["name"] == "McpCodegraphCodegraphExplore"
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == args


def test_restore_tool_calls_maps_mcp_alias(monkeypatch):
    """入站：伪装名还原为真实 mcp_<server>_<tool>，参数原样透传。"""
    from app.models.providers import ta3_mcp
    from app.models.providers.ta3 import Ta3Provider

    monkeypatch.setattr(ta3_mcp, "_registered_mcp_names", lambda: [CG_EXPLORE])
    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    calls = provider._restore_tool_calls([
        {"id": "1", "name": "McpCodegraphCodegraphExplore", "arguments": {"query": "x", "limit": 5}},
        {"id": "2", "name": "Read", "arguments": {"filepath": "a.py"}},
    ])
    assert calls[0]["name"] == CG_EXPLORE
    assert calls[0]["arguments"] == {"query": "x", "limit": 5}
    # 静态映射工具不受影响
    assert calls[1]["name"] == "fs_read"


def test_restore_unknown_mcp_alias_keeps_name():
    """未注册的 MCP 伪装名保持原样（由执行层报未知工具，不静默吞掉）。"""
    from app.models.providers import ta3_mcp
    from app.models.providers.ta3 import Ta3Provider

    monkeypatch_saved = ta3_mcp._registered_mcp_names
    ta3_mcp._registered_mcp_names = lambda: []
    try:
        provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
        calls = provider._restore_tool_calls([
            {"id": "1", "name": "McpGhostTool", "arguments": {}},
        ])
        assert calls[0]["name"] == "McpGhostTool"
    finally:
        ta3_mcp._registered_mcp_names = monkeypatch_saved


# ───────────────────────── 权限白名单门控 ─────────────────────────


def _write_profiles(tmp_path, monkeypatch, profiles):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"permission_profiles": profiles}), encoding="utf-8")
    monkeypatch.setattr(pps, "_CFG_PATH", cfg)


def test_mcp_allowed_tools_default_unrestricted(tmp_path, monkeypatch):
    """agent（空列表 = 全量）→ None（不限制，全量注入）。

    plan-75-332: 内置模式改为 agent / readonly / plan；旧值 default、accept_edits
    由 _canonical 归一化到 agent，仍返回 None（存量会话不因改名而收紧工具）。
    """
    _write_profiles(tmp_path, monkeypatch, [])
    assert pps.mcp_allowed_tools("agent") is None
    assert pps.mcp_allowed_tools("default") is None
    assert pps.mcp_allowed_tools("accept_edits") is None
    # 未识别的模式名按 None 处理（engine 侧按默认语义）
    assert pps.mcp_allowed_tools("no_such_mode") is None


def test_mcp_allowed_tools_readonly_plan_unrestricted(tmp_path, monkeypatch):
    """只读/计划类 → None（低风险过滤由 engine 的 readonly_only 规则承担）。"""
    _write_profiles(tmp_path, monkeypatch, [])
    assert pps.mcp_allowed_tools("readonly") is None
    assert pps.mcp_allowed_tools("plan") is None


def test_mcp_allowed_tools_custom_profile_honours_selection(tmp_path, monkeypatch):
    """自定义模式勾选生效：只放行勾选的 MCP 工具。"""
    _write_profiles(tmp_path, monkeypatch, [{
        "name": "custom_mcp",
        "display_name": "自定义",
        "kind": "full",
        "tools": ["fs_read", CG_EXPLORE],
    }])
    allowed = pps.mcp_allowed_tools("custom_mcp")
    assert allowed == {"fs_read", CG_EXPLORE}

    # 勾选列表里没有 MCP 工具 → 集合不含任何 mcp_ 前缀项（调用方按集合过滤 = 全不注入）
    _write_profiles(tmp_path, monkeypatch, [{
        "name": "custom_nomcp",
        "display_name": "自定义2",
        "kind": "full",
        "tools": ["fs_read", "fs_write"],
    }])
    allowed2 = pps.mcp_allowed_tools("custom_nomcp")
    assert allowed2 == {"fs_read", "fs_write"}
    assert not any(t.startswith("mcp_") for t in allowed2)


def test_inject_mcp_tools_filters_by_allowed(monkeypatch):
    """engine._inject_mcp_tools：allowed 集合外的 MCP 工具不注入；只读模式仍只放低风险。"""
    import asyncio

    from app.orchestration import engine
    from app.orchestration.tools.registry import tool_registry

    class _FakeTool:
        def __init__(self, name, risk="medium"):
            self.name = name
            self.description = "fake"
            self.risk_level = risk

        def function_schema(self):
            return {"type": "function", "function": {"name": self.name, "parameters": {}}}

    fake_tools = [_FakeTool(CG_EXPLORE, "low"), _FakeTool("mcp_codegraph_call", "medium")]

    def _fake_build(_servers):
        # build_mcp_tools_for_agent 是同步函数（缓存清单，不做握手），fake 必须同形
        return list(fake_tools)

    monkeypatch.setattr(
        "app.orchestration.tools.mcp_wrapper.build_mcp_tools_for_agent", _fake_build,
    )

    async def _fake_servers(_db, _agent):
        return [object()]

    monkeypatch.setattr("app.services.skill_service.get_agent_mcp_servers", _fake_servers)

    async def _run(**kw):
        schemas: list = []
        n = await engine._inject_mcp_tools(None, None, schemas, 1, **kw)
        names = [s["function"]["name"] for s in schemas]
        return n, names

    # 不限制 → 两个都注入
    n, names = asyncio.run(_run())
    assert n == 2 and names == [CG_EXPLORE, "mcp_codegraph_call"]

    # 只读模式 → 仅低风险
    n, names = asyncio.run(_run(readonly_only=True))
    assert n == 1 and names == [CG_EXPLORE]

    # 白名单只勾了 explore → call 不注入
    n, names = asyncio.run(_run(allowed={CG_EXPLORE}))
    assert n == 1 and names == [CG_EXPLORE]

    # 白名单没勾任何 MCP 工具 → 全不注入
    n, names = asyncio.run(_run(allowed={"fs_read"}))
    assert n == 0 and names == []

    # 清理：注册到全局 registry 的假工具不应残留（避免影响其他用例）
    for t in fake_tools:
        tool_registry._tools.pop(t.name, None)

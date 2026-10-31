"""ta3 模式下 MCP 工具伪装与权限门控单测（plan-230-1144 / plan-598-2482 重写）。

覆盖四类回归：
1. 命名——真实 MCP 工具 `mcp_<server>_<tool>` 伪装成官方形态 `mcp__{server}__{tool}`，
   本项目独有工具伪装成 `mcp__workspace-tools__{真实名}`；
2. 描述——官方模板 `[MCP: {server}] {原文}`，不再用中文模板块、也不外泄 wrapper 标注；
3. 可用性——出站改名、入站还原（含历史调用与 Anthropic/OpenAI 两条协议路径）；
4. 权限——设置页白名单勾选对 MCP 工具生效（此前无条件注入）。
"""
import json

from app.models.providers.ta3_mcp import (
    EXTENSION_SERVER_ID,
    EXTENSION_SERVER_NAME,
    TA3_MCP_NAME_PREFIX,
    build_extension_tool,
    describe,
    disguise_schema,
    is_mcp_alias,
    is_mcp_tool,
    mcp_alias,
    resolve_mcp_alias,
    server_id_for,
    split_ta3_mcp_name,
    ta3_mcp_name,
)
from app.services import permission_profile_service as pps

# 真实 codegraph MCP 工具的包装名（McpToolWrapper 生成规则：mcp_<server>_<tool>）
CG_EXPLORE = "mcp_codegraph_codegraph_explore"
# plan-601：serverId 仿官方 store.js：`{小写名}-{稳定 base36 时间戳}`
CG_SID = server_id_for("codegraph")
CG_ALIAS = f"mcp__{CG_SID}__codegraph_explore"


# ───────────────────────── 命名与 schema 伪装 ─────────────────────────


def test_mcp_alias_naming():
    """mcp_<server>_<tool> → 官方形态 mcp__{server}__{tool}（plan-598-2482）。"""
    assert is_mcp_tool(CG_EXPLORE) is True
    assert is_mcp_tool("fs_read") is False
    assert mcp_alias(CG_EXPLORE) == CG_ALIAS
    assert mcp_alias("mcp_codegraph_call") == f"mcp__{CG_SID}__call"
    # serverId 形态：名字前缀 + 单个 base36 后缀，且同名每次相同
    assert CG_SID.startswith("codegraph-") and CG_SID == server_id_for("codegraph")
    # 非 MCP 名原样返回（调用方通常已用 is_mcp_tool 过滤）
    assert mcp_alias("fs_read") == "fs_read"
    # 伪装名判定只看官方双下划线前缀
    assert is_mcp_alias(CG_ALIAS) is True
    assert is_mcp_alias(TA3_MCP_NAME_PREFIX + f"{EXTENSION_SERVER_ID}__browser_click") is True
    assert is_mcp_alias("Read") is False
    assert is_mcp_alias("MultiFileEdit") is False


def test_ta3_mcp_name_roundtrip():
    """ta3_mcp_name / split_ta3_mcp_name 互逆；非法形态返回 ('', 原名)。"""
    assert ta3_mcp_name(CG_SID, "codegraph_explore") == CG_ALIAS
    assert split_ta3_mcp_name(CG_ALIAS) == (CG_SID, "codegraph_explore")
    assert split_ta3_mcp_name("Read") == ("", "Read")
    # 缺分隔符 → 视为非伪装形态
    assert split_ta3_mcp_name("mcp__onlyserver") == ("", "mcp__onlyserver")


def test_parse_server_tool_prefers_wrapper_tag():
    """服务/工具名优先取 McpToolWrapper 标注（名字里的下划线边界不可靠）。

    plan-598-2482: 内部函数改名为 `_split_real_mcp_name`，经 `mcp_alias` 对外体现。
    """
    raw = "Explore the codegraph.\n[MCP Server: codegraph, Tool: codegraph_explore]"
    assert mcp_alias(CG_EXPLORE) == CG_ALIAS
    # 无标注、未注册时按首个下划线切分（兑底）
    assert mcp_alias(CG_EXPLORE) == ta3_mcp_name(CG_SID, "codegraph_explore")
    # 标记存在时以标记为准：真实名与标注冲突也能正确切分
    from app.models.providers.ta3_mcp import _split_real_mcp_name

    assert _split_real_mcp_name(CG_EXPLORE, raw) == ("codegraph", "codegraph_explore")
    assert _split_real_mcp_name(CG_EXPLORE, "") == ("codegraph", "codegraph_explore")


def test_describe_uses_official_mcp_template():
    """官方描述模板：`[MCP: {server}] {原文}`，且不外泄 wrapper 英文标注。"""
    raw = "Explore an area.\n[MCP Server: codegraph, Tool: codegraph_explore]"
    desc = describe("codegraph", raw)
    assert desc == "[MCP: codegraph] Explore an area."
    assert "[MCP Server:" not in desc  # wrapper 标注行不再外泄给模型
    # 无描述时只留前缀（对齐官方 mcp/manager.js:268-270）
    assert describe("codegraph", "") == "[MCP: codegraph]"
    # 不再使用中文模板
    assert "调用 MCP 服务" not in desc


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
    assert out["function"]["name"] == CG_ALIAS
    assert out["function"]["parameters"] == params


def test_build_extension_tool_uses_workspace_tools_server():
    """本项目独有工具 → `mcp__workspace-tools__{真实名}`，描述挂官方 MCP 前缀。"""
    out = build_extension_tool({
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "点击页面元素。",
            "parameters": {"type": "object", "properties": {"ref": {"type": "string"}}},
        },
    })
    assert out["function"]["name"] == f"mcp__{EXTENSION_SERVER_ID}__browser_click"
    # 描述前缀用显示名（官方用 config.name），工具名用 serverId
    assert out["function"]["description"] == f"[MCP: {EXTENSION_SERVER_NAME}] 点击页面元素。"
    assert EXTENSION_SERVER_ID.startswith("workspace-tools-")
    assert "ref" in out["function"]["parameters"]["properties"]


def test_disguise_tools_keeps_mcp_tools():
    """disguise_tools：MCP 工具走官方形态，内置工具走快照，无映射仍剔除。"""
    from app.models.providers.ta3_tool_schemas import disguise_tools

    schemas = [
        {"type": "function", "function": {"name": "fs_read", "parameters": {}}},
        {"type": "function", "function": {
            "name": CG_EXPLORE,
            "description": "Explore area.\n[MCP Server: codegraph, Tool: codegraph_explore]",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }},
        # 本项目独有工具 → 拓展通道
        {"type": "function", "function": {"name": "browser_click",
                                          "description": "点击页面元素。", "parameters": {}}},
        # 无映射且非 MCP → 仍剔除
        {"type": "function", "function": {"name": "mystery_tool", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    # 内置在前；MCP 按 serverId\0toolName 字典序（codegraph-… < workspace-tools-…）
    assert names == ["Read", CG_ALIAS, f"mcp__{EXTENSION_SERVER_ID}__browser_click"]


# ───────────────────────── 出站/入站往返 ─────────────────────────


def _patch_registry(monkeypatch, names: list[str]) -> None:
    """把 registry 的 MCP 条目替换成固定清单（反查走确定性映射表）。"""
    from app.models.providers import ta3_mcp

    entries = sorted((n, "") for n in names)
    monkeypatch.setattr(ta3_mcp, "_registered_mcp_tools", lambda: list(entries))


def test_resolve_mcp_alias_by_registry(monkeypatch):
    """伪装名 → 真实名：按 registry 反查（与 executor 查找源一致）。"""
    _patch_registry(monkeypatch, [CG_EXPLORE])
    assert resolve_mcp_alias(CG_ALIAS) == CG_EXPLORE
    assert resolve_mcp_alias("mcp__other__tool") is None
    # 旧命名（无 serverId 后缀）不再被猜测还原
    assert resolve_mcp_alias("mcp__codegraph__codegraph_explore") is None


def test_resolve_extension_alias_without_registry():
    """拓展名直接按 server 段还原，不依赖 registry 是否注册。"""
    assert resolve_mcp_alias(f"mcp__{EXTENSION_SERVER_ID}__browser_click") == "browser_click"
    assert resolve_mcp_alias(f"mcp__{EXTENSION_SERVER_ID}__multi_file_edit") == "multi_file_edit"
    # 只认映射表里的拓展工具，不再把任意 tool 段当真实名
    assert resolve_mcp_alias(f"mcp__{EXTENSION_SERVER_ID}__no_such_tool") is None


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
    assert out["tool_calls"][0]["function"]["name"] == CG_ALIAS
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == args


def test_restore_tool_calls_maps_mcp_alias(monkeypatch):
    """入站：伪装名还原为真实 mcp_<server>_<tool>，参数原样透传。"""
    from app.models.providers.ta3 import Ta3Provider

    _patch_registry(monkeypatch, [CG_EXPLORE])
    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    calls = provider._restore_tool_calls([  # noqa: E501
        {"id": "1", "name": CG_ALIAS, "arguments": {"query": "x", "limit": 5}},
        {"id": "2", "name": "Read", "arguments": {"filepath": "a.py"}},
    ])
    assert calls[0]["name"] == CG_EXPLORE
    assert calls[0]["arguments"] == {"query": "x", "limit": 5}
    # 内置通道工具不受影响
    assert calls[1]["name"] == "fs_read"


def test_restore_tool_calls_maps_extension_alias():
    """入站：拓展伪装名还原为本项目真实工具名（不依赖 registry）。"""
    from app.models.providers.ta3 import Ta3Provider

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    calls = provider._restore_tool_calls([
        {"id": "1", "name": f"mcp__{EXTENSION_SERVER_ID}__browser_click", "arguments": {"ref": "e1"}},
        {"id": "2", "name": f"mcp__{EXTENSION_SERVER_ID}__multi_file_edit", "arguments": {"edits": []}},
    ])
    assert calls[0]["name"] == "browser_click"
    assert calls[0]["arguments"] == {"ref": "e1"}
    assert calls[1]["name"] == "multi_file_edit"


def test_restore_unknown_mcp_alias_keeps_name(monkeypatch):
    """未注册的 MCP 伪装名保持原样（由执行层报未知工具，不静默吞掉）。"""
    from app.models.providers.ta3 import Ta3Provider

    _patch_registry(monkeypatch, [])
    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    calls = provider._restore_tool_calls([
        {"id": "1", "name": "mcp__ghost__tool", "arguments": {}},
    ])
    assert calls[0]["name"] == "mcp__ghost__tool"


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

    # 不限制 → 两个都注入（注入的是真实名，伪装发生在 ta3 出站层）
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

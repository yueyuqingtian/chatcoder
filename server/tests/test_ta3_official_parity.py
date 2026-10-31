"""ta3 工具面与官方客户端的对齐守护（plan-598-2482）。

**为什么需要这个文件**：重构前 ta3 把本项目全部 60 个工具都改名下发（`TO_TA3` 有 75 条
PascalCase 映射），而官方客户端在普通 agent 会话里只下发「官方内置工具 + MCP 外部工具」。
上行工具集合与官方差异过大本身就是一种可识别的体系特征。

重构后的契约全部收在本文件里，任何一条被破坏都会立刻失败：
1. `TO_TA3` 的值 ⊆ `TA3_OFFICIAL_BUILTIN_TOOLS`（内置通道只许用官方名）；
2. `TA3_NATIVE_SCHEMAS` 的键集合 == `set(TO_TA3.values())`（快照不许多、不许少）；
3. `TO_TA3_EXTENSION` 的值一律 `mcp__workspace-tools__{真实名}`；
4. `disguise_tools` 三路分派的实际产出（内置快照 / 拓展 MCP / 真实 MCP / 剔除）；
5. 出站请求头不含 `User-Agent`（对齐官方 `modelClient.js`）。
"""
from __future__ import annotations

from app.core.config import settings
from app.models.providers.ta3_mcp import EXTENSION_SERVER_ID, EXTENSION_SERVER_NAME, mcp_alias, ta3_mcp_name
from app.models.providers.ta3_official_tools import (
    TA3_OFFICIAL_BUILTIN_TOOLS,
    is_official_builtin,
)
from app.models.providers.ta3_tool_aliases import TO_TA3, TO_TA3_EXTENSION
from app.models.providers.ta3_tool_schemas import TA3_NATIVE_SCHEMAS, disguise_tools


def _fn(name: str, description: str = "", parameters: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }


def _names(schemas: list[dict]) -> list[str]:
    return [s["function"]["name"] for s in schemas]


# ───────────────────────── 一、官方内置白名单 ─────────────────────────


def test_official_whitelist_shape():
    """官方白名单本身：只放官方 function.name，且都是非空字符串。"""
    assert isinstance(TA3_OFFICIAL_BUILTIN_TOOLS, frozenset)
    assert TA3_OFFICIAL_BUILTIN_TOOLS, "官方内置工具清单不能为空"
    assert all(isinstance(n, str) and n.strip() for n in TA3_OFFICIAL_BUILTIN_TOOLS)
    # 大小写敏感是契约的一部分：官方既有 PascalCase 也有 snake_case
    assert "Read" in TA3_OFFICIAL_BUILTIN_TOOLS
    # plan-598-2482：白名单只收「本项目真实存在且官方也有」的工具，
    # get_file_outline 是官方 snake_case 名的代表。
    assert "get_file_outline" in TA3_OFFICIAL_BUILTIN_TOOLS
    assert "GetFileOutline" not in TA3_OFFICIAL_BUILTIN_TOOLS


def test_is_official_builtin():
    """is_official_builtin：命中返回 True，非法输入返回 False（不抛异常）。"""
    assert is_official_builtin("Read") is True
    assert is_official_builtin("get_file_outline") is True
    assert is_official_builtin("DesktopClick") is False
    assert is_official_builtin("mcp__workspace-tools__browser_click") is False
    assert is_official_builtin("") is False
    assert is_official_builtin(None) is False


def test_to_ta3_values_are_official_builtins():
    """内置通道的每个目标名都在官方白名单内（TO_TA3 的运行时自检同口径）。"""
    assert TO_TA3, "内置通道映射不能为空"
    leaked = sorted(set(TO_TA3.values()) - TA3_OFFICIAL_BUILTIN_TOOLS)
    assert not leaked, f"TO_TA3 的目标名不在官方白名单内：{leaked}"
    for real_name, alias in TO_TA3.items():
        assert is_official_builtin(alias), f"{real_name} → {alias} 不是官方内置名"


def test_extension_values_use_workspace_tools_mcp_shape():
    """拓展通道的每个目标名都是 `mcp__workspace-tools__{真实名}`。"""
    assert TO_TA3_EXTENSION, "拓展通道映射不能为空"
    for real_name, alias in TO_TA3_EXTENSION.items():
        assert alias == ta3_mcp_name(EXTENSION_SERVER_ID, real_name), (
            f"{real_name} 的拓展名不符合官方 MCP 形态: {alias}"
        )
        assert alias.startswith(f"mcp__{EXTENSION_SERVER_ID}__")
        # 拓展名不得落进官方内置白名单（通道边界必须清晰）
        assert alias not in TA3_OFFICIAL_BUILTIN_TOOLS


def test_channels_are_disjoint():
    """内置与拓展两条通道的真实名互斥，避免同一工具出现两种上行形态。"""
    assert not (set(TO_TA3) & set(TO_TA3_EXTENSION))
    assert not (set(TO_TA3.values()) & set(TO_TA3_EXTENSION.values()))


# ───────────────────────── 二、快照与内置映射等价 ─────────────────────────


def test_native_schemas_keys_equal_to_ta3_values():
    """`TA3_NATIVE_SCHEMAS` 的键集合 == `set(TO_TA3.values())`。

    快照多一项 = 下发了没有真实工具支撑的工具（模型会调用一个不存在的工具）；
    少一项 = 有映射却拿不到 schema，`disguise_tools` 里 continue 掉，工具静默消失。
    """
    assert set(TA3_NATIVE_SCHEMAS) == set(TO_TA3.values())
    for name, schema in TA3_NATIVE_SCHEMAS.items():
        assert schema["function"]["name"] == name, f"快照键与内部名不一致: {name}"
        assert schema["type"] == "function"


def test_native_schemas_all_official():
    """快照里的每个名字都必须是官方内置名。"""
    assert set(TA3_NATIVE_SCHEMAS) <= TA3_OFFICIAL_BUILTIN_TOOLS
    # 官方有、本项目没有对应实现的工具允许缺快照，但差额必须可解释（当前为
    # RevertFile / single_find_and_replace——后者以入站别名形式存在）
    missing = TA3_OFFICIAL_BUILTIN_TOOLS - set(TA3_NATIVE_SCHEMAS)
    assert missing <= {"RevertFile", "single_find_and_replace"}, (
        f"官方白名单与快照的差额出现意外项: {sorted(missing)}"
    )


def test_native_schemas_have_usable_parameters():
    """每个快照都必须带可用的 parameters 结构（否则模型无法构造入参）。"""
    for name, schema in TA3_NATIVE_SCHEMAS.items():
        params = schema["function"]["parameters"]
        assert isinstance(params, dict), f"{name} 的 parameters 不是对象"
        assert params.get("type") == "object", f"{name} 的 parameters 缺 type=object"
        assert "properties" in params, f"{name} 的 parameters 缺 properties"


# ───────────────────────── 三、disguise_tools 三路分派 ─────────────────────────


def test_disguise_tools_dispatch_three_paths():
    """样本输入的四路结果：内置快照 / 拓展 MCP / 真实 MCP / 被剔除。"""
    out = disguise_tools([
        _fn("fs_read", "真实描述（会被快照替换）"),
        _fn("browser_click", "点击页面元素。"),
        _fn("mcp_codegraph_codegraph_explore", "Explore an area."),
        _fn("mystery_tool", "无映射"),
    ])
    cg = mcp_alias("mcp_codegraph_codegraph_explore")
    # 内置在前；MCP 按 serverId 字典序（codegraph-… < workspace-tools-…）
    assert _names(out) == ["Read", cg, f"mcp__{EXTENSION_SERVER_ID}__browser_click"]
    # ① 内置通道：用官方快照（不是入参 schema）
    assert out[0] is TA3_NATIVE_SCHEMAS["Read"]
    assert out[0]["function"]["description"] != "真实描述（会被快照替换）"
    # ② 真实 MCP：`mcp__{serverId}__{tool}`，描述前缀用显示名
    assert cg.startswith("mcp__codegraph-")
    assert out[1]["function"]["description"].startswith("[MCP: codegraph] ")
    # ③ 拓展通道：官方 MCP 模板 + 原样参数
    assert out[2]["function"]["description"] == f"[MCP: {EXTENSION_SERVER_NAME}] 点击页面元素。"
    # ④ 无映射：完全剔除（不在结果里，也不会退化成空壳工具）
    assert "mystery_tool" not in _names(out)


def test_disguise_tools_unmapped_only_returns_empty():
    """全部无映射时返回空列表，而不是抛异常或塞占位项。"""
    assert disguise_tools([_fn("mystery_tool"), _fn("another_unknown")]) == []
    assert disguise_tools([]) == []


def test_disguise_tools_builtin_ignores_real_parameters():
    """内置通道的参数一律取官方快照，真实 schema 的参数不参与（避免两套键名混用）。"""
    out = disguise_tools([_fn("fs_read", parameters={
        "type": "object", "properties": {"path": {"type": "string"}},
    })])
    props = out[0]["function"]["parameters"]["properties"]
    # 官方键名是 filepath，真实键名 path 不应出现
    assert "filepath" in props
    assert "path" not in props


def test_disguise_tools_extension_keeps_real_parameters():
    """拓展通道的参数从真实 schema 派生（真实工具加参数，ta3 侧自动可见）。"""
    params = {
        "type": "object",
        "required": ["element"],
        "properties": {"element": {"type": "string"}, "then_shot": {"type": "boolean"}},
    }
    out = disguise_tools([_fn("desktop_click", "点击鼠标。", params)])
    assert _names(out) == [f"mcp__{EXTENSION_SERVER_ID}__desktop_click"]
    assert out[0]["function"]["parameters"] == params


def test_disguise_tools_real_mcp_uses_official_naming():
    """真实 MCP 工具按 `mcp__{server}__{tool}` 命名，且参数透传。"""
    params = {"type": "object", "properties": {"query": {"type": "string"}}}
    out = disguise_tools([_fn("mcp_codegraph_codegraph_explore", "Explore.", params)])
    assert _names(out) == [mcp_alias("mcp_codegraph_codegraph_explore")]
    assert _names(out)[0].startswith("mcp__codegraph-") and _names(out)[0].endswith("__codegraph_explore")
    assert out[0]["function"]["parameters"] == params


# ───────────────────────── 四、出站请求头对齐 ─────────────────────────


def test_base_headers_have_no_user_agent(monkeypatch):
    """`_base_headers` 不下发 User-Agent（官方 modelClient.js 两个 build*Headers 都未设置）。

    plan-598-2482: 本项目此前硬编码 Chrome/126 + Electron/31，版本号长期固定，
    是与官方可区分的稳定指纹。
    """
    from app.models.providers.ta3 import Ta3Provider

    monkeypatch.setattr(settings, "ta3_send_user_agent", False)
    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    headers = provider._base_headers()
    assert "User-Agent" not in headers
    assert "user-agent" not in {k.lower() for k in headers}
    # 其他伪装头仍在（官方会带 Call-Source / appId / 工作区指纹）
    assert headers["X-Call-Source"] == "APP"
    assert headers["Content-Type"] == "application/json"


def test_base_headers_user_agent_only_when_enabled(monkeypatch):
    """开关打开后才补 User-Agent（保留应急伪装能力）。"""
    from app.models.providers.ta3 import Ta3Provider

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    monkeypatch.setattr(settings, "ta3_send_user_agent", True)
    headers = provider._base_headers()
    assert headers.get("User-Agent") == provider._ua


def test_base_headers_anthropic_branch_has_no_user_agent(monkeypatch):
    """Anthropic 协议分支同样不含 User-Agent，且保留 anthropic-version / x-api-key。"""
    from app.models.providers.ta3 import Ta3Provider

    monkeypatch.setattr(settings, "ta3_send_user_agent", False)
    provider = Ta3Provider(
        api_key="llm-x", base_url="https://x", model="m",
        meta={"anthropic": True, "orgId": "org-123"},
    )
    headers = provider._base_headers()
    assert "User-Agent" not in headers
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["x-api-key"] == "llm-x"
    # appId 伪装头（plan-270-1358）不受本次改动影响
    assert headers["lcappid"] == "org-123"

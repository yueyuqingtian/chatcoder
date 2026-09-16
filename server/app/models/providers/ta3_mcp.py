"""ta3 模式下 MCP 工具的伪装适配（plan-230-1144 M1.3 的 Phase 2 补完）。

背景：MCP 工具在 registry 里的真实名形如 `mcp_codegraph_codegraph_explore`
（`mcp_wrapper.McpToolWrapper`，点号已替换为下划线）。ta3 伪装层的两张表
（`TO_TA3` / `TA3_NATIVE_SCHEMAS`）是静态的，MCP 工具名随用户配置动态变化，
因此它们此前被 `disguise_tools` 整体剔除——ta3 会话里模型既看不到、也调不动
MCP 工具（方案 §5.4 当时的口径是"mcp_* 默认剔除，Phase 2 再议"，本文即 Phase 2）。

本模块提供三件事：
1. 伪装名：`mcp_codegraph_codegraph_explore` → `McpCodegraphCodegraphExplore`
   （PascalCase，对齐 ta3 原生命名风格）；
2. 反查：伪装名 → 真实名。转换不可逆（下划线边界丢失），故反查不做字符串还原，
   而是扫描 registry 中已注册的 MCP 工具逐个比对伪装名——registry 正是 executor
   执行工具时的查找源，二者天然一致，不存在"伪装名能反查但执行时找不到"的漂移；
3. schema 伪装：名字换伪装名、描述改写成中文风格，参数 schema 原样透传
   （MCP 自带 inputSchema，不做臆测改写——plan-609 口径）。
"""
from __future__ import annotations

import re

MCP_PREFIX = "mcp_"
# 真实描述末尾由 McpToolWrapper 附加的服务/工具标注：
# "\n[MCP Server: codegraph, Tool: codegraph_explore]"
_TAG_RE = re.compile(r"\[MCP Server:\s*([^,\]]+),\s*Tool:\s*([^\]]+)\]")


def is_mcp_tool(name: str) -> bool:
    """真实执行名是否为 MCP 工具（`mcp_<server>_<tool>`）。"""
    return str(name or "").startswith(MCP_PREFIX)


def mcp_alias(real_name: str) -> str:
    """真实名 → ta3 风格伪装名。

    `mcp_codegraph_codegraph_explore` → `McpCodegraphCodegraphExplore`；
    非 MCP 名原样返回（调用方通常已用 is_mcp_tool 过滤）。
    """
    parts = [p for p in str(real_name or "").split("_") if p]
    if not parts:
        return str(real_name or "")
    return "".join(p[:1].upper() + p[1:] for p in parts)


def is_mcp_alias(name: str) -> bool:
    """疑似 MCP 伪装名：以 Mcp 开头且非静态映射表里的名字。"""
    from app.models.providers.ta3_tool_aliases import FROM_TA3

    n = str(name or "")
    return n.startswith("Mcp") and n not in FROM_TA3


def _registered_mcp_names() -> list[str]:
    """registry 中已注册的 MCP 工具真实名（失败返回空表，不抛）。"""
    try:
        from app.orchestration.tools.registry import tool_registry

        return sorted(t.name for t in tool_registry.all() if is_mcp_tool(t.name))
    except Exception:  # pragma: no cover - 导入失败不应影响出/入站转换
        return []


def resolve_mcp_alias(alias: str) -> str | None:
    """伪装名 → 真实执行名；未命中返回 None（由调用方保持原样）。"""
    target = str(alias or "")
    if not target:
        return None
    for real in _registered_mcp_names():
        if mcp_alias(real) == target:
            return real
    return None


def parse_server_tool(real_name: str, raw_desc: str = "") -> tuple[str, str]:
    """解析出 (server, tool)：优先用描述里的 McpToolWrapper 标注，回退解析工具名。

    名字里的下划线边界不可靠（server/tool 自身可能含下划线），故标注优先；
    无标注时按首个下划线切分，仅用于展示文案。
    """
    m = _TAG_RE.search(str(raw_desc or ""))
    if m:
        return m.group(1).strip(), m.group(2).strip()
    rest = str(real_name or "")[len(MCP_PREFIX):]
    server, _, tool = rest.partition("_")
    return server or rest, tool or rest


def describe(real_name: str, raw_desc: str = "") -> str:
    """ta3 风格中文描述（保留 MCP 原始描述，去掉英文标注行）。"""
    server, tool = parse_server_tool(real_name, raw_desc)
    body = _TAG_RE.sub("", str(raw_desc or "")).strip() or "（该 MCP 工具未提供描述）"
    return (
        f"调用 MCP 服务「{server}」提供的工具 {tool}。"
        f"工具说明：{body}"
        " 由 MCP Server 实际执行并返回文本结果；参数 schema 由该 MCP 工具定义。"
    )


def disguise_schema(schema: dict) -> dict:
    """真实 MCP 工具 schema → ta3 风格 schema（名字伪装 + 描述中文化，参数透传）。"""
    fn = schema.get("function") or {}
    real_name = str(fn.get("name") or "")
    return {
        "type": "function",
        "function": {
            "name": mcp_alias(real_name),
            "description": describe(real_name, str(fn.get("description") or "")),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        },
    }

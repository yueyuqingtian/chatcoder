"""ta3 模式下 MCP 工具的伪装适配（plan-598-2482 重写，plan-601 命名对齐）。

背景：MCP 工具在 registry 里的真实名形如 `mcp_codegraph_codegraph_explore`
（`mcp_wrapper.McpToolWrapper`，`.`/`-` 已替换为下划线，**不可逆**）。ta3 伪装层把这类
工具改名后下发，同时本项目独有的工具（浏览器、电脑操控、工作流等）统一挂到一个拓展
MCP 服务上，命名与描述都对齐官方客户端（Ta+3 牛码 0.7.5）的 MCP 形态。

对齐官方的四个点：
1. 工具名 `mcp__{serverId}__{toolName}`（官方 `mcp/manager.js:263`、`plugin/mcpRegistry.js:79`）；
2. serverId 形态 `{名称非法字符换 -、小写}-{base36 毫秒时间戳}`（官方 `mcp/store.js:95-97`）。
   官方时间戳取创建时刻；这里用服务器名的稳定哈希派生一个落在 2025 年内的毫秒值——
   plan-601：每次请求都变会破坏前缀缓存，同名服务器必须每次得到同一个 serverId；
3. 描述 `[MCP: {显示名}] {原始描述}`，无描述时只留前缀（官方 `manager.js:268-270`）；
4. 入站还原用**确定性映射表**：拿同一套出站生成函数对 registry 里全部真 MCP 工具与
   拓展工具算一遍再反查——plan-601：此前按 `_` 切分猜服务名，服务器名含 `-_.`、
   工具名含 `.` 时都会还原失败。
"""
from __future__ import annotations

import hashlib
import re

# 真实执行名前缀（`mcp_<server>_<tool>`，见 `mcp_wrapper.McpToolWrapper`）
MCP_PREFIX = "mcp_"

# 官方客户端的 MCP 伪装名分隔符：`mcp__{serverId}__{toolName}`。
TA3_MCP_NAME_PREFIX = "mcp__"
TA3_MCP_NAME_INFIX = "__"

# serverId 时间戳后缀的派生区间：2025-01-01T00:00:00Z 起一年内（与官方真实创建时间同量级）
_SERVER_ID_EPOCH_MS = 1735689600000
_SERVER_ID_SPAN_MS = 365 * 24 * 3600 * 1000
_SERVER_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")
# function.name 只允许 [a-zA-Z0-9_-]；MCP 工具名里的其它字符（典型是 `.`）按此替换
_TOOL_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")

# 真实描述末尾由 McpToolWrapper 附加的服务/工具标注：
# "\n[MCP Server: codegraph, Tool: codegraph_explore]"
_TAG_RE = re.compile(r"\[MCP Server:\s*([^,\]]+),\s*Tool:\s*([^\]]+)\]")
# McpToolWrapper 在原始描述为空时填的中文模板（官方对空描述只留前缀，这里视同空描述）
_EMPTY_TEMPLATE_RE = re.compile(r"^MCP 工具: .+ \(来自 .+\)$")


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n <= 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def make_server_id(name: str, *, salt: str = "") -> str:
    """仿官方 `generateServerId`：非法字符换 `-`、小写、加 `-{base36 时间戳}`。

    时间戳由名字的 sha256 稳定派生（同名每次相同），不随请求变化。
    """
    raw = str(name or "")
    safe = _SERVER_SAFE_RE.sub("-", raw).lower()
    digest = hashlib.sha256((salt + raw).encode("utf-8")).digest()
    ts = _SERVER_ID_EPOCH_MS + int.from_bytes(digest[:8], "big") % _SERVER_ID_SPAN_MS
    return f"{safe}-{_base36(ts)}"


# 本项目独有工具挂靠的 MCP 服务：显示名用中性名，serverId 与真 MCP 服务同一形态
EXTENSION_SERVER_NAME = "workspace-tools"
EXTENSION_SERVER_ID = make_server_id(EXTENSION_SERVER_NAME)


def server_id_for(server_name: str) -> str:
    """真 MCP 服务的 serverId；与拓展通道撞名时换盐重算，保证两者永不重名。"""
    sid = make_server_id(server_name)
    if sid == EXTENSION_SERVER_ID:
        sid = make_server_id(server_name, salt="mcp:")
    return sid


def safe_tool_name(tool_name: str) -> str:
    """MCP 工具名 → function.name 合法片段（非法字符换 `_`）。可逆性由映射表保证。"""
    return _TOOL_SAFE_RE.sub("_", str(tool_name or ""))


def is_mcp_tool(name: str) -> bool:
    """真实执行名是否为 MCP 工具（`mcp_<server>_<tool>`）。

    注意：伪装名以 `mcp__` 开头同样满足 `startswith("mcp_")`；
    这里只用于判断"真实执行名"，调用方传入的都是 registry 里的真实名。
    """
    return str(name or "").startswith(MCP_PREFIX)


def is_ta3_mcp_name(name: str) -> bool:
    """是否为 ta3 上行形态的 MCP 伪装名（`mcp__{server}__{tool}`）。"""
    return str(name or "").startswith(TA3_MCP_NAME_PREFIX)


def ta3_mcp_name(server_id: str, tool_name: str) -> str:
    """构造官方形态的 MCP 工具名：`mcp__{server_id}__{tool_name}`。"""
    return f"{TA3_MCP_NAME_PREFIX}{server_id}{TA3_MCP_NAME_INFIX}{tool_name}"


def split_ta3_mcp_name(name: str) -> tuple[str, str]:
    """拆 `mcp__{server}__{tool}` → (server, tool)；不符合形态时返回 ('', 原名)。

    与官方 `getMcpServerIdFromToolName` 同口径：serverId 是第一个 `__` 之前的部分。
    """
    raw = str(name or "")
    if not raw.startswith(TA3_MCP_NAME_PREFIX):
        return "", raw
    rest = raw[len(TA3_MCP_NAME_PREFIX):]
    server, sep, tool = rest.partition(TA3_MCP_NAME_INFIX)
    if not sep:
        return "", raw
    return server, tool


def is_mcp_alias(name: str) -> bool:
    """是否为本模块产出的 MCP 伪装名。"""
    return is_ta3_mcp_name(name)


# ─────────────────────────── 真 MCP 工具的出站命名 ───────────────────────────


def _split_real_mcp_name(real_name: str, raw_desc: str = "") -> tuple[str, str]:
    """真实名 → (服务器显示名, MCP 原始工具名)。

    优先级：registry 中 McpToolWrapper 的原始字段 > 描述标注 > 首个下划线切分（兜底）。
    前两者保留了 `-`、`.` 等被真实名抹掉的字符。
    """
    tool = _registry_tool(real_name)
    if tool is not None:
        server = str(getattr(tool, "_server_name", "") or "")
        name = str(getattr(tool, "_tool_name", "") or "")
        if server and name:
            return server, name
        if not raw_desc:
            raw_desc = str(getattr(tool, "description", "") or "")
    m = _TAG_RE.search(str(raw_desc or ""))
    if m:
        return m.group(1).strip(), m.group(2).strip()
    rest = str(real_name or "")[len(MCP_PREFIX):]
    server, _, name = rest.partition("_")
    return server or rest, name or rest


def _registry_tool(real_name: str):
    try:
        from app.orchestration.tools.registry import tool_registry

        return tool_registry.get(real_name) if is_mcp_tool(real_name) else None
    except Exception:  # pragma: no cover - 导入失败不应影响出/入站转换
        return None


def _registered_mcp_tools() -> list[tuple[str, str]]:
    """registry 中已注册的 MCP 工具 → [(真实名, 原始描述)]（失败返回空表）。"""
    try:
        from app.orchestration.tools.registry import tool_registry

        out: list[tuple[str, str]] = []
        for t in tool_registry.all():
            name = str(getattr(t, "name", "") or "")
            if is_mcp_tool(name):
                out.append((name, str(getattr(t, "description", "") or "")))
        return sorted(out)
    except Exception:  # pragma: no cover
        return []


def mcp_parts(real_name: str, raw_desc: str = "") -> tuple[str, str, str, str]:
    """真实 MCP 名 → (出站名, serverId, MCP 原始工具名, 服务器显示名)。

    出站名优先查确定性映射表（处理同服务器内工具名清洗后撞名的情况），
    不在表里（例如尚未注册的调用方构造的 schema）时按同一规则直接计算。
    """
    server, tool = _split_real_mcp_name(real_name, raw_desc)
    sid = server_id_for(server)
    alias = build_mcp_name_map().get(real_name) or ta3_mcp_name(sid, safe_tool_name(tool))
    return alias, sid, tool, server


# 映射表缓存：键是 registry 的 (真实名, 描述) 快照，registry 不变时直接复用
_NAME_MAP_CACHE: dict[tuple, dict[str, str]] = {}


def build_mcp_name_map() -> dict[str, str]:
    """真 MCP 工具的「真实名 → 出站名」确定性映射（registry 全量）。

    按真实名排序后逐个计算；同一出站名被占用时追加 `_2`、`_3`……（排序稳定，
    同一组工具每次得到同一结果，出站与入站共用这一张表）。
    """
    entries = tuple(_registered_mcp_tools())
    cached = _NAME_MAP_CACHE.get(entries)
    if cached is not None:
        return cached
    used: set[str] = set()
    out: dict[str, str] = {}
    for real, desc in entries:
        if real in out:
            continue
        server, tool = _split_real_mcp_name(real, desc)
        base = ta3_mcp_name(server_id_for(server), safe_tool_name(tool))
        alias, n = base, 1
        while alias in used:
            n += 1
            alias = f"{base}_{n}"
        used.add(alias)
        out[real] = alias
    _NAME_MAP_CACHE.clear()  # 只保留最新快照，避免 MCP 服务反复增删时缓存无限增长
    _NAME_MAP_CACHE[entries] = out
    return out


def mcp_alias(real_name: str) -> str:
    """真实 MCP 名 → ta3 上行伪装名（历史回放与当轮 schema 用同一个函数）。

    非 MCP 名原样返回（调用方通常已用 is_mcp_tool 过滤）。
    """
    raw = str(real_name or "")
    if not is_mcp_tool(raw):
        return raw
    return mcp_parts(raw)[0]


def resolve_mcp_alias(alias: str) -> str | None:
    """伪装名 → 真实执行名；未命中返回 None（由调用方保持原样，执行层报未知工具）。

    只查确定性映射表，不做字符串切分猜测：
    ① 拓展通道 —— `TO_TA3_EXTENSION` 反查；② 真 MCP —— registry 全量出站名反查。
    """
    target = str(alias or "")
    if not target:
        return None
    from app.models.providers.ta3_tool_aliases import TO_TA3_EXTENSION

    for real, out in TO_TA3_EXTENSION.items():
        if out == target:
            return real
    for real, out in build_mcp_name_map().items():
        if out == target:
            return real
    return None


# ─────────────────────────── 描述 ───────────────────────────

# 描述内需要中性化的产品痕迹：应用数据目录与应用名
_APPDATA_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s'\"）)]+[\\/]+AppData[\\/]+Local[\\/]+chatcoder[\\/]+",
    re.IGNORECASE,
)
_APPDATA_TAIL_RE = re.compile(r"AppData[\\/]+Local[\\/]+chatcoder[\\/]*", re.IGNORECASE)
_PRODUCT_RE = re.compile(r"chatcoder", re.IGNORECASE)


def _sanitize_desc_text(raw_desc: str) -> str:
    """去掉 McpToolWrapper 标注、空描述模板与产品痕迹，保留功能描述。"""
    body = _TAG_RE.sub("", str(raw_desc or "")).strip()
    if _EMPTY_TEMPLATE_RE.match(body):
        return ""
    body = _APPDATA_PATH_RE.sub("<data-dir>/", body)
    body = _APPDATA_TAIL_RE.sub("<data-dir>/", body)
    return _PRODUCT_RE.sub("workspace", body)


def describe(server_name: str, raw_desc: str = "") -> str:
    """官方形态的 MCP 描述：`[MCP: {显示名}] {原始描述}`；无描述时只留前缀。"""
    body = _sanitize_desc_text(raw_desc)
    prefix = f"[MCP: {server_name}]"
    return f"{prefix} {body}" if body else prefix


_TOOL_REF_RE_CACHE: tuple[re.Pattern, dict[str, str]] | None = None


def _tool_ref_pattern() -> tuple[re.Pattern, dict[str, str]]:
    """拓展工具描述里引用的内部工具名 → 模型侧可见名。

    只替换带下划线的标识符（fs_read、terminal_exec……）：`git`、`outline` 这类单词
    在正文里大多是普通词，替换会改坏语义。
    """
    global _TOOL_REF_RE_CACHE
    if _TOOL_REF_RE_CACHE is None:
        from app.models.providers.ta3_tool_aliases import TO_TA3, TO_TA3_EXTENSION

        repl: dict[str, str] = {}
        for real, alias in TO_TA3.items():
            if "_" in real:
                repl[real] = alias
        for real, alias in TO_TA3_EXTENSION.items():
            if "_" in real:
                repl[real] = alias
        names = sorted(repl, key=len, reverse=True)
        pat = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_])")
        _TOOL_REF_RE_CACHE = (pat, repl)
    return _TOOL_REF_RE_CACHE


def clean_extension_text(text: str) -> str:
    """拓展工具描述清洗：内部工具名换成模型侧可见名，去产品与路径指纹。"""
    if not text:
        return text
    pat, repl = _tool_ref_pattern()
    out = pat.sub(lambda m: repl[m.group(1)], str(text))
    out = _APPDATA_PATH_RE.sub("<data-dir>/", out)
    out = _APPDATA_TAIL_RE.sub("<data-dir>/", out)
    return _PRODUCT_RE.sub("workspace", out)


def _clean_param_descriptions(value):
    """递归复制参数 schema，只清洗 description 字符串（结构、键、类型一律不动）。"""
    if isinstance(value, list):
        return [_clean_param_descriptions(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "description" and isinstance(v, str):
                out[k] = clean_extension_text(v)
            else:
                out[k] = _clean_param_descriptions(v)
        return out
    return value


# ─────────────────────────── schema 构造 ───────────────────────────


def disguise_schema(schema: dict) -> dict:
    """真实 MCP 工具 schema → ta3 上行 schema（官方命名 + 官方描述模板，参数透传）。"""
    fn = schema.get("function") or {}
    real_name = str(fn.get("name") or "")
    raw_desc = str(fn.get("description") or "")
    alias, _sid, _tool, server = mcp_parts(real_name, raw_desc)
    return {
        "type": "function",
        "function": {
            "name": alias,
            "description": describe(server, raw_desc),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        },
    }


def build_extension_tool(schema: dict) -> dict:
    """本项目独有工具 schema → 拓展 MCP 工具 schema。

    命名 `mcp__{EXTENSION_SERVER_ID}__{真实工具名}`；描述与参数说明清洗内部工具名与
    产品痕迹（plan-601），参数结构原样沿用真实 schema。
    """
    fn = schema.get("function") or {}
    real_name = str(fn.get("name") or "")
    raw_desc = str(fn.get("description") or "")
    params = fn.get("parameters") or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": ta3_mcp_name(EXTENSION_SERVER_ID, real_name),
            "description": describe(EXTENSION_SERVER_NAME, clean_extension_text(raw_desc)),
            "parameters": _clean_param_descriptions(params),
        },
    }

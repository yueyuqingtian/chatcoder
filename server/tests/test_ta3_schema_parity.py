"""ta3 伪装 schema 与真实工具的一致性守护（plan-365-1769 F1 / plan-598-2482 重构）。

**为什么需要这个文件**：`ta3_tool_schemas.py` 的快照曾与真实工具严重脱节——
`desktop_click` 在 plan-365-1757 新增了 `element` / `text` / `keys` 等参数，但 ta3 会话
下发的仍是旧快照（只有 x/y），模型只能「看图心算屏幕坐标」，直接造成点击准确率下降。

plan-598-2482 把结构改成三路分派后，契约变成两层：
1. 官方内置工具（`TO_TA3`）→ 用 `TA3_NATIVE_SCHEMAS` 快照，参数键名走 `ARGS_*` 转换；
2. 本项目独有工具（`TO_TA3_EXTENSION`）→ 从真实 schema 派生，参数**必须**逐字段一致；
3. 真实 MCP 工具（`mcp_` 前缀）→ 派生官方 MCP 形态。

第 2、3 条是「真实工具加参数 → ta3 侧自动可见」的机制保证，本文件把它固定为必过测试；
第 1 条则通过官方白名单与快照等价性守护（详见 test_ta3_official_parity.py）。
"""
from __future__ import annotations

from app.models.providers.ta3_mcp import (
    EXTENSION_SERVER_ID,
    EXTENSION_SERVER_NAME,
    clean_extension_text,
    describe,
    ta3_mcp_name,
)
from app.models.providers.ta3_official_tools import TA3_OFFICIAL_BUILTIN_TOOLS
from app.models.providers.ta3_tool_aliases import (
    ARGS_FROM_TA3,
    ARGS_TO_TA3,
    FROM_TA3,
    TO_TA3,
    TO_TA3_EXTENSION,
)
from app.models.providers.ta3_tool_schemas import TA3_NATIVE_SCHEMAS, disguise_tools


def _registry_schemas() -> list[dict]:
    """真实注册表的全量 schema（与 engine 下发时同一来源）。"""
    from app.orchestration.tools.registry import tool_registry

    return tool_registry.all_schemas()


def _disguised_by_name(schemas: list[dict], alias: str) -> dict | None:
    return next((s for s in schemas if (s.get("function") or {}).get("name") == alias), None)


def _ext(real_name: str) -> str:
    return ta3_mcp_name(EXTENSION_SERVER_ID, real_name)


# ───────────────────────── 清单卫生 ─────────────────────────


def test_extension_aliases_have_clean_hygiene():
    """拓展通道的每个目标名都必须是 `mcp__workspace-tools__{真实名}` 且无参数适配器。

    拓展工具的参数直接从真实 schema 派生，因此不允许在 `ARGS_*` 里出现同名条目——
    一旦出现，说明「原样派生」的前提被破坏，转名与转参会互相打架。
    """
    for real_name, alias in TO_TA3_EXTENSION.items():
        assert alias == _ext(real_name), f"{real_name} 的拓展名不符合官方形态: {alias}"
        assert real_name not in TO_TA3, f"{real_name} 同时出现在内置通道，通道归属必须唯一"
        assert real_name not in ARGS_TO_TA3, (
            f"{real_name} 有出站参数适配器，键名不一致时不能走「原样派生」"
        )
        assert alias not in ARGS_FROM_TA3, (
            f"{alias} 有入站参数适配器，键名不一致时不能走「原样派生」"
        )


def test_builtin_aliases_stay_in_official_whitelist():
    """内置通道的每个目标名都必须在官方内置白名单内（plan-598-2482）。"""
    for real_name, alias in TO_TA3.items():
        assert alias in TA3_OFFICIAL_BUILTIN_TOOLS, (
            f"{real_name} → {alias} 不在官方内置工具白名单内：上行工具面会与官方出现差异"
        )
        assert alias not in TO_TA3_EXTENSION.values(), f"{alias} 同时占用拓展名，通道必须唯一"


def test_every_alias_is_covered_by_one_of_two_paths():
    """每个伪装名都必须有 schema 来源——不允许「两者皆漏导致工具静默消失」。"""
    for alias in set(TO_TA3.values()):
        assert alias in TA3_NATIVE_SCHEMAS, (
            f"{alias} 不在官方快照里：ta3 会话里该工具会静默消失"
        )
    for alias in set(TO_TA3_EXTENSION.values()):
        # 拓展名由真实 schema 派生，必然可见；这里守护反查链没有断
        assert FROM_TA3.get(alias), f"{alias} 无法反查回真实工具"


# ───────────────────────── 派生一致性（核心） ─────────────────────────


def test_extension_derivation_preserves_parameters_exactly():
    """拓展工具的 schema 必须与真实 schema 结构一致：仅 name 换伪装名、描述加 MCP 前缀并清洗。

    这是「真实工具加参数 → ta3 侧自动可见」的机制保证，也是 plan-365-1757 故障的防复发核心。
    plan-601：参数只允许 description 文案被清洗（内部工具名→可见名），键与类型不变；
    输出参数经官方 canonicalize 深度键排序，字典比较与键序无关。
    """
    from app.models.providers.ta3_mcp import _clean_param_descriptions

    real_schemas = _registry_schemas()
    disguised = disguise_tools(real_schemas)
    checked = 0

    for schema in real_schemas:
        fn = schema.get("function") or {}
        real_name = fn.get("name") or ""
        if real_name not in TO_TA3_EXTENSION:
            continue
        alias = TO_TA3_EXTENSION[real_name]
        got = _disguised_by_name(disguised, alias)
        assert got is not None, f"{real_name} 已被注册且配置了映射，但伪装结果里没有 {alias}"
        got_fn = got["function"]
        assert got_fn["name"] == alias
        assert got_fn.get("description") == describe(
            EXTENSION_SERVER_NAME, clean_extension_text(fn.get("description") or ""),
        ), f"{alias} 描述未按官方 MCP 模板派生"
        expected = _clean_param_descriptions(fn.get("parameters") or {"type": "object", "properties": {}})
        assert got_fn.get("parameters") == expected, (
            f"{alias} 参数未原样沿用——真实工具与 ta3 侧又漂移了"
        )
        checked += 1

    assert checked >= 30, f"只校验了 {checked} 个拓展工具，清单或注册表可能已不匹配"


def test_native_schemas_are_frozen_official_snapshots():
    """plan-20261105-file-tools：文件工具使用执行定义，其余内置工具保持官方快照。"""
    real_schemas = _registry_schemas()
    disguised = disguise_tools(real_schemas)
    checked = 0
    for schema in real_schemas:
        real_name = (schema.get("function") or {}).get("name") or ""
        if real_name not in TO_TA3:
            continue
        alias = TO_TA3[real_name]
        got = _disguised_by_name(disguised, alias)
        assert got is not None, f"{real_name} 有内置映射，但伪装结果里没有 {alias}"
        # Bash 描述随当前 shell 追加运行环境提示（官方 appendShellEnvironmentNote），
        # 取自 native_schema 而非静态表；其余必须是静态快照对象本身
        from app.models.providers.ta3_tool_schemas import execution_schema, native_schema

        if alias in {"Read", "Search", "Diff"}:
            assert got == execution_schema(alias)
            checked += 1
            continue
        assert got is native_schema(alias), (
            f"{alias} 未使用官方快照（被派生或被改写）：内置通道必须是冻结副本"
        )
        checked += 1

    assert checked >= 10, f"只校验了 {checked} 个内置工具，注册表可能已大变"


def test_derivation_does_not_mutate_input():
    """派生必须复制而非改写调用方对象——engine 每轮复用同一批 schema。"""
    schemas = [{
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": "原始描述",
            "parameters": {"type": "object", "properties": {"element": {"type": "string"}}},
        },
    }]
    before = repr(schemas)
    out = disguise_tools(schemas)
    assert repr(schemas) == before, "disguise_tools 改写了传入的 schema 对象"
    assert out[0]["function"]["name"] == _ext("desktop_click")
    # 派生只换名字/加描述前缀，不复制也不改写参数对象（engine 每轮复用同一批 schema）
    assert out[0]["function"]["parameters"] == schemas[0]["function"]["parameters"]
    assert schemas[0]["function"]["name"] == "desktop_click", "真实 schema 的 name 被改写"


# ───────────────────────── 桌面工具关键参数（plan-365-1757 的直接回归） ─────────────────────────


def test_desktop_click_exposes_target_params_to_ta3():
    """desktop_click 的目标参数（元素编号 / 截图坐标）必须在 ta3 侧可见。

    plan-20261019-desktop-precision：坐标统一取自最近一次截图、口径按模型族写在截图结果里；
    OCR 文字定位、截图编号、点前校验相关参数已移除。参数一旦在 ta3 会话缺失，模型就只能手算坐标。
    """
    disguised = disguise_tools(_registry_schemas())
    click = _disguised_by_name(disguised, _ext("desktop_click"))
    assert click is not None, "ta3 会话下 desktop_click 不可见"
    props = click["function"]["parameters"].get("properties", {})
    for key in ("element", "x", "y", "button", "count", "title", "handle", "focus_first",
                "allow_outside", "then_shot"):
        assert key in props, f"desktop_click 缺参数 {key}"
    for gone in ("ref", "text_target", "image_px", "image_x", "expect", "destructive", "allow_blank"):
        assert gone not in props, f"desktop_click 仍暴露已移除的参数 {gone}"


def test_desktop_screenshot_exposes_new_capabilities_to_ta3():
    """desktop_screenshot 的窗口截图 / 放大查看 / 稳定等待能力必须在 ta3 侧可见。"""
    disguised = disguise_tools(_registry_schemas())
    shot = _disguised_by_name(disguised, _ext("desktop_screenshot"))
    assert shot is not None, "ta3 会话下 desktop_screenshot 不可见"
    props = shot["function"]["parameters"].get("properties", {})
    for key in ("handle", "title", "zoom", "wait_stable"):
        assert key in props, f"desktop_screenshot 缺参数 {key}"
    for gone in ("mark", "with_elements", "zoom_at", "region"):
        assert gone not in props, f"desktop_screenshot 仍暴露已移除的参数 {gone}"


def test_desktop_type_and_keys_expose_then_shot_to_ta3():
    """输入/按键的「动作后附带截图」也必须在 ta3 侧可见。"""
    disguised = disguise_tools(_registry_schemas())
    for real in ("desktop_type", "desktop_keys"):
        schema = _disguised_by_name(disguised, _ext(real))
        assert schema is not None, f"ta3 会话下 {real} 不可见"
        props = schema["function"]["parameters"].get("properties", {})
        assert "then_shot" in props, f"{real} 缺参数 then_shot"


def test_desktop_batch_input_params_visible_to_ta3():
    """一步完成多段操作的能力必须在 ta3 侧可见：点击带输入、以及 desktop_batch 本身。

    提速的主刀不是单步工具耗时，而是把多次往返合并成一次调用。
    """
    disguised = disguise_tools(_registry_schemas())
    click = _disguised_by_name(disguised, _ext("desktop_click"))
    assert click is not None
    click_props = click["function"]["parameters"].get("properties", {})
    for key in ("text", "keys", "clear_first"):
        assert key in click_props, f"desktop_click 缺批量输入参数 {key}"

    batch = _disguised_by_name(disguised, _ext("desktop_batch"))
    assert batch is not None, "ta3 会话下 desktop_batch 不可见"
    batch_params = batch["function"]["parameters"]
    assert "actions" in batch_params.get("properties", {})
    assert batch_params.get("required") == ["actions"]


def test_desktop_removed_tools_not_exposed_to_ta3():
    """OCR 文字定位与命中测试已移除：ta3 侧不应再出现对应伪装工具。"""
    disguised = disguise_tools(_registry_schemas())
    assert _disguised_by_name(disguised, _ext("desktop_find_text")) is None
    assert _disguised_by_name(disguised, _ext("desktop_hit")) is None
    # 旧的自造伪装名同样不应出现
    assert _disguised_by_name(disguised, "DesktopFindText") is None
    assert _disguised_by_name(disguised, "DesktopHit") is None


def test_desktop_click_no_longer_requires_bare_coordinates():
    """元素编号在场时，x/y 不应是必需项（否则模型被逼着给坐标）。"""
    disguised = disguise_tools(_registry_schemas())
    click = _disguised_by_name(disguised, _ext("desktop_click"))
    assert click is not None
    required = set(click["function"]["parameters"].get("required", []) or [])
    assert "element" in click["function"]["parameters"].get("properties", {})
    assert not ({"x", "y"} & required), "desktop_click 仍要求必填 x/y"

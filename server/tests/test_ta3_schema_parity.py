"""ta3 伪装 schema 与真实工具的一致性守护（plan-365-1769 F1）。

**为什么需要这个文件**：`ta3_tool_schemas.py` 的硬编码 schema 曾与真实工具严重脱节——
`desktop_click` 在 plan-365-1757 新增了 `ref` / `image_x` / `image_y` / `text_target` /
`expect` 等参数，但 ta3 会话下发的仍是旧快照（只有 x/y），模型只能「看图心算屏幕坐标」，
直接造成点击准确率大幅下降、操作反复失败。

修复方式是让自造伪装工具的 schema **从真实 schema 派生**。本文件把这条契约固定为
必过测试：一旦有人把参数写回硬编码、或新增自造工具时漏配清单，这里立刻失败——
**不允许再静默漂移**。
"""
from __future__ import annotations

from app.models.providers.ta3_tool_aliases import (
    ARGS_FROM_TA3,
    ARGS_TO_TA3,
    DYNAMIC_SCHEMA_ALIASES,
    FROM_TA3,
    TO_TA3,
)
from app.models.providers.ta3_tool_schemas import TA3_NATIVE_SCHEMAS, disguise_tools


def _registry_schemas() -> list[dict]:
    """真实注册表的全量 schema（与 engine 下发时同一来源）。"""
    from app.orchestration.tools.registry import tool_registry

    return tool_registry.all_schemas()


def _disguised_by_name(schemas: list[dict], alias: str) -> dict | None:
    return next((s for s in schemas if (s.get("function") or {}).get("name") == alias), None)


# ───────────────────────── 清单卫生 ─────────────────────────


def test_dynamic_aliases_have_clean_hygiene():
    """动态清单的每个成员都必须是「自造伪装名 + 无参数适配器 + 无硬编码副本」。"""
    for alias in DYNAMIC_SCHEMA_ALIASES:
        assert alias in set(TO_TA3.values()), f"{alias} 不在 TO_TA3 的值里（没有真实工具映射到它）"
        assert alias not in TA3_NATIVE_SCHEMAS, (
            f"{alias} 同时存在于动态清单与硬编码表——两份副本就是漂移的温床，必须只留一份"
        )
        assert alias not in ARGS_FROM_TA3, (
            f"{alias} 有入站参数适配器，键名不一致时不能走「原样派生」，请从动态清单移除"
        )
        real_name = FROM_TA3.get(alias, "")
        assert real_name not in ARGS_TO_TA3, (
            f"{real_name} 有出站参数适配器，键名不一致时不能走「原样派生」，请从动态清单移除"
        )


def test_every_alias_is_covered_by_one_of_two_paths():
    """每个伪装名要么走动态派生，要么在硬编码表里——不允许「两者皆漏导致工具静默消失」。"""
    for alias in set(TO_TA3.values()):
        assert (alias in DYNAMIC_SCHEMA_ALIASES) or (alias in TA3_NATIVE_SCHEMAS), (
            f"{alias} 既不在动态清单也不在原生表：ta3 会话里该工具会静默消失"
        )


# ───────────────────────── 派生一致性（核心） ─────────────────────────


def test_dynamic_derivation_preserves_parameters_exactly():
    """动态派生的 schema 必须与真实 schema 逐字段一致，仅 function.name 换成伪装名。

    这是「真实工具加参数 → ta3 侧自动可见」的机制保证，也是本次故障的防复发核心。
    """
    real_schemas = _registry_schemas()
    disguised = disguise_tools(real_schemas)
    checked = 0

    for schema in real_schemas:
        fn = schema.get("function") or {}
        real_name = fn.get("name") or ""
        alias = TO_TA3.get(real_name)
        if alias is None or alias not in DYNAMIC_SCHEMA_ALIASES:
            continue
        got = _disguised_by_name(disguised, alias)
        assert got is not None, f"{real_name} 已被注册且配置了映射，但伪装结果里没有 {alias}"
        got_fn = got["function"]
        assert got_fn["name"] == alias
        assert got_fn.get("description") == fn.get("description"), f"{alias} 描述未原样沿用"
        assert got_fn.get("parameters") == fn.get("parameters"), (
            f"{alias} 参数未原样沿用——真实工具与 ta3 侧又漂移了"
        )
        checked += 1

    assert checked >= 20, f"只校验了 {checked} 个动态工具，清单或注册表可能已不匹配"


def test_derivation_does_not_mutate_input():
    """派生必须复制而非改写调用方对象——engine 每轮复用同一批 schema。"""
    schemas = [{
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": "原始描述",
            "parameters": {"type": "object", "properties": {"ref": {"type": "integer"}}},
        },
    }]
    before = repr(schemas)
    out = disguise_tools(schemas)
    assert repr(schemas) == before, "disguise_tools 改写了传入的 schema 对象"
    assert out[0]["function"]["name"] == "DesktopClick"


# ───────────────────────── 桌面工具关键参数（本次故障的直接回归） ─────────────────────────


def test_desktop_click_exposes_target_params_to_ta3():
    """desktop_click 的目标参数（元素编号 / 截图坐标）必须在 ta3 侧可见。

    plan-20261019-desktop-precision：坐标统一取自最近一次截图、口径按模型族写在截图结果里；
    OCR 文字定位、截图编号、点前校验相关参数已移除。参数一旦在 ta3 会话缺失，模型就只能手算坐标。
    """
    disguised = disguise_tools(_registry_schemas())
    click = _disguised_by_name(disguised, "DesktopClick")
    assert click is not None, "ta3 会话下 DesktopClick 不可见"
    props = click["function"]["parameters"].get("properties", {})
    for key in ("element", "x", "y", "button", "count", "title", "handle", "focus_first",
                "allow_outside", "then_shot"):
        assert key in props, f"DesktopClick 缺参数 {key}"
    for gone in ("ref", "text_target", "image_px", "image_x", "expect", "destructive", "allow_blank"):
        assert gone not in props, f"DesktopClick 仍暴露已移除的参数 {gone}"


def test_desktop_screenshot_exposes_new_capabilities_to_ta3():
    """desktop_screenshot 的窗口截图 / 放大查看 / 稳定等待能力必须在 ta3 侧可见。"""
    disguised = disguise_tools(_registry_schemas())
    shot = _disguised_by_name(disguised, "DesktopScreenshot")
    assert shot is not None, "ta3 会话下 DesktopScreenshot 不可见"
    props = shot["function"]["parameters"].get("properties", {})
    for key in ("handle", "title", "zoom", "wait_stable"):
        assert key in props, f"DesktopScreenshot 缺参数 {key}"
    for gone in ("mark", "with_elements", "zoom_at", "region"):
        assert gone not in props, f"DesktopScreenshot 仍暴露已移除的参数 {gone}"


def test_desktop_type_and_keys_expose_then_shot_to_ta3():
    """输入/按键的「动作后附带截图」也必须在 ta3 侧可见。"""
    disguised = disguise_tools(_registry_schemas())
    for alias in ("DesktopType", "DesktopKeys"):
        schema = _disguised_by_name(disguised, alias)
        assert schema is not None, f"ta3 会话下 {alias} 不可见"
        props = schema["function"]["parameters"].get("properties", {})
        assert "then_shot" in props, f"{alias} 缺参数 then_shot"


def test_desktop_batch_input_params_visible_to_ta3():
    """一步完成多段操作的能力必须在 ta3 侧可见：点击带输入、以及 desktop_batch 本身。

    提速的主刀不是单步工具耗时，而是把多次往返合并成一次调用。
    """
    disguised = disguise_tools(_registry_schemas())
    click = _disguised_by_name(disguised, "DesktopClick")
    assert click is not None
    click_props = click["function"]["parameters"].get("properties", {})
    for key in ("text", "keys", "clear_first"):
        assert key in click_props, f"DesktopClick 缺批量输入参数 {key}"

    batch = _disguised_by_name(disguised, "DesktopBatch")
    assert batch is not None, "ta3 会话下 DesktopBatch 不可见"
    batch_params = batch["function"]["parameters"]
    assert "actions" in batch_params.get("properties", {})
    assert batch_params.get("required") == ["actions"]


def test_desktop_removed_tools_not_exposed_to_ta3():
    """OCR 文字定位与命中测试已移除：ta3 侧不应再出现对应伪装工具。"""
    disguised = disguise_tools(_registry_schemas())
    assert _disguised_by_name(disguised, "DesktopFindText") is None
    assert _disguised_by_name(disguised, "DesktopHit") is None


def test_desktop_click_no_longer_requires_bare_coordinates():
    """元素编号在场时，x/y 不应是必需项（否则模型被逼着给坐标）。"""
    disguised = disguise_tools(_registry_schemas())
    click = _disguised_by_name(disguised, "DesktopClick")
    assert click is not None
    required = set(click["function"]["parameters"].get("required", []) or [])
    assert "element" in click["function"]["parameters"].get("properties", {})
    assert not ({"x", "y"} & required), "DesktopClick 仍要求必填 x/y"

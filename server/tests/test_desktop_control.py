"""电脑操控（plan-334-1661）单测：门禁、审批归类、操作路线去重与管理。

覆盖三块最容易回归的地方：
1. 门禁：总开关/子开关关闭时给出可读指引（而不是静默失败），浏览器入口是并集语义；
2. 审批归类：桌面写操作单独成一类（自动审批下仍要问），只读感知走纯读；
3. 操作路线：按 (app_name, intent) 去重累加、用户编辑不被 AI 覆盖、导入导出往返。
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.orchestration.approval_policy import (
    ACTION_DESKTOP,
    ACTION_READ,
    VERDICT_ALLOW,
    VERDICT_DENY,
    VERDICT_ASK,
    classify,
    decide,
)
from app.orchestration.tools.base import ToolContext


def _ctx(tmp_path, *, permission_mode: str = "agent", approval_mode: str = "ask") -> ToolContext:
    ctx = ToolContext(
        workspace_root=str(tmp_path), session_id=1, task_id=1, agent_id=1, agent_name="t",
    )
    ctx.permission_mode = permission_mode
    ctx.approval_mode = approval_mode
    return ctx


# ══════════════════════════════════════════════════════════
# 1. 门禁
# ══════════════════════════════════════════════════════════


@pytest.fixture
def desktop_flags(monkeypatch):
    """固定电脑操控开关，避免测试间互相污染。"""
    def _set(**kwargs):
        for k, v in kwargs.items():
            monkeypatch.setattr(settings, k, v, raising=False)
    _set(
        desktop_enabled=False,
        desktop_plain_ops_enabled=True,
        desktop_browser_ops_enabled=True,
        desktop_require_foreground=True,
        desktop_recipe_enabled=True,
    )
    return _set


def test_check_enabled_guides_user_when_off(desktop_flags):
    from app.orchestration.tools.desktop import _check_enabled

    desktop_flags(desktop_enabled=False)
    msg = _check_enabled()
    assert msg and "电脑操控" in msg  # 必须指路，不能静默失败
    desktop_flags(desktop_enabled=True)
    assert _check_enabled() is None


def test_check_plain_ops_respects_sub_switch(desktop_flags):
    from app.orchestration.tools.desktop import _check_plain_ops

    desktop_flags(desktop_enabled=True, desktop_plain_ops_enabled=False)
    msg = _check_plain_ops()
    assert msg and "普通电脑操作" in msg
    desktop_flags(desktop_plain_ops_enabled=True)
    assert _check_plain_ops() is None


def test_browser_gate_is_union_of_two_entries(desktop_flags):
    """浏览器能力：常规页开关与电脑操控页开关任一开启即可用。"""
    from app.orchestration.tools.browser import _check_browser_enabled

    desktop_flags(desktop_enabled=False, desktop_browser_ops_enabled=True)
    settings.browser_enabled = False
    assert _check_browser_enabled() is not None

    # 电脑操控页开启即可（无需回常规页二次开启）
    desktop_flags(desktop_enabled=True, desktop_browser_ops_enabled=True)
    assert _check_browser_enabled() is None

    # 电脑操控页的子开关关闭时，回落到常规页开关
    desktop_flags(desktop_enabled=True, desktop_browser_ops_enabled=False)
    settings.browser_enabled = True
    assert _check_browser_enabled() is None

    desktop_flags(desktop_enabled=True, desktop_browser_ops_enabled=False)
    settings.browser_enabled = False
    assert _check_browser_enabled() is not None


# ══════════════════════════════════════════════════════════
# 2. 审批归类
# ══════════════════════════════════════════════════════════


def test_desktop_write_tools_classified_as_desktop(tmp_path):
    ctx = _ctx(tmp_path)
    for name in ("desktop_click", "desktop_type", "desktop_keys", "desktop_scroll"):
        info = classify(name, {}, ctx, risk_level="high")
        assert info.action == ACTION_DESKTOP, name
        assert info.label  # 审批卡需要有可读标题


def test_desktop_read_tools_classified_as_read(tmp_path):
    ctx = _ctx(tmp_path)
    for name in ("desktop_windows", "desktop_snapshot", "desktop_hit", "desktop_screenshot",
                 "desktop_focus", "desktop_find_text"):
        info = classify(name, {}, ctx, risk_level="low")
        assert info.action == ACTION_READ, name


def test_desktop_write_asks_even_in_auto_mode(tmp_path):
    """自动审批下，操作真实鼠标键盘仍要问——点错窗口的代价高于一次打扰。"""
    ctx = _ctx(tmp_path, approval_mode="auto")
    d = decide("desktop_click", {"x": 1, "y": 2}, ctx, risk_level="high")
    assert d.verdict == VERDICT_ASK


def test_desktop_write_allowed_in_full_mode(tmp_path):
    ctx = _ctx(tmp_path, approval_mode="full")
    d = decide("desktop_type", {"text": "hi"}, ctx, risk_level="high")
    assert d.verdict == VERDICT_ALLOW


def test_readonly_mode_denies_desktop_write(tmp_path):
    ctx = _ctx(tmp_path, permission_mode="readonly")
    d = decide("desktop_click", {"x": 1, "y": 2}, ctx, risk_level="high")
    assert d.verdict == VERDICT_DENY
    assert "只读模式" in d.reason


def test_plan_mode_denies_desktop_write(tmp_path):
    ctx = _ctx(tmp_path, permission_mode="plan")
    d = decide("desktop_keys", {"keys": "ENTER"}, ctx, risk_level="high")
    assert d.verdict == VERDICT_DENY
    assert "计划模式" in d.reason


def test_readonly_mode_allows_desktop_sensing(tmp_path):
    """只读感知不构成副作用，只读模式下也应放行。"""
    ctx = _ctx(tmp_path, permission_mode="readonly")
    d = decide("desktop_snapshot", {"title": "x"}, ctx, risk_level="low")
    assert d.verdict == VERDICT_ALLOW


# ══════════════════════════════════════════════════════════
# 3. 操作路线
# ══════════════════════════════════════════════════════════


@pytest.fixture
async def db_session():
    from app.persistence.database import async_session_factory

    async with async_session_factory() as s:
        yield s


@pytest.mark.asyncio
async def test_recipe_dedup_accumulates_usage(db_session):
    from app.services import desktop_recipe_service as svc

    row1, created1 = await svc.save_recipe(
        db_session, app_name="QQMusic", intent="播放歌曲", principle="用顶部搜索框",
    )
    assert created1 is True

    # 大小写与首尾空白不同也视为同一条，不产生重复
    row2, created2 = await svc.save_recipe(
        db_session, app_name=" qqmusic ", intent="播放歌曲", principle="用顶部搜索框，输入后回车",
    )
    assert created2 is False
    assert row2.id == row1.id
    assert row2.usage_count == 2
    assert "回车" in row2.principle  # 知识被较新观察刷新


@pytest.mark.asyncio
async def test_recipe_recall_matches_and_counts(db_session):
    from app.services import desktop_recipe_service as svc

    await svc.save_recipe(
        db_session, app_name="QQMusic", intent="播放歌曲", principle="用顶部搜索框",
    )
    hits = await svc.recall_recipe(db_session, app_name="QQMusic", intent="播放")
    assert len(hits) == 1
    assert hits[0].usage_count == 2  # 保存 1 次 + 命中 1 次

    # 完全无关的应用不应命中
    assert await svc.recall_recipe(db_session, app_name="notepad", intent="打开文件") == []


@pytest.mark.asyncio
async def test_recipe_user_edit_not_downgraded_by_agent(db_session):
    """用户编辑过的路线保持 user 来源，不被后续 AI 沉淀降级。"""
    from app.services import desktop_recipe_service as svc

    row, _ = await svc.save_recipe(
        db_session, app_name="notepad", intent="新建文档", principle="Ctrl+N",
    )
    await svc.update_recipe(db_session, row.id, principle="Ctrl+N 或 文件菜单 → 新建")
    assert row.source == "user"

    same, created = await svc.save_recipe(
        db_session, app_name="notepad", intent="新建文档", principle="Ctrl+N", source="agent",
    )
    assert created is False
    assert same.source == "user"


@pytest.mark.asyncio
async def test_recipe_import_export_roundtrip(db_session):
    from app.services import desktop_recipe_service as svc

    await svc.save_recipe(db_session, app_name="A", intent="i1", principle="p1")
    await svc.save_recipe(db_session, app_name="B", intent="i2", principle="p2", pitfalls="坑")

    payload = await svc.export_recipes(db_session)
    assert payload["count"] == 2
    # 导出内容是可移植的：不含本地 id
    assert all("id" not in it for it in payload["items"])

    # 导入到同一库应全部按去重合并（更新而非新增），脏数据只跳过
    result = await svc.import_recipes(db_session, {"items": payload["items"] + [{"app_name": ""}]})
    assert result["created"] == 0
    assert result["updated"] == 2
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_recipe_delete_batch_and_all(db_session):
    from app.services import desktop_recipe_service as svc

    r1, _ = await svc.save_recipe(db_session, app_name="A", intent="i1", principle="p1")
    await svc.save_recipe(db_session, app_name="B", intent="i2", principle="p2")

    assert await svc.delete_recipes(db_session, [r1.id]) == 1
    assert await svc.delete_all(db_session) == 1
    assert await svc.list_recipes(db_session) == []


@pytest.mark.asyncio
async def test_recipe_requires_principle(db_session):
    """principle 是本功能的核心价值，不能为空。"""
    from app.services import desktop_recipe_service as svc

    with pytest.raises(ValueError):
        await svc.save_recipe(db_session, app_name="A", intent="i", principle="   ")


# ══════════════════════════════════════════════════════════
# 4. 感知提速与观测（plan-340-1705）
# ══════════════════════════════════════════════════════════


def test_obs_scene_labels_cover_three_cases():
    """观测场景标签分三类（失败/写操作/读取操作），用于「场景 × 思考量」分布统计。"""
    from app.orchestration.agent_loop import _desktop_obs_scene

    write = frozenset({"desktop_click", "desktop_type"})
    assert _desktop_obs_scene([("desktop_click", False)], write) == "after_error"
    assert _desktop_obs_scene([("desktop_click", True)], write) == "after_action"
    assert _desktop_obs_scene([("desktop_snapshot", True)], write) == "after_perceive"
    # 同一轮多个调用取最后一个：它最接近当前决策的上下文
    assert _desktop_obs_scene(
        [("desktop_snapshot", True), ("desktop_click", False)], write
    ) == "after_error"


def test_chromium_window_class_routing():
    """分流判据：Chromium 系窗口类名前缀命中才尝试 CDP，其余一律走 UIA。"""
    from app.core.desktop_cdp import extract_debug_port, is_chromium_window

    assert is_chromium_window("Chrome_WidgetWin_1") is True
    assert is_chromium_window("CefWebViewWnd") is True
    assert is_chromium_window("Notepad") is False
    assert is_chromium_window("") is False
    assert is_chromium_window(None) is False
    # Chromium 同时接受 = 与空格两种写法，都要认
    assert extract_debug_port("chrome.exe --remote-debugging-port=9222") == 9222
    assert extract_debug_port("chrome.exe --remote-debugging-port 9333") == 9333
    assert extract_debug_port("chrome.exe --no-flag") is None


def test_cdp_coord_conversion_uses_window_scale():
    """CDP 视口 CSS 坐标 → 屏幕物理像素：按「窗口物理尺寸 / 窗口逻辑尺寸」换算。"""
    from app.core.desktop_cdp import to_screen_coords

    # 窗口物理 2400x1600，逻辑 1200x800 → 缩放 2x；顶部浏览器 UI 占 100 逻辑像素
    win = {"outerWidth": 1200, "outerHeight": 800,
           "innerWidth": 1200, "innerHeight": 700}
    rect = [100, 50, 2400, 1600]
    x, y = to_screen_coords({"cx": 600.0, "cy": 350.0}, win, rect)
    assert (x, y) == (100 + 1200, 50 + (100 + 350) * 2)


def test_desktop_tool_schemas_are_byte_stable():
    """稳定前缀：工具 schema 必须逐字节稳定，否则 provider 侧 prompt cache 无法命中。"""
    import json

    from app.orchestration.tools.desktop import (
        DesktopClickTool,
        DesktopHitTool,
        DesktopScreenshotTool,
        DesktopSnapshotTool,
    )

    for tool_cls in (DesktopSnapshotTool, DesktopHitTool,
                     DesktopScreenshotTool, DesktopClickTool):
        first = json.dumps(tool_cls().function_schema(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(tool_cls().function_schema(), ensure_ascii=False, sort_keys=True)
        assert first == second


def test_desktop_prompt_sits_before_dynamic_context():
    """稳定前缀：静态的电脑操控规范必须排在动态 extra_context 之前，不打断缓存前缀。"""
    from app.orchestration.prompts.main import build_main_system_prompt

    dynamic = "DYNAMIC_MARKER_SHOULD_BE_LAST"
    prompt = build_main_system_prompt(
        extra_context=dynamic, desktop_enabled=True, language="zh",
    )
    assert "Computer-Control Tasks" in prompt
    assert prompt.index("Computer-Control Tasks") < prompt.index(dynamic)


def test_desktop_prompt_absent_when_disabled():
    """未开启电脑操控时不注入该规范：零 token 成本，也不影响普通任务。"""
    from app.orchestration.prompts.main import build_main_system_prompt

    prompt = build_main_system_prompt(extra_context="", desktop_enabled=False, language="zh")
    assert "Computer-Control Tasks" not in prompt


def test_first_step_extraction_for_recipe_recall():
    """路线「第一步」提取：命中路线时直接给出起点，省掉「先观察再决定」的一轮。"""
    from app.orchestration.tools.desktop import _first_step_of

    class _Recipe:
        def __init__(self, steps):
            self.steps = steps

    assert _first_step_of(_Recipe([{"action": "先点顶部搜索框"}, {"action": "输入关键词"}])) \
        == "先点顶部搜索框"
    assert _first_step_of(_Recipe(["打开设置"])) == "打开设置"
    assert _first_step_of(_Recipe(None)) == ""
    assert _first_step_of(_Recipe([])) == ""
    assert _first_step_of(_Recipe([{"note": "没有可执行动作"}])) == ""



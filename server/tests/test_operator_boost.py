"""plan-20261018-operator-boost：电脑操控 / 内置浏览器加强的回归测试。

覆盖：默认值一次性升级、路线类型去重与按目标召回、自动召回只附一次、收尾沉淀守卫、
操控子会话思考深度解析、截图缩放长边跟随设置。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import settings


# ── 默认值升级 ──────────────────────────────────────────────


def test_upgrade_desktop_defaults_only_touches_old_defaults(monkeypatch):
    from app.gateway.routers import settings as router

    saved: list[dict] = []
    monkeypatch.setattr(router, "_save_config", lambda d: saved.append(dict(d)))
    data = {
        "desktop_enabled": False,            # 旧默认 → 升级为开启
        "desktop_screenshot_quality": 60,    # 用户改过 → 保持
        "desktop_screenshot_max_dim": 1600,  # 旧默认 → 不缩放
    }
    router._upgrade_desktop_defaults(data)
    assert data["desktop_enabled"] is True
    assert data["desktop_screenshot_quality"] == 60
    assert data["desktop_screenshot_max_dim"] == 0
    assert data["desktop_defaults_rev"] == router._DESKTOP_DEFAULTS_REV
    assert saved, "升级后必须落盘，避免每次启动重复判断"

    # 幂等：已升级过的配置里用户再关掉电脑操控，不会被再次打开
    data["desktop_enabled"] = False
    router._upgrade_desktop_defaults(data)
    assert data["desktop_enabled"] is False


def test_new_install_defaults():
    from app.core.config import Settings

    s = Settings()
    assert s.desktop_enabled is True
    assert s.desktop_plain_ops_enabled and s.desktop_require_foreground
    assert s.desktop_screenshot_max_dim == 0
    assert s.desktop_screenshot_quality == 100
    assert s.desktop_recipe_enabled is True
    # plan-20261019-desktop-precision：编号 / 点前校验 / 破坏性词表已移除；坐标口径默认按模型自动选择
    assert s.desktop_coord_mode == "auto"
    assert not hasattr(s, "desktop_mark_shot") and not hasattr(s, "desktop_verify_click")


def test_screenshot_max_dim_follows_setting(monkeypatch):
    from app.orchestration.tools import desktop

    monkeypatch.setattr(settings, "desktop_screenshot_max_dim", 1280, raising=False)
    assert desktop._setting_max_dim() == 1280
    monkeypatch.setattr(settings, "desktop_screenshot_max_dim", 0, raising=False)
    assert desktop._setting_max_dim() == 0


# ── 路线：类型与按目标召回 ───────────────────────────────────


@pytest.fixture
async def db_session():
    from app.persistence.database import async_session_factory

    async with async_session_factory() as s:
        yield s


@pytest.mark.asyncio
async def test_recipe_scope_dedup_and_target_match(db_session):
    from app.services import desktop_recipe_service as svc

    d, _ = await svc.save_recipe(db_session, app_name="WPS", intent="打开表格", principle="双击文件")
    w, created = await svc.save_recipe(
        db_session, app_name="localhost:5173", intent="打开表格", principle="点导出", scope="browser",
    )
    assert created is True and w.id != d.id  # 同名意图、不同类型不算重复
    assert svc.to_dict(w)["scope"] == "browser"

    # 网页：同 host、子域名、未写端口的路线都能命中；自动附带不计数
    hits = await svc.match_for_target(db_session, scope="browser", names=["localhost:5173"])
    assert [h.id for h in hits] == [w.id]
    assert hits[0].usage_count == 1
    await svc.save_recipe(db_session, app_name="example.com", intent="登录", principle="先点右上角", scope="browser")
    assert await svc.match_for_target(db_session, scope="browser", names=["erp.example.com:8443"])
    assert await svc.match_for_target(db_session, scope="browser", names=["other.com"]) == []

    # 桌面：进程名或窗口标题包含应用名
    hits = await svc.match_for_target(db_session, scope="desktop", names=["", "报表.xlsx - WPS"])
    assert [h.id for h in hits] == [d.id]

    # recall 按类型过滤
    assert [h.id for h in await svc.recall_recipe(db_session, intent="打开表格", scope="browser")] == [w.id]


@pytest.mark.asyncio
async def test_route_hint_attached_once_per_session(db_session, monkeypatch):
    from app.orchestration import route_hints
    from app.services import desktop_recipe_service as svc

    monkeypatch.setattr(settings, "desktop_recipe_enabled", True, raising=False)
    route_hints._SHOWN.clear()
    await svc.save_recipe(db_session, app_name="localhost:5173", intent="导出订单", principle="筛选后点导出",
                          pitfalls="导出前先等列表加载完", scope="browser")
    ctx = SimpleNamespace(db=db_session, session_id=99)
    first = await route_hints.route_hint(ctx, scope="browser", names=["localhost:5173"])
    assert "筛选后点导出" in first and "导出前先等列表加载完" in first
    assert await route_hints.route_hint(ctx, scope="browser", names=["localhost:5173"]) == ""

    monkeypatch.setattr(settings, "desktop_recipe_enabled", False, raising=False)
    route_hints._SHOWN.clear()
    assert await route_hints.route_hint(ctx, scope="browser", names=["localhost:5173"]) == ""


def test_host_of():
    from app.orchestration.route_hints import host_of

    assert host_of("http://localhost:5173/a?b=1") == "localhost:5173"
    assert host_of("erp.example.com/x") == "erp.example.com"
    assert host_of("") == ""


# ── 收尾沉淀守卫 ────────────────────────────────────────────


def test_guard_reminds_once_after_unsaved_operation():
    from app.orchestration.op_route_guard import OpRouteGuard

    g = OpRouteGuard()
    g.observe("browser_navigate", {"url": "http://localhost:5173/orders"}, True, {"url": "http://localhost:5173/orders"})
    g.observe("browser_click", {"ref": "ref_3"}, True)
    g.observe("browser_batch", {"actions": [{"action": "click"}, {"action": "type"}]}, True)
    note = g.reminder()
    assert note and "browser_recipe" in note and "localhost:5173" in note
    assert g.reminder() is None  # 每轮最多一次


def test_guard_silent_when_saved_or_reused_or_trivial():
    from app.orchestration.op_route_guard import OpRouteGuard

    saved = OpRouteGuard()
    for _ in range(4):
        saved.observe("desktop_click", {"title": "WPS"}, True)
    saved.observe("desktop_recipe", {"action": "save"}, True)
    assert saved.reminder() is None

    reused = OpRouteGuard()
    reused.observe("browser_navigate", {"url": "http://a.test"}, True, {"url": "http://a.test", "route_hint": True})
    for _ in range(4):
        reused.observe("browser_click", {}, True)
    assert reused.reminder() is None  # 命中路线且全程无失败：路线够用

    reused.observe("browser_click", {}, False)
    assert reused.reminder() is not None  # 复用中出现失败：值得更新路线

    trivial = OpRouteGuard()
    trivial.observe("browser_click", {}, True)
    assert trivial.reminder() is None

    off = OpRouteGuard(enabled=False)
    for _ in range(5):
        off.observe("browser_click", {}, True)
    assert off.reminder() is None


# ── 操控子会话 ──────────────────────────────────────────────


class _FakeDb:
    def __init__(self, levels):
        self._levels = levels

    async def get(self, _model, _id):
        return SimpleNamespace(reasoning_efforts=self._levels)


@pytest.mark.asyncio
async def test_resolve_operator_effort(monkeypatch):
    from app.services.subsession_service import is_operator_purpose, resolve_operator_effort

    monkeypatch.setattr(settings, "operator_reasoning_effort", "lowest", raising=False)
    assert await resolve_operator_effort(_FakeDb(["none", "minimal", "low", "high"]), 1) == "minimal"
    assert await resolve_operator_effort(_FakeDb(["medium", "high"]), 1) == "medium"
    assert await resolve_operator_effort(_FakeDb([]), 1) == "low"
    assert await resolve_operator_effort(_FakeDb(["low"]), None) == "low"

    monkeypatch.setattr(settings, "operator_reasoning_effort", "follow", raising=False)
    assert await resolve_operator_effort(_FakeDb(["low"]), 1) is None
    monkeypatch.setattr(settings, "operator_reasoning_effort", "none", raising=False)
    assert await resolve_operator_effort(_FakeDb(["low"]), 1) == "none"

    monkeypatch.setattr(settings, "operator_subsession_enabled", True, raising=False)
    assert is_operator_purpose("operator") and not is_operator_purpose("")
    monkeypatch.setattr(settings, "operator_subsession_enabled", False, raising=False)
    assert not is_operator_purpose("operator")


def test_operator_hint_gating(monkeypatch):
    from app.orchestration.prompts.parallel_subsession import operator_subsession_hint

    monkeypatch.setattr(settings, "operator_subsession_enabled", True, raising=False)
    assert "purpose=operator" in operator_subsession_hint("agent", gui_tools=True)
    assert operator_subsession_hint("readonly", gui_tools=True) == ""
    assert operator_subsession_hint("agent", gui_tools=False) == ""
    assert operator_subsession_hint("agent", gui_tools=True, is_subsession=True) == ""
    monkeypatch.setattr(settings, "operator_subsession_enabled", False, raising=False)
    assert operator_subsession_hint("agent", gui_tools=True) == ""


def test_create_subsession_schema_exposes_purpose():
    from app.orchestration.subsession_tools import CREATE_SUBSESSION_SCHEMA

    props = CREATE_SUBSESSION_SCHEMA["function"]["parameters"]["properties"]
    assert props["purpose"]["enum"] == ["", "operator"]


# ── 下载回报文案 ────────────────────────────────────────────


def test_download_event_lines():
    from app.orchestration.browser.driver import _download_line

    done = _download_line({"type": "download", "state": "completed", "path": "D:/dl/a.xlsx", "bytes": 2048})
    assert "D:/dl/a.xlsx" in done and "2.0 KB" in done and "terminal_exec" in done
    assert "browser_downloads" in _download_line({"state": "started", "filename": "a.xlsx", "path": "D:/dl/a.xlsx"})
    assert "中断" in _download_line({"state": "interrupted", "filename": "a.xlsx"})


# ── 电脑操控：截图尺寸适配模型 / 点击不出目标窗口 ─────────────


def test_model_safe_max_dim():
    from app.core.desktop_coords import model_safe_max_dim

    # 整屏 16:9：缩到短边 768（模型实际看到的尺寸），长边 1365
    assert model_safe_max_dim(2560, 1440, 0) == 1365
    assert model_safe_max_dim(1920, 1080, 0) == 1365
    # 小区域本身就在可见尺寸内：不缩放
    assert model_safe_max_dim(900, 600, 0) == 0
    # 显式设置更小时用设置值；更大时不超过可见尺寸
    assert model_safe_max_dim(2560, 1440, 1000) == 1000
    assert model_safe_max_dim(2560, 1440, 2560) == 1365
    # 超宽屏受长边 1568 约束
    assert model_safe_max_dim(5120, 1440, 0) == 1568


@pytest.mark.asyncio
async def test_click_outside_target_window_refused(monkeypatch, tmp_path):
    from app.core import desktop_shots
    from app.orchestration.tools import desktop as D
    from app.orchestration.tools.base import ToolContext

    clicked: list[dict] = []

    async def fake_call(op, params=None):
        if op == "windows":
            return {"items": [{"h": 7, "title": "记事本", "proc": "notepad", "rect": [500, 300, 800, 600]}]}
        if op == "frame":
            return {"frame": [500, 300, 800, 600]}
        if op == "click":
            clicked.append(dict(params or {}))
            return {"clicked": True, "at": [params["x"], params["y"]], "target": "记事本"}
        return {}

    monkeypatch.setattr(D, "_call", fake_call)
    monkeypatch.setattr(settings, "desktop_enabled", True, raising=False)
    monkeypatch.setattr(settings, "desktop_plain_ops_enabled", True, raising=False)
    ctx = ToolContext(workspace_root=str(tmp_path), session_id=1, task_id=1, agent_id=1, agent_name="t",
                      model_name="claude-opus-5")
    desktop_shots.clear(ctx)
    # 整屏截图 2560x1600 → 1280x800（像素口径：图上坐标 ×2 即屏幕坐标）
    desktop_shots.store_frame(ctx, path="a.jpg", mode="pixel", meta={
        "srcX": 0, "srcY": 0, "srcW": 2560, "srcH": 1600, "width": 1280, "height": 800})

    res = await D.DesktopClickTool().run({"x": 60, "y": 45, "title": "记事本", "then_shot": False}, ctx)
    assert not res.ok and "不在目标窗口" in res.error and not clicked

    ok = await D.DesktopClickTool().run({"x": 350, "y": 250, "title": "记事本", "then_shot": False}, ctx)
    assert ok.ok and clicked and clicked[-1]["x"] == 700

    out = await D.DesktopClickTool().run(
        {"x": 60, "y": 45, "title": "记事本", "allow_outside": True, "then_shot": False}, ctx)
    assert out.ok
    desktop_shots.clear(ctx)


# ── 电脑操控：新窗口跟随 / 前台提示 / 黑画面提示 / 稀疏控件树（腾讯视频实录） ──


_HOME = {"h": 100, "title": "腾讯视频", "proc": "QQLive", "pid": 11, "x": 1920, "y": 1044}
_PLAYER = {"h": 200, "title": "仙逆 第1集", "proc": "QQLive", "pid": 11, "x": 1500, "y": 900}
_CHAT = {"h": 300, "title": "chatcoder", "proc": "chatcoder", "pid": 22, "x": 1920, "y": 1044}


@pytest.mark.asyncio
async def test_window_change_note_reports_new_player_window(monkeypatch):
    from app.orchestration.tools import desktop as D

    async def fake_call(op, params=None):
        assert op == "windows"
        return {"items": [dict(_HOME), dict(_PLAYER, fg=True), dict(_CHAT)]}

    monkeypatch.setattr(D, "_call", fake_call)
    # plan-537-2251：窗口快照改为按 (session_id, task_id) 隔离，测试需用同一 ctx 键写入
    ctx = SimpleNamespace(session_id=1, task_id=1)
    D._LAST_WINDOWS[D._state_key(ctx)] = {"at": D.time.monotonic(), "items": [dict(_HOME, fg=True), dict(_CHAT)]}
    note, follow = await D._window_change_note(ctx, dict(_HOME))
    assert "新窗口" in note and "handle=200" in note and "不要再 desktop_focus" in note
    assert follow and follow["h"] == 200


@pytest.mark.asyncio
async def test_not_foreground_hint_names_same_app_window(monkeypatch):
    from app.orchestration.tools import desktop as D

    async def fake_call(op, params=None):
        return {"items": [dict(_HOME), dict(_PLAYER, fg=True)]}

    monkeypatch.setattr(D, "_call", fake_call)
    ctx = SimpleNamespace(session_id=1, task_id=1)
    msg = await D._not_foreground_hint(ctx, RuntimeError("目标窗口不在前台（腾讯视频）。"), 100, "")
    assert "同一应用的另一个窗口" in msg and "handle=200" in msg
    assert await D._not_foreground_hint(ctx, RuntimeError("别的错误"), 100, "") == "别的错误"


def test_shot_notes_dark_and_covered(tmp_path, monkeypatch):
    from PIL import Image

    from app.orchestration.tools import desktop as D

    black = tmp_path / "black.jpg"
    Image.new("RGB", (640, 400), (0, 0, 0)).save(black)
    bright = tmp_path / "bright.jpg"
    Image.new("RGB", (640, 400), (230, 230, 230)).save(bright)
    assert D._dark_ratio(str(black)) > 0.9 and D._dark_ratio(str(bright)) == 0
    ctx = SimpleNamespace(session_id=1, task_id=1)
    assert any("接近纯黑" in n for n in D._shot_notes(ctx, str(black), None, False))

    D._LAST_WINDOWS[D._state_key(ctx)] = {"at": D.time.monotonic(), "items": [dict(_HOME), dict(_PLAYER, fg=True)]}
    notes = D._shot_notes(ctx, str(bright), dict(_HOME), True)
    assert len(notes) == 1 and "handle=200" in notes[0]
    assert D._shot_notes(ctx, str(bright), dict(_HOME, fg=True), True) == []


def test_uia_sparse():
    from app.orchestration.tools.desktop import _uia_sparse

    top_bar = [{"x": 1600 + i * 50, "y": 331} for i in range(13)]
    assert _uia_sparse(top_bar, [864, 285, 2112, 1518])
    spread = [{"x": 900, "y": 300 + i * 100} for i in range(13)]
    assert not _uia_sparse(spread, [864, 285, 2112, 1518])
    assert not _uia_sparse([{"x": 1, "y": 1}] * 40, [0, 0, 100, 100])

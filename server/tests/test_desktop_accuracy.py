"""电脑操控「点准」单测（plan-20261019-desktop-precision；取代 plan-365-1757 的编号 / 点前校验用例）。

覆盖三块一旦回归就会直接表现为「点错位置」或「操作变慢」的确定性代码：

1. **坐标口径与换算**：按模型族选口径（像素 / 0–1000）、截图坐标 → 屏幕物理像素（窗口原点、
   多显示器负坐标、比例小数容错、越界报错）、截图尺寸上限。
2. **坐标系与元素编号表**：唯一坐标系（放大查看不改变它）、元素编号查表与过期重定位、任务间隔离。
3. **工具行为**（内核用假实现）：点击 / 输入 / 批量动作透传给内核的参数、动作后默认带回新截图、
   「画面没有可见变化」提示、批量动作执行前整体校验；以及本轮截图裁剪与工具等待不再轮询。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.core import desktop_coords, desktop_shots
from app.core.desktop_coords import NORM1000, PIXEL
from app.orchestration.tools.base import ToolContext


@pytest.fixture(autouse=True)
def _clean_state():
    """坐标系与编号表是模块级状态，测试间必须隔离。"""
    desktop_shots.clear()
    yield
    desktop_shots.clear()


def _meta(**kw) -> dict:
    """标准截图元数据：2560x1600 屏幕缩到 1228x768（本机整屏截图的实际尺寸）。"""
    base = {"path": "shot.jpg", "srcX": 0, "srcY": 0, "srcW": 2560, "srcH": 1600,
            "width": 1228, "height": 768, "scale": 0.4797}
    base.update(kw)
    return base


def _ctx(tmp_path=None, *, session_id: int = 1, task_id: int = 1, model: str = "deepseek-flash") -> ToolContext:
    return ToolContext(
        workspace_root=str(tmp_path or "."), session_id=session_id, task_id=task_id,
        agent_id=1, agent_name="t", model_name=model,
    )


# ══════════════════════════════════════════════════════════
# 1. 坐标口径与换算
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("name,mode", [
    ("claude-opus-5-5", PIXEL),
    ("ag/claude-sonnet-4-6", PIXEL),
    ("gpt-5.6-luna", PIXEL),
    ("gpt-6-astra", PIXEL),
    ("openai/o3", PIXEL),
    ("Qwen2.5-VL-72B-Instruct", PIXEL),
    # DeepSeek 与未知模型：没有原生 0–1000 口径，一律图上像素（实录：deepseek-flash 在像素口径下点得准，
    # 在 0–1000 口径下 x 轴换算了、y 轴没换，20 多次点击全部落空）
    ("deepseek-flash", PIXEL),
    ("deepseek/deepseek-v4.1-flash", PIXEL),
    ("MiniMaxAI/MiniMax-M2.7", PIXEL),
    ("space-bunny", PIXEL),
    ("", PIXEL),
    # 训练时就用 0–1000 归一化坐标的模型族
    ("glm-5.3-flash", NORM1000),
    ("zai-org/GLM-5.1", NORM1000),
    ("Qwen/Qwen3.8-Flash", NORM1000),
    ("kimi-k3", NORM1000),
    ("moonshotai/Kimi-K2.6", NORM1000),
    ("gemini-3.8-flash-high", NORM1000),
    ("doubao-seed-1.6-vision", NORM1000),
    ("UI-TARS-1.5", NORM1000),
])
def test_coord_mode_follows_model_family(name, mode):
    """Claude / GPT / Qwen2.5-VL / DeepSeek 与未知模型报图上像素；Gemini、Qwen3、GLM、Kimi、豆包等原生 0–1000 的走比例。"""
    assert desktop_coords.coord_mode_for(name) == mode


def test_coord_mode_override_wins():
    assert desktop_coords.coord_mode_for("deepseek-flash", "norm1000") == NORM1000
    assert desktop_coords.coord_mode_for("gemini-3-pro", "pixel") == PIXEL
    assert desktop_coords.coord_mode_for("claude-opus-5", "norm1000") == NORM1000
    # 无法识别的覆盖值按 auto 处理
    assert desktop_coords.coord_mode_for("claude-opus-5", "bogus") == PIXEL
    assert desktop_coords.coord_mode_for("gemini-3-pro", "bogus") == NORM1000


def test_coord_mode_name_with_both_families_prefers_pixel():
    """名字里同时带像素族与比例族的词（经网关改名的 claude / gpt 之类）以像素为准。"""
    assert desktop_coords.coord_mode_for("kimi-via-claude-proxy") == PIXEL
    assert desktop_coords.coord_mode_for("gpt-4o-glm-gateway") == PIXEL


def test_convention_line_states_anchor_and_both_axes():
    """口径行写明图中心数值作锚点；比例口径强调两个轴都要换算（实录里模型只换了 x 轴）。"""
    px = desktop_coords.convention_line(PIXEL, 1286, 768)
    assert "图上像素" in px and "(643,384)" in px and "不要换算成比例" in px
    nm = desktop_coords.convention_line(NORM1000, 1286, 768)
    assert "0–1000" in nm and "都" in nm and "500,500" in nm


def test_to_image_px_echoes_normalized_point():
    """比例口径回显它落在图上的哪个像素：(770, 336) → 图上 (990, 258)，模型当场可对出 y 轴没换算。"""
    m = {"srcX": 0, "srcY": 0, "srcW": 2560, "srcH": 1529, "width": 1286, "height": 768}
    sx, sy, _ = desktop_coords.to_screen(m, 770, 336, NORM1000)
    assert desktop_coords.to_image_px(m, sx, sy) == (990, 258)


def test_to_screen_pixel_full_screen():
    m = _meta()
    sx, sy, note = desktop_coords.to_screen(m, 614, 384, PIXEL)
    assert (sx, sy) == (1280, 800) and note == ""
    assert desktop_coords.to_screen(m, 0, 0, PIXEL)[:2] == (0, 0)
    # 右下角夹到最后一个物理像素，不会越出截图区域
    assert desktop_coords.to_screen(m, 1228, 768, PIXEL)[:2] == (2559, 1599)


def test_to_screen_norm1000_ignores_image_pixels():
    """0–1000 口径与图片像素尺寸无关：中心永远是 500,500。"""
    m = _meta()
    assert desktop_coords.to_screen(m, 500, 500, NORM1000)[:2] == (1280, 800)
    assert desktop_coords.to_screen(_meta(width=900, height=563), 500, 500, NORM1000)[:2] == (1280, 800)


def test_to_screen_respects_window_origin():
    """窗口截图原点非零：换算必须加上原点（plan-365-1773 实录中模型漏掉的正是它）。"""
    m = _meta(srcX=504, srcY=153, srcW=1551, srcH=1212, width=982, height=767)
    sx, sy, _ = desktop_coords.to_screen(m, 240, 450, PIXEL)
    assert (sx, sy) == (504 + round(240 / 982 * 1551), 153 + round(450 / 767 * 1212))
    assert desktop_coords.to_screen(m, 0, 0, NORM1000)[:2] == (504, 153)


def test_to_screen_multimonitor_negative_origin():
    m = _meta(srcX=-1920, srcY=0, srcW=1920, srcH=1080, width=1365, height=768)
    assert desktop_coords.to_screen(m, 500, 500, NORM1000)[:2] == (-960, 540)
    assert desktop_coords.to_screen(m, 0, 0, PIXEL)[:2] == (-1920, 0)


def test_to_screen_fraction_tolerated_in_any_mode():
    """0~1 的小数（Kimi 等的习惯）按比例理解，并在结果里说明。"""
    m = _meta()
    for mode in (PIXEL, NORM1000):
        sx, sy, note = desktop_coords.to_screen(m, 0.5, 0.25, mode)
        assert (sx, sy) == (1280, 400)
        assert "比例" in note
    # 整数 0/1 不是小数：按口径原样理解
    assert desktop_coords.to_screen(m, 1, 1, PIXEL)[2] == ""


def test_to_screen_norm1000_falls_back_to_pixels_when_obviously_pixels():
    """0–1000 口径下给了 >1000 且落在图片内的值：模型显然报的是像素，按像素换算并提示。"""
    m = _meta()
    sx, sy, note = desktop_coords.to_screen(m, 1148, 198, NORM1000)
    assert (sx, sy) == (round(1148 / 1228 * 2560), round(198 / 768 * 1600))
    assert "像素" in note


def test_to_screen_out_of_range_raises_with_convention():
    with pytest.raises(desktop_coords.CoordError, match="0–1000"):
        desktop_coords.to_screen(_meta(), 1500, 900, NORM1000)
    with pytest.raises(desktop_coords.CoordError, match="1228"):
        desktop_coords.to_screen(_meta(), 1300, 100, PIXEL)
    with pytest.raises(desktop_coords.CoordError):
        desktop_coords.to_screen(_meta(), "abc", 1, PIXEL)
    with pytest.raises(desktop_coords.CoordError):
        desktop_coords.to_screen(_meta(width=0), 1, 1, PIXEL)


def test_from_screen_roundtrip():
    m = _meta(srcX=100, srcY=50)
    for mode in (PIXEL, NORM1000):
        x, y = desktop_coords.from_screen(m, 1380, 850, mode)
        sx, sy, _ = desktop_coords.to_screen(m, x, y, mode)
        assert abs(sx - 1380) <= 3 and abs(sy - 850) <= 3


def test_parse_region_orders_corners():
    m = _meta()
    assert desktop_coords.parse_region(m, "600,600,400,400", NORM1000) == (1024, 640, 512, 320)
    assert desktop_coords.parse_region(m, [400, 400, 600, 600], NORM1000) == (1024, 640, 512, 320)
    with pytest.raises(desktop_coords.CoordError):
        desktop_coords.parse_region(m, "1,2,3", NORM1000)


def test_model_safe_max_dim_caps_long_short_and_area():
    # 16:10 整屏：短边 768 是约束 → 长边 1228
    assert desktop_coords.model_safe_max_dim(2560, 1600) == 1228
    # 细长窗口：旧规则得到 768x1536（1.18 MP），总像素上限把它压到 1.15 MP 以内
    dim = desktop_coords.model_safe_max_dim(800, 1600)
    assert dim < 1536 and (dim * dim / 2) <= desktop_coords.MODEL_MAX_PIXELS
    # 小区域不放大（0 = 不缩放）；显式设置更小时用设置值
    assert desktop_coords.model_safe_max_dim(800, 600) == 0
    assert desktop_coords.model_safe_max_dim(2560, 1600, 1000) == 1000
    assert desktop_coords.model_safe_max_dim(2560, 1600, 4000) == 1228


# ══════════════════════════════════════════════════════════
# 2. 坐标系与元素编号表
# ══════════════════════════════════════════════════════════


def test_frame_store_and_ttl(monkeypatch):
    ctx = _ctx()
    f = desktop_shots.store_frame(ctx, path="a.jpg", meta=_meta(), mode=NORM1000, shot_id="s1")
    assert desktop_shots.get_frame(ctx).shot_id == "s1"
    assert f.region() == (0, 0, 2560, 1600)
    later = time.monotonic() + desktop_shots.FRAME_TTL_S + 1
    monkeypatch.setattr(desktop_shots.time, "monotonic", lambda: later)
    assert desktop_shots.get_frame(ctx) is None


def test_frames_isolated_per_task():
    a, b = _ctx(task_id=1), _ctx(task_id=2)
    desktop_shots.store_frame(a, path="a.jpg", meta=_meta(), mode=PIXEL, shot_id="A")
    assert desktop_shots.get_frame(b) is None
    assert desktop_shots.get_frame(a).shot_id == "A"


def test_element_refs_lookup_and_stale():
    ctx = _ctx()
    items = [{"n": "保存", "ct": "Button", "r": [10, 20, 100, 40]},
             {"n": "用户名", "ct": "Edit", "r": [10, 80, 300, 30]}]
    mapping = desktop_shots.store_elements(ctx, items, handle=7, title="设置", channel="uia")
    assert list(mapping) == ["e1", "e2"]
    item, table = desktop_shots.lookup_element(ctx, "e2")
    assert item["n"] == "用户名" and table.handle == 7 and not table.stale
    # 模型偶尔只写数字
    assert desktop_shots.lookup_element(ctx, "1")[0]["n"] == "保存"
    assert desktop_shots.lookup_element(ctx, "e9") is None
    desktop_shots.mark_stale(ctx)
    assert desktop_shots.lookup_element(ctx, "e1")[1].stale is True


def test_last_point_expires(monkeypatch):
    ctx = _ctx()
    desktop_shots.note_point(ctx, 5, 6)
    assert desktop_shots.last_point(ctx) == (5, 6)
    later = time.monotonic() + desktop_shots.LAST_POINT_TTL_S + 1
    monkeypatch.setattr(desktop_shots.time, "monotonic", lambda: later)
    assert desktop_shots.last_point(ctx) is None


# ══════════════════════════════════════════════════════════
# 3. 工具行为（内核假实现）
# ══════════════════════════════════════════════════════════

_WIN = {"h": 100, "title": "登录 - Google Chrome", "cls": "Chrome_WidgetWin_1", "pid": 1,
        "proc": "chrome", "rect": [0, 0, 2560, 1528], "fg": True, "min": False}


class _FakeCore:
    """记录每次内核调用；按 op 返回与真实内核同构的数据。"""

    def __init__(self, settle: dict | None = None, snapshot: list | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.settle = settle if settle is not None else {"stable": True, "elapsedMs": 280, "checks": 4,
                                                          "changed": True}
        self.snapshot_items = snapshot or []

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    def params(self, op: str) -> dict:
        return next(p for o, p in self.calls if o == op)

    async def __call__(self, op, params=None):
        p = dict(params or {})
        self.calls.append((op, p))
        if op == "windows":
            return {"items": [dict(_WIN)], "count": 1, "foregroundTitle": _WIN["title"]}
        if op == "frame":
            return {"frame": [0, 0, 2560, 1528]}
        if op == "info":
            return {"virtualScreen": {"x": 0, "y": 0, "w": 2560, "h": 1600}}
        if op == "shot":
            w = int(p.get("w") or 2560)
            h = int(p.get("h") or 1600)
            md = int(p.get("maxDim") or 0)
            sc = min(1.0, md / max(w, h)) if md else 1.0
            return {"path": p.get("path") or "x.jpg", "srcX": int(p.get("x") or 0), "srcY": int(p.get("y") or 0),
                    "srcW": w, "srcH": h, "width": round(w * sc), "height": round(h * sc),
                    "bytes": 1000, "scale": sc}
        if op in ("click", "type", "keys", "scroll", "move", "drag"):
            out = {"target": _WIN["title"], "focusedNow": False}
            if op == "click":
                out.update(clicked=True, at=[p.get("x"), p.get("y")], typed=len(p.get("text") or ""))
            if op == "type":
                out.update(typed=len(p.get("text") or ""))
            if p.get("settle"):
                out["settle"] = dict(self.settle)
            return out
        if op == "settle":
            return {"stable": True, "elapsedMs": 200, "checks": 3}
        if op == "snapshot":
            return {"nodes": len(self.snapshot_items), "items": list(self.snapshot_items)}
        if op == "focus":
            return {"focused": True, "foreground": True}
        return {}


@pytest.fixture
def core(monkeypatch):
    from app.core.config import settings
    from app.orchestration.tools import desktop as D

    fake = _FakeCore()
    monkeypatch.setattr(D, "_call", fake)
    monkeypatch.setattr(settings, "desktop_enabled", True)
    monkeypatch.setattr(settings, "desktop_plain_ops_enabled", True)
    monkeypatch.setattr(settings, "desktop_coord_mode", "auto", raising=False)
    D._SCREEN_CACHE.update(at=0.0, size=None)
    return fake


@pytest.mark.asyncio
async def test_screenshot_states_convention_and_sets_frame(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path, model="gemini-3.8-flash")
    res = await D.DesktopScreenshotTool().run({}, ctx)
    assert res.ok, res.error
    assert "坐标口径：0–1000 比例" in res.output
    frame = desktop_shots.get_frame(ctx)
    assert frame is not None and frame.mode == NORM1000 and frame.region_source == "full"
    # 整屏 2560x1600 缩到模型可见尺寸
    assert core.params("shot")["maxDim"] == 1228

    res2 = await D.DesktopScreenshotTool().run({}, _ctx(tmp_path, task_id=2, model="claude-opus-5-5"))
    assert "坐标口径：图上像素" in res2.output and "1228" in res2.output

    # DeepSeek 等没有原生 0–1000 口径的模型：图上像素
    res3 = await D.DesktopScreenshotTool().run({}, _ctx(tmp_path, task_id=3, model="deepseek-flash"))
    assert "坐标口径：图上像素" in res3.output


@pytest.mark.asyncio
async def test_click_xy_maps_frame_and_returns_new_shot(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    desktop_shots.store_frame(ctx, path="a.jpg", meta=_meta(), mode=NORM1000, shot_id="s1")
    res = await D.DesktopClickTool().run({"x": 500, "y": 250}, ctx)
    assert res.ok, res.error
    click = core.params("click")
    assert (click["x"], click["y"]) == (1280, 400)
    # 动作前采样 + 动作后等稳定的参数随请求下发
    assert click["settle"] is True and click["settleMs"] == 1500
    # then_shot 默认开启：动作后截图成为新的坐标系
    assert core.ops().count("shot") == 1
    assert "[动作后的界面]" in res.output and "坐标口径" in res.output
    assert desktop_shots.get_frame(ctx).shot_id != "s1"


@pytest.mark.asyncio
async def test_click_requires_frame_for_coordinates(core, tmp_path):
    from app.orchestration.tools import desktop as D

    res = await D.DesktopClickTool().run({"x": 10, "y": 10}, _ctx(tmp_path))
    assert not res.ok and "desktop_screenshot" in res.error
    assert "click" not in core.ops()


@pytest.mark.asyncio
async def test_click_reports_no_visible_change(core, tmp_path):
    from app.orchestration.tools import desktop as D

    core.settle = {"stable": True, "elapsedMs": 905, "checks": 12, "changed": False}
    ctx = _ctx(tmp_path)
    desktop_shots.store_frame(ctx, path="a.jpg", meta=_meta(), mode=NORM1000)
    res = await D.DesktopClickTool().run({"x": 500, "y": 500, "then_shot": False}, ctx)
    assert res.ok
    assert "没有可见变化" in res.output
    assert "shot" not in core.ops(), "then_shot=false 时不应截图"


@pytest.mark.asyncio
async def test_type_forwards_keys_clear_first_and_last_point(core, tmp_path):
    """输入 + 提交一次完成；最近一次点击位置随请求下发，内核据此观察输入框附近的变化。"""
    from app.orchestration.tools import desktop as D

    core.settle = {"stable": True, "elapsedMs": 300, "checks": 4, "changed": False}
    ctx = _ctx(tmp_path)
    desktop_shots.note_point(ctx, 700, 500)
    res = await D.DesktopTypeTool().run({"text": "hello", "keys": "ENTER", "clear_first": True}, ctx)
    assert res.ok, res.error
    p = core.params("type")
    assert p["text"] == "hello" and p["keys"] == "ENTER" and p["clearFirst"] is True
    assert (p["lx"], p["ly"]) == (700, 500)
    # 输入 / 按键不给「无变化」提示：焦点框、光标这类小变化不一定看得出
    assert "没有可见变化" not in res.output


@pytest.mark.asyncio
async def test_click_by_element_and_stale_relocation(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.orchestration.tools import desktop as D

    items = [{"n": "保存", "ct": "Button", "en": True, "r": [100, 200, 80, 30]},
             {"n": "取消", "ct": "Button", "en": True, "r": [200, 200, 80, 30]}]
    fake = _FakeCore(snapshot=items)
    monkeypatch.setattr(D, "_call", fake)
    monkeypatch.setattr(settings, "desktop_enabled", True)
    monkeypatch.setattr(settings, "desktop_plain_ops_enabled", True)
    win = dict(_WIN, cls="Notepad", title="设置")
    monkeypatch.setattr(D, "_resolve_window", lambda h, t: _async(win))

    ctx = _ctx(tmp_path)
    snap = await D.DesktopSnapshotTool().run({"handle": 100}, ctx)
    assert snap.ok and "e1" in snap.output and "保存" in snap.output
    assert "(100," not in snap.output, "元素清单不应再列屏幕坐标"

    res = await D.DesktopClickTool().run({"element": "e1", "then_shot": False}, ctx)
    assert res.ok, res.error
    assert (fake.params("click")["x"], fake.params("click")["y"]) == (140, 215)

    # 点击后编号过期：界面里「保存」移动了位置 → 按名称唯一命中，点新位置
    fake.snapshot_items = [{"n": "保存", "ct": "Button", "en": True, "r": [300, 400, 80, 30]}]
    fake.calls.clear()
    res2 = await D.DesktopClickTool().run({"element": "e1", "then_shot": False}, ctx)
    assert res2.ok, res2.error
    assert (fake.params("click")["x"], fake.params("click")["y"]) == (340, 415)
    assert "重新定位" in res2.output

    # 重新定位不唯一 → 拒绝而不是猜
    fake.snapshot_items = [{"n": "保存", "ct": "Button", "r": [1, 1, 10, 10]},
                           {"n": "保存", "ct": "Button", "r": [50, 50, 10, 10]}]
    fake.calls.clear()
    res3 = await D.DesktopClickTool().run({"element": "e1", "then_shot": False}, ctx)
    assert not res3.ok and "唯一" in res3.error
    assert "click" not in fake.ops()


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_batch_validates_all_steps_before_executing(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    desktop_shots.store_frame(ctx, path="a.jpg", meta=_meta(), mode=NORM1000)
    res = await D.DesktopBatchTool().run({"actions": [
        {"action": "click", "x": 500, "y": 500},
        {"action": "type", "text": "ok"},
        {"action": "click", "x": 1500, "y": 2000},   # 越界：整批都不执行
    ]}, ctx)
    assert not res.ok and "第 3 步" in res.error and "未执行任何步骤" in res.error
    assert not {"click", "type"} & set(core.ops())


@pytest.mark.asyncio
async def test_batch_runs_in_order_then_settles_and_shoots(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    desktop_shots.store_frame(ctx, path="a.jpg", meta=_meta(), mode=NORM1000)
    res = await D.DesktopBatchTool().run({"actions": [
        {"action": "click", "x": 500, "y": 500},
        {"action": "type", "text": "admin"},
        {"action": "key", "keys": "TAB"},
        {"action": "type", "text": "pwd"},
        {"action": "press", "keys": "ENTER"},    # 别名
    ], "title": "登录"}, ctx)
    assert res.ok, res.error
    seq = [op for op in core.ops() if op in ("click", "type", "keys", "settle", "shot")]
    assert seq == ["click", "type", "keys", "type", "keys", "settle", "shot"]
    assert "已执行 5/5 步" in res.output and "[动作后的界面]" in res.output
    # 批量里每步都带前台校验，但不在每步里重复做动作后采样（只在结尾等一次）
    assert all("settle" not in p for op, p in core.calls if op in ("click", "type", "keys"))


@pytest.mark.asyncio
async def test_zoom_does_not_change_frame(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    desktop_shots.store_frame(ctx, path="a.jpg", meta=_meta(), mode=NORM1000, shot_id="base")
    res = await D.DesktopScreenshotTool().run({"zoom": "400,400,600,600"}, ctx)
    assert res.ok, res.error
    shot = core.params("shot")
    assert (shot["x"], shot["y"], shot["w"], shot["h"]) == (1024, 640, 512, 320)
    assert shot["fit"] is True
    assert desktop_shots.get_frame(ctx).shot_id == "base"
    assert "shot_id=base" in res.output


@pytest.mark.asyncio
async def test_old_core_without_settle_still_waits_before_shot(core, tmp_path, monkeypatch):
    """旧内核不回 settle：动作后固定等待一下再截图，避免截到动作前的画面。"""
    from app.orchestration.tools import desktop as D

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(sec, *a, **k):
        slept.append(sec)
        await real_sleep(0)

    monkeypatch.setattr(D.asyncio, "sleep", fake_sleep)
    original = core.__call__

    async def no_settle(op, params=None):
        out = await original(op, params)
        out.pop("settle", None)
        return out

    monkeypatch.setattr(D, "_call", no_settle)
    ctx = _ctx(tmp_path)
    desktop_shots.store_frame(ctx, path="a.jpg", meta=_meta(), mode=NORM1000)
    res = await D.DesktopClickTool().run({"x": 500, "y": 500}, ctx)
    assert res.ok
    assert 0.4 in slept and "[动作后的界面]" in res.output


# ══════════════════════════════════════════════════════════
# 4. 提速：截图裁剪与工具等待
# ══════════════════════════════════════════════════════════


def test_prune_tool_images_keeps_latest_in_chunks():
    from app.models.schemas import ChatMessage
    from app.orchestration.agent_loop import _prune_tool_images

    def img(i):
        return ChatMessage(role="user", content=f"img{i}",
                           content_blocks=[{"type": "image_url", "image_url": {"url": f"data:,{i}"}}])

    msgs = [img(i) for i in range(6)]
    assert _prune_tool_images(msgs) == 0, "未超过「保留 3 + 块 3」时不裁，减少提示缓存失效"
    msgs.append(img(6))
    assert _prune_tool_images(msgs) == 4
    live = [m for m in msgs if m.content_blocks]
    assert [m.content for m in live] == ["img4", "img5", "img6"]
    assert all("已省略" in m.content for m in msgs[:4])


@pytest.mark.asyncio
async def test_poll_cancel_returns_without_polling_delay():
    from app.orchestration.agent_loop import _poll_cancel

    async def quick():
        return 42

    ev = asyncio.Event()
    t0 = time.monotonic()
    assert await _poll_cancel(asyncio.ensure_future(quick()), ev) == 42
    assert time.monotonic() - t0 < 0.1, "工具完成后应立即返回，不再等 0.2 s 轮询点"


@pytest.mark.asyncio
async def test_poll_cancel_cancels_on_event():
    from app.orchestration.agent_loop import _poll_cancel

    ev = asyncio.Event()
    task = asyncio.ensure_future(asyncio.sleep(5))
    asyncio.get_running_loop().call_later(0.05, ev.set)
    with pytest.raises(asyncio.CancelledError):
        await _poll_cancel(task, ev)
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()

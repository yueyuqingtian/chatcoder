"""电脑操控「元素优先 + 落点反馈」单测（plan-20261019-desktop-targeting）。

背景：实录里 deepseek-flash 在 0–1000 口径下 x 轴换算了、y 轴没换，(770,336) 落在标题文字上，
连点 20 多次都落空，结果里只有「已点击」三个字，它一直以为点中了输入框。本文件覆盖修复它的几块确定性代码：

1. **元素清单**（core/desktop_elements）：筛选 / 去重 / 借名 / 封顶 / 分组排序、清单文本、按文字或编号定位、
   点击点附近的候选、命中元素描述与「被遮挡」判定。
2. **叠加层**（core/desktop_overlay）：点击落点标记、元素编号框、参考网格——用像素断言「真的画上去了」。
3. **工具行为**（内核用假实现）：截图自带清单并登记编号表、按文字点击、点偏时的反馈与附近候选、
   动作后清单去重、批量动作按元素解析、按键重复、DPI 感知告警。
"""
from __future__ import annotations

import base64
import io

import pytest

from app.core import desktop_elements as E
from app.core import desktop_overlay, desktop_shots
from app.orchestration.tools.base import ToolContext


@pytest.fixture(autouse=True)
def _clean_state():
    desktop_shots.clear()
    yield
    desktop_shots.clear()


def _ctx(tmp_path=None, *, session_id: int = 1, task_id: int = 1, model: str = "deepseek-flash") -> ToolContext:
    return ToolContext(
        workspace_root=str(tmp_path or "."), session_id=session_id, task_id=task_id,
        agent_id=1, agent_name="t", model_name=model,
    )


# ══════════════════════════════════════════════════════════
# 1. 元素清单
# ══════════════════════════════════════════════════════════


def _web_items() -> list[dict]:
    """模拟 Chrome 里的登录页：网页内容 d=1，浏览器自身界面 d=0。"""
    return [
        {"n": "最小化", "ct": "Button", "ix": True, "en": True, "d": 0, "r": [2034, 152, 68, 59]},
        {"n": "地址和搜索栏", "ct": "Edit", "ix": True, "en": True, "d": 0, "r": [596, 227, 1297, 37]},
        {"n": "用户登录", "ct": "Text", "ix": False, "d": 1, "r": [1121, 581, 450, 37]},
        {"n": "用户名", "ct": "Edit", "ix": True, "en": True, "d": 1, "r": [1121, 636, 416, 56]},
        {"n": "密码", "ct": "Edit", "ix": True, "en": True, "d": 1, "r": [1121, 710, 416, 56]},
        {"n": "验证码", "ct": "Edit", "ix": True, "en": True, "d": 1, "r": [1121, 784, 416, 56]},
        {"n": "登 录", "ct": "Text", "ix": False, "d": 1, "r": [1288, 872, 56, 31]},
    ]


def test_prepare_groups_page_content_first_and_numbers_in_reading_order():
    out = E.prepare(_web_items())
    names = [E.label_of(it) for it in out]
    # 网页内容在前（保持阅读顺序），浏览器 / 窗口界面在后
    assert names == ["用户登录", "用户名", "密码", "验证码", "登 录", "最小化", "地址和搜索栏"]
    assert [bool(it.get("d")) for it in out] == [True] * 5 + [False] * 2


def test_prepare_filters_region_tiny_duplicates_and_nameless_text():
    items = [
        {"n": "在内", "ct": "Button", "ix": True, "r": [100, 100, 80, 30]},
        {"n": "在外", "ct": "Button", "ix": True, "r": [5000, 5000, 80, 30]},       # 不在区域内
        {"n": "碎片", "ct": "Button", "ix": True, "r": [100, 200, 2, 2]},            # 布局碎片
        {"n": "在内", "ct": "Button", "ix": True, "r": [100, 100, 80, 30]},          # 重复
        {"n": "", "ct": "Text", "ix": False, "r": [300, 300, 80, 30]},               # 无名文字
        {"n": "", "ct": "Button", "ix": True, "r": [400, 400, 40, 40]},              # 无名可交互：保留
    ]
    out = E.prepare(items, region=[0, 0, 1000, 1000])
    assert [E.label_of(it) for it in out] == ["在内", "(无名称)"]


def test_prepare_drops_text_covered_by_link_and_lends_label_to_unnamed_button():
    items = [
        {"n": "详情12", "ct": "Hyperlink", "ix": True, "r": [100, 100, 80, 24]},
        {"n": "详情12", "ct": "Text", "ix": False, "r": [104, 102, 60, 20]},        # 链接里的文字子节点：与父重复
        {"n": "", "ct": "Button", "ix": True, "r": [300, 100, 100, 32]},             # 无名按钮
        {"n": "保存", "ct": "Text", "ix": False, "r": [320, 104, 40, 24]},           # 它的标签文字：借给按钮
        {"n": "独立文字", "ct": "Text", "ix": False, "r": [600, 100, 80, 24]},
    ]
    out = E.prepare(items)
    labels = [E.label_of(it) for it in out]
    assert labels == ["详情12", "保存", "独立文字"]
    assert out[1]["ct"] == "Button", "借名后仍是按钮，被点的是按钮本身"


def test_prepare_caps_total_and_prefers_interactive():
    items = [{"n": f"文字{i}", "ct": "Text", "ix": False, "r": [10, i * 30, 60, 20]} for i in range(30)]
    items += [{"n": f"按钮{i}", "ct": "Button", "ix": True, "r": [200, i * 30, 60, 20]} for i in range(30)]
    out = E.prepare(items, limit=40)
    assert len(out) == 40
    assert sum(1 for it in out if it["ix"]) == 30, "可交互元素全部保留，文字只补满余量"
    # 输出仍按原阅读顺序
    idx = [items.index(next(x for x in items if x["n"] == E.label_of(it))) for it in out]
    assert idx == sorted(idx)


def test_format_list_groups_and_lines():
    mapping = desktop_shots.store_elements(_ctx(), E.prepare(_web_items()), handle=7, title="登录")
    lines = E.format_list(mapping)
    assert lines[0] == "【网页内容】" and "【浏览器 / 窗口界面】" in lines
    text = "\n".join(lines)
    assert "e2  输入框「用户名」  （可输入）" in text
    assert "文字「登 录」" in text
    # 清单里不列任何屏幕坐标
    assert "1121" not in text and "636" not in text


def test_duplicate_labels_get_a_row_context_hint():
    """表格里每行都有「收费」：同名元素补一句「紧跟在「X」之后」，模型据此挑编号；唯一名称不加。"""
    items = [
        {"n": "患者A", "ct": "Text", "ix": False, "r": [10, 10, 60, 20]},
        {"n": "收费", "ct": "Button", "ix": True, "r": [200, 10, 60, 20]},
        {"n": "患者B", "ct": "Text", "ix": False, "r": [10, 50, 60, 20]},
        {"n": "收费", "ct": "Button", "ix": True, "r": [200, 50, 60, 20]},
        {"n": "导出", "ct": "Button", "ix": True, "r": [400, 10, 60, 20]},
    ]
    mapping = desktop_shots.store_elements(_ctx(), E.prepare(items), handle=1, title="t")
    text = "\n".join(E.format_list(mapping))
    assert "收费」  〔紧跟在「患者A」之后〕" in text and "收费」  〔紧跟在「患者B」之后〕" in text
    assert "导出」  〔" not in text and "患者A」  〔" not in text


def test_format_line_marks_disabled_and_current_value():
    assert "已禁用" in E.format_line("e1", {"n": "前进", "ct": "Button", "en": False})
    line = E.format_line("e2", {"n": "备注", "ct": "Edit", "en": True, "val": "你好", "editable": True})
    assert "当前值「你好」" in line


def test_find_by_text_normalizes_spaces_and_prefers_interactive():
    mapping = desktop_shots.store_elements(_ctx(), E.prepare(_web_items()), handle=7, title="登录")
    # 「登录」与「登 录」视为同一个
    hits = E.find_by_text(mapping, "登录")
    assert [E.label_of(it) for _r, it in hits] == ["登 录"]
    assert E.find_by_text(mapping, "没有这个") == []
    assert E.find_by_text(mapping, "") == []
    # 包含匹配：「用户」同时命中「用户登录」(文字) 与「用户名」(输入框)，可交互的排前面
    partial = E.find_by_text(mapping, "用户")
    assert E.label_of(partial[0][1]) == "用户名"


def test_find_by_text_exact_beats_partial():
    items = [
        {"n": "登录", "ct": "Button", "ix": True, "r": [10, 10, 60, 30]},
        {"n": "登录后查看更多", "ct": "Hyperlink", "ix": True, "r": [10, 60, 160, 30]},
    ]
    mapping = desktop_shots.store_elements(_ctx(), E.prepare(items), handle=1, title="t")
    hits = E.find_by_text(mapping, "登录")
    assert [E.label_of(it) for _r, it in hits] == ["登录"]


@pytest.mark.parametrize("text,expected", [
    ("e5", True), ("E12", True), ("7", True), ("登录", False), ("e", False), ("email", False), ("", False),
])
def test_looks_like_ref(text, expected):
    assert E.looks_like_ref(text) is expected


def test_nearest_orders_by_distance_with_direction_and_skips_text():
    mapping = desktop_shots.store_elements(_ctx(), E.prepare(_web_items()), handle=7, title="登录")
    # 点在「用户登录」标题上（(1300, 599)）：最近的可交互元素是下方的用户名框
    near = E.nearest(mapping, 1300, 599)
    assert [E.label_of(it) for _r, it, _d in near][:2] == ["用户名", "密码"]
    assert all(d == "下方" for _r, _it, d in near[:2])
    assert all(E.is_interactive(it) for _r, it, _d in near)
    # 半径之外没有候选
    assert E.nearest(mapping, 100, 1500, max_dist=50) == []


def test_hit_summary_kinds():
    assert E.hit_summary(None) == ("none", "")
    kind, text = E.hit_summary({"n": "用户名", "ct": "Edit", "ix": True})
    assert kind == "interactive" and "输入框「用户名」" in text
    kind, text = E.hit_summary({"n": "用户登录", "ct": "Text", "ix": False})
    assert kind == "text" and "文字「用户登录」" in text and "不是可点击控件" in text
    kind, text = E.hit_summary({"n": "", "ct": "Pane", "ix": False})
    assert kind == "blank" and "没有可识别的控件" in text


def test_hit_conflicts_detects_cover_but_allows_descendants():
    target = {"n": "确定", "ct": "Button", "r": [100, 100, 100, 40]}
    # 点击点下是另一个有名字的元素（弹层）→ 冲突
    assert E.hit_conflicts({"n": "提示", "ct": "Pane", "r": [0, 0, 800, 600]}, target) is True
    # 名称互相包含（链接里的文字子节点、按钮里的图标）→ 同一处
    assert E.hit_conflicts({"n": "确定", "ct": "Text", "r": [120, 105, 40, 20]}, target) is False
    # 命中元素在目标内部（名称不同，如按钮里的图标）→ 同一处
    assert E.hit_conflicts({"n": "对勾", "ct": "Image", "r": [110, 110, 20, 20]}, target) is False
    # 命中元素没有名称：无从比较，不报
    assert E.hit_conflicts({"n": "", "ct": "Pane", "r": [0, 0, 800, 600]}, target) is False
    assert E.hit_conflicts(None, target) is False


def test_list_signature_ignores_positions_and_browser_chrome():
    a = desktop_shots.store_elements(_ctx(task_id=1), E.prepare(_web_items()), handle=7, title="t")
    items = _web_items()
    for it in items:
        it["r"] = [it["r"][0] + 5, it["r"][1] + 9, it["r"][2], it["r"][3]]       # 整体平移
    items[0]["n"] = "最大化"                                                       # 浏览器界面变了也不算
    b = desktop_shots.store_elements(_ctx(task_id=2), E.prepare(items), handle=7, title="t")
    assert desktop_shots.list_signature(a) == desktop_shots.list_signature(b)
    items[3]["n"] = "账号"                                                         # 网页内容变了才算
    c = desktop_shots.store_elements(_ctx(task_id=3), E.prepare(items), handle=7, title="t")
    assert desktop_shots.list_signature(a) != desktop_shots.list_signature(c)


def test_overlay_flags_are_sticky_per_task():
    ctx = _ctx()
    assert desktop_shots.get_overlay(ctx) == {"marks": False, "grid": False}
    desktop_shots.set_overlay(ctx, marks=True)
    assert desktop_shots.get_overlay(ctx) == {"marks": True, "grid": False}
    desktop_shots.set_overlay(ctx, grid=True)
    desktop_shots.set_overlay(ctx, marks=False)
    assert desktop_shots.get_overlay(ctx) == {"marks": False, "grid": True}
    assert desktop_shots.get_overlay(_ctx(task_id=2)) == {"marks": False, "grid": False}


# ══════════════════════════════════════════════════════════
# 2. 叠加层（像素断言）
# ══════════════════════════════════════════════════════════

_META = {"srcX": 100, "srcY": 50, "srcW": 2000, "srcH": 1200, "width": 1000, "height": 600}


def _gray_jpg(tmp_path, size=(1000, 600)) -> str:
    from PIL import Image

    p = tmp_path / "base.jpg"
    Image.new("RGB", size, (200, 200, 200)).save(p, quality=100, subsampling=0)
    return str(p)


def _px(path: str, x: int, y: int) -> tuple[int, int, int]:
    from PIL import Image

    with Image.open(path) as im:
        return im.convert("RGB").getpixel((x, y))


def _is_red(rgb) -> bool:
    return rgb[0] > 200 and rgb[1] < 120 and rgb[2] < 150


def test_draw_clicks_marks_landing_point_in_image_space(tmp_path):
    path = _gray_jpg(tmp_path)
    # 屏幕 (1100, 650) → 图上 ((1100-100)*0.5, (650-50)*0.5) = (500, 300)
    assert desktop_overlay.draw_clicks(path, _META, [(1100, 650, "")]) is True
    assert _is_red(_px(path, 500 + 15, 300)), "圆环落在落点周围半径 15 处"
    assert _px(path, 500, 300) == pytest.approx((200, 200, 200), abs=6), "中心留空，不盖住被点的元素"
    assert _px(path, 50, 50) == pytest.approx((200, 200, 200), abs=6), "其它位置不动"


def test_draw_clicks_ignores_points_outside_image(tmp_path):
    path = _gray_jpg(tmp_path)
    assert desktop_overlay.draw_clicks(path, _META, [(5000, 5000, "")]) is False
    assert desktop_overlay.draw_clicks(path, _META, []) is False
    assert _px(path, 500, 300) == pytest.approx((200, 200, 200), abs=6)


def test_draw_clicks_numbers_multiple_points(tmp_path):
    path = _gray_jpg(tmp_path)
    ok = desktop_overlay.draw_clicks(path, _META, [(300, 200, ""), (1500, 800, "")])
    assert ok is True
    assert _is_red(_px(path, 100 + 15, 75)) and _is_red(_px(path, 700 + 15, 375))


def test_draw_marks_boxes_elements(tmp_path):
    path = _gray_jpg(tmp_path)
    # 屏幕矩形 (500, 250, 400, 200) → 图上 (200, 100, 200, 100)
    assert desktop_overlay.draw_marks(path, _META, [("e3", (500, 250, 400, 200))]) is True
    assert _is_red(_px(path, 200, 150)) or _is_red(_px(path, 201, 150)), "左边框"
    assert _is_red(_px(path, 300, 100)) or _is_red(_px(path, 300, 101)), "上边框"
    assert _px(path, 300, 150) == pytest.approx((200, 200, 200), abs=6), "框内不填色"
    assert desktop_overlay.draw_marks(path, _META, []) is False


def test_draw_grid_pixel_and_norm_modes(tmp_path):
    from PIL import Image

    p1 = _gray_jpg(tmp_path)
    assert desktop_overlay.draw_grid(p1, _META, "pixel") is True
    # 像素口径：x=100 处有一条竖线（半透明红叠在灰上，应偏红）
    r, g, b = _px(p1, 100, 300)
    assert r > g + 20, "网格线在 x=100 处"
    # 比例口径：x=500 单位 = 图宽一半 = 像素 500
    p2 = tmp_path / "base2.jpg"
    Image.new("RGB", (1000, 600), (200, 200, 200)).save(p2, quality=100, subsampling=0)
    assert desktop_overlay.draw_grid(str(p2), _META, "norm1000") is True
    r, g, b = _px(str(p2), 500, 300)
    assert r > g + 20


def test_overlay_never_raises_on_bad_input(tmp_path):
    assert desktop_overlay.draw_clicks(str(tmp_path / "nope.jpg"), _META, [(1, 1, "")]) is False
    assert desktop_overlay.draw_marks(str(tmp_path / "nope.jpg"), _META, [("e1", (1, 1, 5, 5))]) is False
    assert desktop_overlay.draw_grid(str(tmp_path / "nope.jpg"), _META, "pixel") is False


# ══════════════════════════════════════════════════════════
# 3. 工具行为（内核假实现）
# ══════════════════════════════════════════════════════════

_WIN = {"h": 100, "title": "登录 - Google Chrome", "cls": "Chrome_WidgetWin_1", "pid": 1,
        "proc": "chrome", "rect": [0, 0, 2560, 1528], "fg": True, "min": False}


class _Core:
    """记录每次内核调用；按 op 返回与真实内核同构的数据，shot 会写出真实的 JPEG（叠加层要读它）。"""

    def __init__(self, items: list[dict] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.items = items if items is not None else _web_items()
        self.settle = {"stable": True, "elapsedMs": 280, "checks": 4, "changed": True}
        self.hit: dict | None = None
        self.dpi = "PerMonitorV2"

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    def params(self, op: str) -> dict:
        return next(p for o, p in self.calls if o == op)

    async def __call__(self, op, params=None):
        from PIL import Image

        p = dict(params or {})
        self.calls.append((op, p))
        if op == "windows":
            return {"items": [dict(_WIN)], "count": 1, "foregroundTitle": _WIN["title"]}
        if op == "frame":
            return {"frame": [0, 0, 2560, 1528]}
        if op == "info":
            return {"virtualScreen": {"x": 0, "y": 0, "w": 2560, "h": 1600}, "dpiAwareness": self.dpi,
                    "monitors": [{"w": 2560, "h": 1600, "scale": 150, "primary": True}], "version": 1.1}
        if op == "shot":
            w, h = int(p.get("w") or 2560), int(p.get("h") or 1600)
            md = int(p.get("maxDim") or 0)
            sc = min(1.0, md / max(w, h)) if md else 1.0
            ow, oh = round(w * sc), round(h * sc)
            Image.new("RGB", (ow, oh), (200, 200, 200)).save(p["path"], quality=100, subsampling=0)
            return {"path": p["path"], "srcX": int(p.get("x") or 0), "srcY": int(p.get("y") or 0),
                    "srcW": w, "srcH": h, "width": ow, "height": oh, "bytes": 1000, "scale": sc}
        if op in ("click", "type", "keys", "scroll", "move", "drag"):
            out = {"target": _WIN["title"], "focusedNow": False}
            if op == "click":
                out.update(clicked=True, at=[p.get("x"), p.get("y")], typed=len(p.get("text") or ""),
                           cursor=[p.get("x"), p.get("y")], landErr=0)
                if self.hit is not None:
                    out["hit"] = dict(self.hit)
            if op == "keys":
                out["pressed"] = int(p.get("repeat") or 1) * max(1, len(str(p.get("keys")).split()))
            if p.get("settle"):
                out["settle"] = dict(self.settle)
            return out
        if op == "settle":
            return {"stable": True, "elapsedMs": 200, "checks": 3}
        if op == "snapshot":
            return {"nodes": len(self.items), "items": [dict(i) for i in self.items], "path": "findall",
                    "chromium": True, "doc": [1100, 560, 500, 400], "total": len(self.items), "truncated": False}
        if op == "focus":
            return {"focused": True, "foreground": True}
        return {}


@pytest.fixture
def core(monkeypatch):
    from app.core.config import settings
    from app.orchestration.tools import desktop as D

    fake = _Core()
    monkeypatch.setattr(D, "_call", fake)
    monkeypatch.setattr(settings, "desktop_enabled", True)
    monkeypatch.setattr(settings, "desktop_plain_ops_enabled", True)
    monkeypatch.setattr(settings, "desktop_coord_mode", "auto", raising=False)
    monkeypatch.setattr(settings, "desktop_auto_elements", True, raising=False)
    D._SCREEN_CACHE.update(at=0.0, size=None)
    D._ENV_CACHE.update(at=0.0, info=None, logged=False)
    D._CDP_FAILED.clear()
    return fake


@pytest.mark.asyncio
async def test_screenshot_lists_elements_and_registers_table(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    res = await D.DesktopScreenshotTool().run({}, ctx)
    assert res.ok, res.error
    assert "页面元素（7 项）" in res.output and "【网页内容】" in res.output
    assert "输入框「用户名」" in res.output and "文字「登 录」" in res.output
    assert res.data["element_count"] == 7
    # 前台窗口是 Chrome：快照请求带上了预热上限
    assert core.params("snapshot")["warmupMs"] > 0
    table = desktop_shots.get_table(ctx)
    assert table is not None and not table.stale and table.handle == 100
    assert desktop_shots.lookup_element(ctx, "e2")[0]["n"] == "用户名"


@pytest.mark.asyncio
async def test_screenshot_elements_false_skips_listing(core, tmp_path):
    from app.orchestration.tools import desktop as D

    res = await D.DesktopScreenshotTool().run({"elements": False}, _ctx(tmp_path))
    assert res.ok and "页面元素" not in res.output
    assert "snapshot" not in core.ops()


@pytest.mark.asyncio
async def test_self_window_is_not_listed_unless_targeted(core, tmp_path, monkeypatch):
    """前台是 chatcoder 自己的界面：不读它的无障碍树（没意义，还会让它开无障碍模式）。"""
    from app.orchestration.tools import desktop as D

    me = dict(_WIN, proc="chatcoder", title="chatcoder")

    async def fg():
        return me

    monkeypatch.setattr(D, "_foreground_window", fg)
    res = await D.DesktopScreenshotTool().run({}, _ctx(tmp_path))
    assert res.ok and "页面元素" not in res.output and "snapshot" not in core.ops()


@pytest.mark.asyncio
async def test_click_by_text_resolves_through_table(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    await D.DesktopScreenshotTool().run({}, ctx)
    core.calls.clear()
    res = await D.DesktopClickTool().run({"element": "登录", "then_shot": False}, ctx)
    assert res.ok, res.error
    click = core.params("click")
    # 「登 录」的矩形 [1288, 872, 56, 31] 的中心
    assert (click["x"], click["y"]) == (1288 + 28, 872 + 15)
    assert "按文字「登录」匹配" in res.output
    assert "snapshot" not in core.ops(), "清单还新鲜时按文字定位不再读一遍界面"
    # 没点名窗口时，按元素所属窗口做前台校验：用户中途切走窗口后不会点到别的应用上
    assert click["handle"] == 100


@pytest.mark.asyncio
async def test_click_by_text_refreshes_when_table_stale_or_missing(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    res = await D.DesktopClickTool().run({"element": "用户名", "then_shot": False}, ctx)
    assert res.ok, res.error
    assert "snapshot" in core.ops(), "没有清单时按当前界面现读一份"
    click = core.params("click")
    assert (click["x"], click["y"]) == (1121 + 208, 636 + 28)


@pytest.mark.asyncio
async def test_click_by_text_not_found_and_ambiguous(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    res = await D.DesktopClickTool().run({"element": "不存在的按钮", "then_shot": False}, ctx)
    assert not res.ok and "没有找到文字为「不存在的按钮」" in res.error
    assert "click" not in core.ops()

    core.items = [
        {"n": "收费", "ct": "Button", "ix": True, "en": True, "d": 1, "r": [100, 100, 60, 30]},
        {"n": "收费", "ct": "Button", "ix": True, "en": True, "d": 1, "r": [100, 200, 60, 30]},
    ]
    desktop_shots.clear(ctx)
    res2 = await D.DesktopClickTool().run({"element": "收费", "then_shot": False}, ctx)
    assert not res2.ok and "匹配到 2 个元素" in res2.error and "e1" in res2.error and "e2" in res2.error
    assert "click" not in core.ops(), "有歧义时不猜"


@pytest.mark.asyncio
async def test_click_feedback_on_wrong_target_suggests_nearby_elements(core, tmp_path):
    """复现实录：想点用户名框，坐标落在「用户登录」标题上——结果必须说清点到了什么、附近有什么。"""
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)                       # deepseek-flash → 图上像素口径
    shot = await D.DesktopScreenshotTool().run({}, ctx)
    assert "图上像素" in shot.output
    core.hit = {"n": "用户登录", "ct": "Text", "ix": False, "r": [1121, 581, 450, 37]}
    core.calls.clear()
    # 图 1228×768 ↔ 屏幕 2560×1600（窗口 frame 2560×1528 的整屏近似）：取标题中心 (1346, 599)
    meta = desktop_shots.get_frame(ctx).meta
    ix = round((1346 - meta["srcX"]) * meta["width"] / meta["srcW"])
    iy = round((599 - meta["srcY"]) * meta["height"] / meta["srcH"])
    res = await D.DesktopClickTool().run({"x": ix, "y": iy, "then_shot": False}, ctx)
    assert res.ok, res.error
    assert "点击处元素：文字「用户登录」（不是可点击控件" in res.output
    assert "点击点附近的可交互元素：输入框「用户名」" in res.output
    assert 'element="用户名"' in res.output


@pytest.mark.asyncio
async def test_click_feedback_reports_interactive_hit_and_cover(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    await D.DesktopScreenshotTool().run({}, ctx)
    # 按坐标点中了输入框：只报「点击处元素：…」，不啰嗦
    core.hit = {"n": "用户名", "ct": "Edit", "ix": True, "r": [1121, 636, 416, 56]}
    meta = desktop_shots.get_frame(ctx).meta
    ix = round((1329 - meta["srcX"]) * meta["width"] / meta["srcW"])
    iy = round((664 - meta["srcY"]) * meta["height"] / meta["srcH"])
    res = await D.DesktopClickTool().run({"x": ix, "y": iy, "then_shot": False}, ctx)
    assert "点击处元素：输入框「用户名」。" in res.output and "附近" not in res.output

    # 按编号点，但点击点下是一个弹层：提示目标可能被遮住
    core.hit = {"n": "操作提示", "ct": "Pane", "ix": False, "r": [0, 0, 2560, 1500]}
    desktop_shots.clear(ctx)
    res2 = await D.DesktopClickTool().run({"element": "用户名", "then_shot": False}, ctx)
    assert res2.ok
    assert "点击点下实际是" in res2.output and "被弹层 / 浮窗遮住" in res2.output


@pytest.mark.asyncio
async def test_click_reports_landing_error_when_cursor_missed(core, tmp_path, monkeypatch):
    from app.orchestration.tools import desktop as D

    orig = core.__call__

    async def off(op, params=None):
        out = await orig(op, params)
        if op == "click":
            out["landErr"] = 4
        return out

    monkeypatch.setattr(D, "_call", off)
    ctx = _ctx(tmp_path)
    res = await D.DesktopClickTool().run({"element": "用户名", "then_shot": False}, ctx)
    assert res.ok and "偏差约 4 像素" in res.output


@pytest.mark.asyncio
async def test_after_action_screenshot_marks_landing_ring_and_dedupes_list(core, tmp_path):
    from app.orchestration.tools import desktop as D
    from PIL import Image

    ctx = _ctx(tmp_path)
    await D.DesktopScreenshotTool().run({}, ctx)
    res = await D.DesktopClickTool().run({"element": "用户名", "clear_first": True, "text": "yh002"}, ctx)
    assert res.ok, res.error
    # 动作后的清单与上一份内容一致：不再重复整份，编号沿用
    assert "页面元素与上一份清单一致（共 7 项），编号沿用" in res.output
    assert "【网页内容】" not in res.output.split("[动作后的界面]")[1]
    assert "红色圆环" in res.output
    # 落点标记真的画在了发给模型的图上：用户名框中心 (1329, 664) → 图上像素
    meta = desktop_shots.get_frame(ctx).meta
    ix = round((1329 - meta["srcX"]) * meta["width"] / meta["srcW"])
    iy = round((664 - meta["srcY"]) * meta["height"] / meta["srcH"])
    img = Image.open(io.BytesIO(base64.b64decode(res.data["base64"]))).convert("RGB")
    assert _is_red(img.getpixel((ix + 15, iy)))


@pytest.mark.asyncio
async def test_after_action_prints_new_list_when_content_changes(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    await D.DesktopScreenshotTool().run({}, ctx)
    core.items = _web_items() + [{"n": "验证码错误", "ct": "Text", "ix": False, "d": 1, "r": [1121, 850, 200, 24]}]
    res = await D.DesktopClickTool().run({"element": "登录"}, ctx)
    after = res.output.split("[动作后的界面]")[1]
    assert "页面元素（8 项）" in after and "验证码错误" in after
    assert "新清单已替换旧编号" in res.output


@pytest.mark.asyncio
async def test_after_action_skips_list_when_screen_did_not_change(core, tmp_path):
    from app.orchestration.tools import desktop as D

    core.settle = {"stable": True, "elapsedMs": 905, "checks": 12, "changed": False}
    ctx = _ctx(tmp_path)
    await D.DesktopScreenshotTool().run({}, ctx)
    core.calls.clear()
    res = await D.DesktopClickTool().run({"element": "用户登录"}, ctx)
    assert "没有可见变化" in res.output
    assert "snapshot" not in core.ops(), "画面没变：不再读一遍界面、也不重复清单"


@pytest.mark.asyncio
async def test_screenshot_marks_and_grid_are_sticky_and_drawn(core, tmp_path):
    from app.orchestration.tools import desktop as D
    from PIL import Image

    ctx = _ctx(tmp_path)
    res = await D.DesktopScreenshotTool().run({"marks": True, "grid": True}, ctx)
    assert res.ok, res.error
    assert "红框标出元素" in res.output and "网格线与边缘数字" in res.output
    assert desktop_shots.get_overlay(ctx) == {"marks": True, "grid": True}
    img = Image.open(io.BytesIO(base64.b64decode(res.data["base64"]))).convert("RGB")
    # 像素口径：图宽 1228，x=100 处的网格线偏红
    r, g, _b = img.getpixel((100, 400))
    assert r > g + 15
    # 之后的动作后截图沿用：不用再声明
    res2 = await D.DesktopClickTool().run({"element": "用户名"}, ctx)
    assert "红框标出元素" in res2.output or "页面元素与上一份清单一致" in res2.output
    # 关闭
    await D.DesktopScreenshotTool().run({"marks": False, "grid": False}, ctx)
    assert desktop_shots.get_overlay(ctx) == {"marks": False, "grid": False}


@pytest.mark.asyncio
async def test_marks_only_cover_page_content_when_document_present(core, tmp_path, monkeypatch):
    from app.orchestration.tools import desktop as D

    seen: list[list[str]] = []
    real = D.desktop_overlay.draw_marks

    def spy(path, meta, marks, **kw):
        seen.append([ref for ref, _r in marks])
        return real(path, meta, marks, **kw)

    monkeypatch.setattr(D.desktop_overlay, "draw_marks", spy)
    ctx = _ctx(tmp_path)
    await D.DesktopScreenshotTool().run({"marks": True}, ctx)
    # 网页内容 5 项（含文字「登 录」）；浏览器自身界面 2 项不标
    assert seen == [["e1", "e2", "e3", "e4", "e5"]]


@pytest.mark.asyncio
async def test_keys_repeat_and_sequence_forwarded(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    res = await D.DesktopKeysTool().run({"keys": "TAB", "repeat": 3, "then_shot": False}, ctx)
    assert res.ok, res.error
    assert core.params("keys")["repeat"] == 3
    assert "共按下 3 次" in res.output
    res2 = await D.DesktopKeysTool().run({"keys": "TAB TAB", "then_shot": False}, ctx)
    last_keys = [p for o, p in core.calls if o == "keys"][-1]
    assert last_keys["repeat"] == 1 and "共按下 2 次" in res2.output


@pytest.mark.asyncio
async def test_batch_clicks_elements_by_text_and_marks_every_click(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    await D.DesktopScreenshotTool().run({}, ctx)
    core.calls.clear()
    res = await D.DesktopBatchTool().run({"actions": [
        {"action": "click", "element": "用户名"},
        {"action": "type", "text": "yh002"},
        {"action": "click", "element": "密码"},
        {"action": "type", "text": "pwd"},
        {"action": "click", "element": "登录"},
    ], "handle": 100}, ctx)
    assert res.ok, res.error
    clicks = [p for o, p in core.calls if o == "click"]
    assert [(c["x"], c["y"]) for c in clicks] == [(1329, 664), (1329, 738), (1316, 887)]
    # 下划线开头的内部键不发给内核
    assert all(not any(k.startswith("_") for k in c) for c in clicks)
    # 批量规划时不中途刷新清单（否则同一批里的编号会整体换一遍）
    assert core.ops().count("snapshot") <= 1
    assert "红色圆环" in res.output


@pytest.mark.asyncio
async def test_batch_reports_coordinate_steps_that_hit_non_clickable_text(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    await D.DesktopScreenshotTool().run({"elements": False}, ctx)
    core.hit = {"n": "用户登录", "ct": "Text", "ix": False}
    res = await D.DesktopBatchTool().run({"actions": [{"action": "click", "x": 600, "y": 250}]}, ctx)
    assert res.ok, res.error
    assert "第 1 步点击处是文字「用户登录」" in res.output and "落点标记" in res.output


@pytest.mark.asyncio
async def test_snapshot_tool_lists_same_format_and_honors_interactive_only(core, tmp_path):
    from app.orchestration.tools import desktop as D

    ctx = _ctx(tmp_path)
    res = await D.DesktopSnapshotTool().run({"handle": 100}, ctx)
    assert res.ok, res.error
    assert "共 7 个元素" in res.output and "文字「登 录」" in res.output
    res2 = await D.DesktopSnapshotTool().run({"handle": 100, "interactive_only": True}, ctx)
    assert "文字「登 录」" not in res2.output and "输入框「用户名」" in res2.output


@pytest.mark.asyncio
async def test_chromium_without_document_falls_back_to_cdp_then_reports_empty(core, tmp_path, monkeypatch):
    """UIA 没读到网页内容（无障碍树未就绪）：先试 CDP；CDP 也没有就明说读不到，而不是装作页面是空的。"""
    from app.orchestration.tools import desktop as D

    core.items = [{"n": "最小化", "ct": "Button", "ix": True, "en": True, "d": 0, "r": [2034, 152, 68, 59]}]
    called: list[int] = []

    async def no_cdp(win):
        called.append(1)
        return None

    monkeypatch.setattr(D, "_try_cdp", no_cdp)
    res = await D.DesktopScreenshotTool().run({}, _ctx(tmp_path))
    assert res.ok and called == [1]
    assert "网页元素暂时没读到" not in res.output or "页面元素（1 项）" in res.output


@pytest.mark.asyncio
async def test_windows_tool_warns_when_dpi_awareness_is_wrong(core, tmp_path):
    from app.orchestration.tools import desktop as D

    res = await D.DesktopWindowsTool().run({}, _ctx(tmp_path))
    assert "DPI 感知级别" not in res.output
    core.dpi = "Unaware"
    D._ENV_CACHE.update(at=0.0, info=None)
    res2 = await D.DesktopWindowsTool().run({}, _ctx(tmp_path))
    assert "DPI 感知级别是「Unaware」" in res2.output and "元素编号" in res2.output


def test_prompt_and_tool_descriptions_state_elements_first_and_verification():
    from app.orchestration.prompts.main import DESKTOP_TASK_PROMPT
    from app.orchestration.tools.desktop import (
        DesktopBatchTool, DesktopClickTool, DesktopScreenshotTool, DesktopSnapshotTool)

    assert "Target by element, not by coordinates" in DESKTOP_TASK_PROMPT
    assert "Verify where the click landed" in DESKTOP_TASK_PROMPT
    assert "both axes in the same convention" in DESKTOP_TASK_PROMPT
    for tool in (DesktopClickTool(), DesktopScreenshotTool(), DesktopSnapshotTool(), DesktopBatchTool()):
        assert "element" in tool.description or "元素" in tool.description
    assert "优先 element" in DesktopClickTool.description

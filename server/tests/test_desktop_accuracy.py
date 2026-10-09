"""电脑操控「点准」单测（plan-365-1757）。

覆盖三块全是确定性代码、且一旦回归就会直接表现为「点错位置」的地方：

1. **坐标换算**：图片归一化坐标 / 图片像素 ⇄ 屏幕物理像素（含缩放比、区域原点、越界裁剪）。
   这层过去由模型心算，是误点最大来源；现在必须由代码保证正确。
2. **编号与点前校验**：元素筛选与编号、四种校验结论（proceed / correct / relocate / refuse）
   与风险分级（普通元素纠偏、破坏性操作重新定位或拒绝）。
3. **截图缓存**：命中与复用、TTL 过期、窗口不符失效、编号查表、任务间隔离。

刻意不做内核相关的用例：这些函数不依赖内核，纯函数测试才能稳定、快速、可回归。
"""
from __future__ import annotations

import pytest

from app.core import desktop_marks, desktop_shots
from app.orchestration.tools.base import ToolContext


@pytest.fixture(autouse=True)
def _clean_shot_cache():
    """截图缓存是模块级状态，测试间必须隔离，否则会互相污染编号表。"""
    desktop_shots.clear()
    yield
    desktop_shots.clear()


def _meta(**kw) -> dict:
    """标准截图元数据：2560x1600 屏幕缩到 1600x1000（本机最典型的比例）。"""
    base = {
        "path": "shot.jpg", "srcX": 0, "srcY": 0, "srcW": 2560, "srcH": 1600,
        "width": 1600, "height": 1000, "scale": 0.625,
    }
    base.update(kw)
    return base


def _item(name: str, x: int, y: int, w: int, h: int, *, ct: str = "Button",
          ix: bool = True) -> dict:
    """构造一个与感知通道同构的元素（字段名与 UIA/CDP 归一化结果一致）。"""
    return {
        "n": name, "ct": ct, "cls": "", "en": True, "ix": ix,
        "x": x + w // 2, "y": y + h // 2, "r": [x, y, w, h],
    }


def _ctx(*, session_id: int = 1, task_id: int = 1) -> ToolContext:
    return ToolContext(
        workspace_root=".", session_id=session_id, task_id=task_id,
        agent_id=1, agent_name="t",
    )


# ══════════════════════════════════════════════════════════
# 1. 坐标换算
# ══════════════════════════════════════════════════════════


def test_parse_normalized_center_and_edges():
    m = _meta()
    assert desktop_marks.parse_normalized(m, 0.5, 0.5) == (1280, 800)
    assert desktop_marks.parse_normalized(m, 0.0, 0.0) == (0, 0)
    assert desktop_marks.parse_normalized(m, 1.0, 1.0) == (2560, 1600)
    # 越界必须裁剪到边界，否则一次算错就会把点击送到屏幕外
    assert desktop_marks.parse_normalized(m, 1.5, -0.2) == (2560, 0)


def test_parse_normalized_respects_region_origin():
    m = _meta(srcX=100, srcY=50, srcW=800, srcH=600)
    assert desktop_marks.parse_normalized(m, 0.5, 0.5) == (500, 350)


def test_image_screen_roundtrip():
    """图片像素 ⇄ 屏幕像素往返一致（本机实测样例：图片 886,738 → 屏幕 1418,1181）。"""
    m = _meta()
    assert desktop_marks.image_to_screen(m, 886, 738) == (1418, 1181)
    assert desktop_marks.screen_to_image(m, 1418, 1181) == (886, 738)


def test_scale_missing_or_zero_is_identity():
    """scale 缺失/为 0 时按 1:1 处理——绝不能除零，也不能凭空放大坐标。"""
    for bad in (0, None, "x"):
        m = _meta(scale=bad)
        assert desktop_marks.image_to_screen(m, 100, 50) == (100, 50)


def test_element_rect_and_center_fallbacks():
    assert desktop_marks.element_rect(_item("a", 10, 20, 100, 40)) == (10, 20, 100, 40)
    assert desktop_marks.element_center(_item("a", 10, 20, 100, 40)) == (60, 40)
    # 无矩形时退回感知通道直接给的 x/y
    assert desktop_marks.element_center({"n": "a", "x": 7, "y": 8}) == (7, 8)
    assert desktop_marks.element_rect({"n": "a"}) is None
    assert desktop_marks.element_rect({"n": "a", "r": [1, 2, 0, 5]}) is None


# 跨分辨率适配（用户明确要求「适配各种尺寸的电脑」）：
# 归一化坐标与屏幕尺寸无关——下面这些用例把常见分辨率逐个走一遍，
# 防止将来有人把某一档尺寸（比如 1600 长边）硬编码进换算逻辑。


@pytest.mark.parametrize("src_w,src_h", [
    (1366, 768),    # 笔记本小屏，不触发缩放
    (1920, 1080),   # 常见 1080p
    (2560, 1440),   # 2K
    (2560, 1600),   # 本机 16:10
    (3840, 2160),   # 4K
    (3440, 1440),   # 带鱼屏
])
def test_parse_normalized_across_resolutions(src_w, src_h):
    scale = min(1.0, 1600 / max(src_w, src_h))
    m = _meta(srcW=src_w, srcH=src_h, width=int(src_w * scale), height=int(src_h * scale),
              scale=scale)
    assert desktop_marks.parse_normalized(m, 0.5, 0.5) == (src_w // 2, src_h // 2)
    assert desktop_marks.parse_normalized(m, 0.0, 0.0) == (0, 0)
    assert desktop_marks.parse_normalized(m, 1.0, 1.0) == (src_w, src_h)


def test_parse_normalized_multimonitor_negative_origin():
    """副屏在主屏左侧是常见多显示器布局：左边界是负数，换算必须照样正确。"""
    m = _meta(srcX=-1920, srcY=0, srcW=1920, srcH=1080, scale=1.0)
    assert desktop_marks.parse_normalized(m, 0.0, 0.0) == (-1920, 0)
    assert desktop_marks.parse_normalized(m, 1.0, 1.0) == (0, 1080)


def test_image_to_screen_across_scales():
    # 4K 屏缩到长边 1600：图片右下角必须映射回屏幕右下角
    m = _meta(srcW=3840, srcH=2160, width=1600, height=900, scale=1600 / 3840)
    assert desktop_marks.image_to_screen(m, 1600, 900) == (3840, 2160)
    # 小屏不缩放时，图片坐标与屏幕坐标一一对应
    m2 = _meta(srcW=1366, srcH=768, width=1366, height=768, scale=1.0)
    assert desktop_marks.image_to_screen(m2, 683, 384) == (683, 384)


# ══════════════════════════════════════════════════════════
# 2. 编号（Set-of-Marks）
# ══════════════════════════════════════════════════════════


def test_build_marks_filters_and_orders():
    items = [
        _item("底部按钮", 100, 300, 80, 30),
        _item("顶部按钮", 100, 20, 80, 30),
        _item("碎片", 50, 50, 3, 3),                       # 太小 → 过滤
        {"n": "", "ct": "Pane", "cls": "", "en": True, "ix": False,
         "x": 210, "y": 210, "r": [10, 10, 400, 400]},     # 无名称且不可交互 → 过滤
        _item("屏幕外", 3000, 100, 80, 30),                 # 与截图区域无交集 → 过滤
    ]
    marks = desktop_marks.build_marks(items, _meta())
    assert [m.label for m in marks] == ["顶部按钮", "底部按钮"]
    assert [m.index for m in marks] == [1, 2]
    # 中心点由矩形算出，可直接用于点击
    assert marks[0].center == (140, 35)


def test_build_marks_limit_and_index_map():
    items = [_item(f"项{i}", 10, 10 + i * 40, 100, 30) for i in range(70)]
    marks = desktop_marks.build_marks(items, _meta(), limit=5)
    assert len(marks) == 5
    idx = desktop_marks.mark_index_map(marks)
    assert sorted(idx) == [1, 2, 3, 4, 5]
    assert idx[1]["name"] == "项0"
    assert idx[1]["x"] == 60 and idx[1]["y"] == 25
    assert desktop_marks.format_marks(marks)[0].startswith("#1")


def test_build_marks_empty_when_nothing_usable():
    assert desktop_marks.build_marks([], _meta()) == []
    assert desktop_marks.build_marks([_item("碎片", 0, 0, 2, 2)], _meta()) == []


def test_draw_marks_writes_numbered_copy(tmp_path):
    """编号必须真的画到图上：单测用像素颜色验证，防止「编号字段在、图却没标」的假成功。"""
    PIL = pytest.importorskip("PIL.Image")

    src = tmp_path / "shot.png"
    dst = tmp_path / "shot-mark.png"
    PIL.new("RGB", (400, 300), (255, 255, 255)).save(src)

    meta = {"srcX": 0, "srcY": 0, "srcW": 400, "srcH": 300,
            "width": 400, "height": 300, "scale": 1.0}
    marks = desktop_marks.build_marks([_item("按钮", 50, 60, 120, 40)], meta)
    assert desktop_marks.draw_marks(str(src), str(dst), marks, meta) is True
    assert dst.exists()

    img = PIL.open(dst).convert("RGB")
    try:
        assert img.size == (400, 300)   # 尺寸不变，坐标换算才成立
        colors = img.getcolors(maxcolors=200000) or []
        reds = [c for _n, c in colors if c[0] > 200 and c[1] < 120 and c[2] < 120]
        assert reds, "编号色块没有出现在图上"
    finally:
        img.close()


def test_draw_marks_degrades_without_error(tmp_path):
    """异常输入不得抛异常：标注失败只能返回 False，由调用方退化成「原图 + 无编号」。"""
    assert desktop_marks.draw_marks(
        str(tmp_path / "missing.png"), str(tmp_path / "out.png"), [], _meta()) is False
    marks = desktop_marks.build_marks([_item("按钮", 10, 10, 80, 30)], _meta())
    assert desktop_marks.draw_marks(
        str(tmp_path / "missing.png"), str(tmp_path / "out.png"), marks, _meta()) is False


# ══════════════════════════════════════════════════════════
# 3. 点前校验（分级决策）
# ══════════════════════════════════════════════════════════


def test_decide_click_mode_off_passes_through():
    """关闭档：不校验，原样执行（plan-365-1769 F5 由布尔改为三档）。"""
    items = [_item("按钮", 100, 100, 80, 30)]
    d = desktop_marks.decide_click(items, 5, 5, mode="off")
    assert d.action == "proceed" and (d.x, d.y) == (5, 5)


def test_normalize_verify_mode_migrates_legacy_bool():
    """旧布尔值迁移：true→lenient（宽容但仍有校验）、false→off（尊重明确关闭）。"""
    assert desktop_marks.normalize_verify_mode(True) == "lenient"
    assert desktop_marks.normalize_verify_mode(False) == "off"
    assert desktop_marks.normalize_verify_mode("strict") == "strict"
    assert desktop_marks.normalize_verify_mode("off") == "off"
    # 无法识别的值一律回落 lenient——默认姿态是宽容，不是拦截
    assert desktop_marks.normalize_verify_mode("") == "lenient"
    assert desktop_marks.normalize_verify_mode("bogus") == "lenient"
    assert desktop_marks.normalize_verify_mode(None) == "lenient"


def test_decide_click_corrects_small_offset_to_element_center():
    items = [_item("搜索", 100, 100, 80, 30)]        # 中心 (140, 115)
    d = desktop_marks.decide_click(items, 150, 120, expect="搜索")
    assert d.action == "correct"
    assert (d.x, d.y) == (140, 115)
    assert d.delta == (-10, -5)
    assert d.target is not None


def test_decide_click_proceeds_when_already_on_target():
    items = [_item("搜索", 100, 100, 80, 30)]
    d = desktop_marks.decide_click(items, 140, 115, expect="搜索")
    assert d.action == "proceed" and (d.x, d.y) == (140, 115)


def test_decide_click_lenient_warns_but_executes_when_expect_missing():
    """宽松档（默认）：目标名对不上时只告警不拦截——硬拒绝曾造成操作反复失败。"""
    items = [_item("搜索", 100, 100, 80, 30)]
    d = desktop_marks.decide_click(items, 140, 115, expect="播放")
    assert d.action == "proceed"
    assert any("警告" in n for n in d.notes)


def test_decide_click_strict_refuses_when_expect_not_found():
    """严格档：找不到目标即拒绝（旧行为，保留给需要强约束的场景）。"""
    items = [_item("搜索", 100, 100, 80, 30)]
    d = desktop_marks.decide_click(items, 140, 115, expect="播放", mode="strict")
    assert d.action == "refuse"
    assert "没有找到" in d.reason


def test_decide_click_far_unique_match_differs_by_mode():
    """远距离唯一命中：严格档拒绝（防误点），宽松档仍按它纠正（普通元素直接纠正）。"""
    items = [_item("搜索", 100, 100, 80, 30), _item("别处", 480, 480, 80, 30)]
    strict = desktop_marks.decide_click(
        items, 520, 495, expect="搜索", mode="strict", max_shift=100)
    assert strict.action == "refuse"
    lenient = desktop_marks.decide_click(
        items, 520, 495, expect="搜索", max_shift=100)
    assert lenient.action == "correct"
    assert (lenient.x, lenient.y) == (140, 115)


def test_decide_click_multi_match_inside_proceeds_outside_refuses():
    """多命中且无唯一候选：点位在匹配元素上照点；落在别的元素上则拒绝。"""
    items = [
        _item("播放", 100, 100, 80, 30),
        _item("播放", 300, 100, 80, 30),
        _item("其他", 660, 560, 80, 30),
    ]
    inside = desktop_marks.decide_click(items, 140, 115, expect="播放")
    assert inside.action == "proceed"
    # 点位落在「其他」上（非空白），但不在任何「播放」上 → 无唯一候选，拒绝
    outside = desktop_marks.decide_click(items, 700, 575, expect="播放")
    assert outside.action == "refuse"
    assert "2 个" in outside.reason


def test_decide_click_without_expect_reports_hit():
    items = [_item("关闭", 400, 20, 40, 30)]
    d = desktop_marks.decide_click(items, 420, 35)
    assert d.action == "proceed"
    assert d.target is not None and d.notes and "关闭" in d.notes[0]


def test_decide_click_destructive_relocates_when_unique():
    items = [_item("发送", 200, 300, 90, 34)]        # 中心 (245, 317)
    d = desktop_marks.decide_click(items, 210, 305, expect="发送", destructive=True)
    assert d.action == "relocate"
    assert (d.x, d.y) == (245, 317)
    assert d.moved


def test_decide_click_destructive_without_expect_uses_hit_element():
    items = [_item("发送", 200, 300, 90, 34)]
    d = desktop_marks.decide_click(items, 205, 305, destructive=True)
    assert d.action == "relocate"
    assert (d.x, d.y) == (245, 317)


def test_decide_click_destructive_refuses_when_ambiguous_or_missing():
    dup = [_item("发送", 200, 300, 90, 34), _item("发送", 200, 400, 90, 34)]
    assert desktop_marks.decide_click(
        dup, 245, 317, expect="发送", destructive=True).action == "refuse"
    # 目标压根不在附近：破坏性操作绝不能按原坐标硬点
    lone = [_item("输入框", 800, 800, 200, 40)]
    assert desktop_marks.decide_click(
        lone, 100, 100, expect="发送", destructive=True).action == "refuse"
    assert desktop_marks.decide_click(lone, 100, 100, destructive=True).action == "refuse"


def test_destructive_never_auto_corrects():
    """破坏性操作即使偏差很小也必须走 relocate/refuse，绝不出现 correct。"""
    items = [_item("删除", 200, 300, 90, 34)]
    d = desktop_marks.decide_click(items, 205, 305, expect="删除", destructive=True)
    assert d.action in ("relocate", "refuse")
    assert d.action != "correct"


# ══════════════════════════════════════════════════════════
# 4. 词表与匹配
# ══════════════════════════════════════════════════════════


def test_split_words_handles_cjk_and_dedup():
    raw = "发送, 删除，清空\n卸载,发送"
    assert desktop_marks.split_words(raw) == ["发送", "删除", "清空", "卸载"]
    assert desktop_marks.split_words("") == []
    assert desktop_marks.split_words(None) == []


def test_is_destructive_normalizes_whitespace_and_case():
    words = desktop_marks.split_words("发送,删除,Delete")
    assert desktop_marks.is_destructive("发送消息", words)
    assert desktop_marks.is_destructive("删 除 记录", words)
    assert desktop_marks.is_destructive("delete row", words)
    assert not desktop_marks.is_destructive("接收消息", words)
    assert not desktop_marks.is_destructive("", words)


def test_find_by_label_partial_and_case_insensitive():
    items = [_item("Save As", 0, 0, 80, 30), _item("Cancel", 0, 40, 80, 30)]
    assert len(desktop_marks.find_by_label(items, "save")) == 1
    assert len(desktop_marks.find_by_label(items, "SAVE AS")) == 1
    assert desktop_marks.find_by_label(items, "") == []


# ══════════════════════════════════════════════════════════
# 5. 截图记录与编号表（时效 / 隔离）
# ══════════════════════════════════════════════════════════


def test_shot_cache_store_and_lookup_ref():
    ctx = _ctx()
    rec = desktop_shots.store_shot(
        ctx, path="a.jpg", meta=_meta(), window="记事本",
        marks={1: {"index": 1, "name": "保存", "x": 10, "y": 20, "ct": "Button"}},
    )
    got = desktop_shots.get_shot(ctx)
    assert got is not None and got.shot_id == rec.shot_id
    ref = desktop_shots.lookup_ref(ctx, 1)
    assert ref is not None and ref["name"] == "保存"
    assert desktop_shots.lookup_ref(ctx, 2) is None
    assert desktop_shots.lookup_ref(ctx, 0) is None


def test_shot_record_lives_through_ref_ttl_then_expires():
    """记录在编号时效（300s）内可按 id 取到；超时后被清理。

    plan-365-1773：不再有「1.2 秒新鲜度复用」——记录只为编号/坐标引用而存续。
    """
    ctx = _ctx()
    desktop_shots.store_shot(ctx, path="a.jpg", meta=_meta(), shot_id="abc")
    rec = desktop_shots.get_shot(ctx, "abc")
    assert rec is not None
    rec.created -= 30.0                       # 模型一轮推理往返（30s）后仍可取
    assert desktop_shots.get_shot(ctx, "abc") is not None
    rec.created -= desktop_shots.REF_TTL_S    # 超过编号时效 → 记录被清理
    assert desktop_shots.get_shot(ctx, "abc") is None


def test_shot_window_recorded_but_does_not_block_ref():
    """窗口标题只是现场记录；编号解析不受它影响（plan-365-1773 起无窗口复用判定）。"""
    ctx = _ctx()
    desktop_shots.store_shot(
        ctx, path="a.jpg", meta=_meta(), window="记事本",
        marks={1: {"index": 1, "name": "保存", "x": 1, "y": 1}})
    rec = desktop_shots.get_shot(ctx)
    assert rec is not None and rec.window == "记事本"
    assert desktop_shots.lookup_ref(ctx, 1) is not None


def test_shot_id_mismatch_returns_none():
    ctx = _ctx()
    desktop_shots.store_shot(ctx, path="a.jpg", meta=_meta(), shot_id="abc")
    assert desktop_shots.get_shot(ctx, "abc") is not None
    assert desktop_shots.get_shot(ctx, "zzz") is None


def test_shot_cache_isolated_per_task():
    a, b = _ctx(task_id=1), _ctx(task_id=2)
    desktop_shots.store_shot(
        a, path="a.jpg", meta=_meta(), marks={1: {"name": "A", "x": 1, "y": 1}})
    desktop_shots.store_shot(
        b, path="b.jpg", meta=_meta(), marks={1: {"name": "B", "x": 2, "y": 2}})
    assert desktop_shots.lookup_ref(a, 1)["name"] == "A"
    assert desktop_shots.lookup_ref(b, 1)["name"] == "B"


def test_update_marks_targets_named_shot_only():
    """编号写入只落在指定代次；跨代次检索可命中旧编号，但必须带 stale 标记。"""
    ctx = _ctx()
    desktop_shots.store_shot(ctx, path="a.jpg", meta=_meta(), shot_id="abc")
    desktop_shots.update_marks(ctx, {3: {"name": "C", "x": 3, "y": 3}}, shot_id="abc")
    hit = desktop_shots.lookup_ref(ctx, 3)
    assert hit["name"] == "C" and hit["stale"] is False

    # 新截图入历史后：旧代次编号仍可解析（plan-365-1769 F2），但必须标记 stale——
    # 由调用方按元素名重新定位后再点，这就是「跨推理往返可用、但不会照旧点」的契约。
    desktop_shots.store_shot(ctx, path="b.jpg", meta=_meta(), shot_id="def")
    old = desktop_shots.lookup_ref(ctx, 3)
    assert old is not None and old["stale"] is True and old["shot_id"] == "abc"

    # update_marks 指向不存在的代次：不得写入任何记录
    desktop_shots.update_marks(ctx, {9: {"name": "X", "x": 9, "y": 9}}, shot_id="zzz")
    assert desktop_shots.lookup_ref(ctx, 9) is None


def test_meta_with_marks_and_describe():
    ctx = _ctx()
    rec = desktop_shots.store_shot(
        ctx, path="a.jpg", meta=_meta(), marks={1: {"name": "A", "x": 1, "y": 1}})
    merged = rec.meta_with_marks()
    assert merged["shot_id"] == rec.shot_id and merged["mark_count"] == 1
    assert "截图" in desktop_shots.describe(rec)
    assert desktop_shots.describe(None) == "无可用截图"


def test_latest_with_hash_reuses_only_same_screen_with_marks():
    """同画面沿用编号（plan-365-1774）：只有「内容哈希一致且有编号」的截图才能被沿用。

    依据是逐字节相同的像素 ⇒ 元素位置与编号仍然有效；哈希不同或没有编号的
    截图一律不得沿用，否则模型会拿着旧编号去点新界面。
    """
    ctx = _ctx()
    desktop_shots.store_shot(
        ctx, path="a.jpg", meta=_meta(content_hash="h1"),
        marks={1: {"index": 1, "name": "保存", "x": 1, "y": 1}},
        mark_path="a-mark.jpg")
    desktop_shots.store_shot(ctx, path="b.jpg", meta=_meta(content_hash="h2"))

    hit = desktop_shots.latest_with_hash(ctx, "h1")
    assert hit is not None and hit.mark_path == "a-mark.jpg"
    # 没有编号的截图无从点起；空哈希/未命中一律返回 None
    assert desktop_shots.latest_with_hash(ctx, "h2") is None
    assert desktop_shots.latest_with_hash(ctx, "") is None
    assert desktop_shots.latest_with_hash(ctx, "nope") is None


def test_shot_record_carries_items_for_reuse():
    """点前校验复用靠的是截图登记的元素集合：没登记的不能当素材用。"""
    ctx = _ctx()
    desktop_shots.store_shot(ctx, path="a.jpg", meta=_meta())
    assert desktop_shots.get_shot(ctx).items == []
    item = _item("保存", 0, 0, 80, 30)
    desktop_shots.store_shot(ctx, path="b.jpg", meta=_meta(), items=[item])
    rec = desktop_shots.get_shot(ctx)
    assert len(rec.items) == 1 and rec.items[0]["n"] == "保存"


# ══════════════════════════════════════════════════════════
# 5b. 画面近似判定与编号沿用（plan-365-1774 提速主路径）
# ══════════════════════════════════════════════════════════


def test_signature_diff_tolerates_small_changes():
    """指纹比较语义：微小变化（时钟/光标）算同一画面，真实界面变化必须被识别。

    这是「重复查看同一画面沿用编号」的判定核心——阈值太松会拿旧编号点新界面，
    太紧则永远命中不了（桌面截图总在微变），两种错误都要挡住。
    """
    base = bytes([100] * (64 * 40))
    assert desktop_shots.signature_diff(base, base) == (0.0, 0.0)

    # 少量像素微变（模拟任务栏时钟、鼠标光标）：平均差与变化占比都很小
    minor = bytearray(base)
    for i in range(20):
        minor[(i * 37) % len(base)] = 140
    mean, changed = desktop_shots.signature_diff(base, bytes(minor))
    assert mean < 3.0 and changed < 0.06, "微小变化应判为同一画面"

    # 大面积变化（切页/弹窗）：必须超过阈值，不得误判为同一画面
    major = bytes([b + 90 if b < 165 else b - 90 for b in base])
    mean2, changed2 = desktop_shots.signature_diff(base, major)
    assert mean2 > 3.0 or changed2 > 0.06, "界面真变了必须能被识别"

    # 长度不一致（不同尺寸指纹）视为完全不同，避免错配
    assert desktop_shots.signature_diff(base, base[:100]) == (255.0, 1.0)
    assert desktop_shots.signature_diff(b"", base) == (255.0, 1.0)


def test_latest_similar_reuses_numbering_for_same_screen():
    """画面基本相同的连续截图沿用编号；窗口不符或无编号时不得沿用。"""
    ctx = _ctx()
    sig = bytes([120] * (64 * 40))
    desktop_shots.store_shot(
        ctx, path="a.jpg", meta=_meta(content_hash="h1"), signature=sig,
        marks={1: {"index": 1, "name": "保存", "x": 1, "y": 1}},
        mark_path="a-mark.jpg", window="记事本")

    minor = bytearray(sig)
    for i in range(10):
        minor[(i * 53) % len(sig)] = 150
    hit = desktop_shots.latest_similar(ctx, bytes(minor), window="记事本")
    assert hit is not None and hit.path == "a.jpg", "同画面应命中"

    # 目标窗口不符：坐标系与内容都不同，不得沿用
    assert desktop_shots.latest_similar(ctx, bytes(minor), window="浏览器") is None

    # 画面大改：不得沿用
    major = bytes([b + 90 if b < 165 else b - 90 for b in sig])
    assert desktop_shots.latest_similar(ctx, major, window="记事本") is None

    # 没有编号的截图无从沿用（最新一张无 marks，旧一张画面不同）
    desktop_shots.store_shot(ctx, path="b.jpg", meta=_meta(), signature=major)
    assert desktop_shots.latest_similar(ctx, major, window="记事本") is None


def test_screen_signature_shape_and_noise_tolerance(tmp_path):
    """指纹生成：64x40 灰度字节；同内容一致，内容变化可区分；读不到文件返回空。"""
    from PIL import Image

    from app.orchestration.tools.desktop import _screen_signature

    white = tmp_path / "a.png"
    Image.new("RGB", (320, 200), (255, 255, 255)).save(white)
    s1 = _screen_signature(str(white))
    assert len(s1) == 64 * 40, "指纹应为固定尺寸灰度缩略图"

    white2 = tmp_path / "b.png"
    Image.new("RGB", (320, 200), (255, 255, 255)).save(white2)
    assert _screen_signature(str(white2)) == s1, "同内容的两个文件指纹应一致"

    half_black = Image.new("RGB", (320, 200), (255, 255, 255))
    for x in range(160):
        for y in range(200):
            half_black.putpixel((x, y), (0, 0, 0))
    dark = tmp_path / "c.png"
    half_black.save(dark)
    mean, changed = desktop_shots.signature_diff(s1, _screen_signature(str(dark)))
    assert mean > 3.0 or changed > 0.06, "半屏变化必须被指纹识别"

    # 读不到文件 / 非图片：返回空指纹（调用方退回精确哈希判定）
    assert _screen_signature(str(tmp_path / "nope.png")) == b""
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    assert _screen_signature(str(bad)) == b""


# ══════════════════════════════════════════════════════════
# 6. 编号生命周期与截图失效（plan-365-1769 F2/F4 回归）
# ══════════════════════════════════════════════════════════


def test_ref_ttl_covers_model_reasoning_round_trip():
    """核心契约：编号时效 300s——编号必须熬过模型推理往返（数秒~数十秒）。

    历史故障：编号与「截图新鲜度」共用 1.2 秒，而模型「截图 → 推理 → 点击」要数秒到数十秒，
    编号必然失效、失败文案又引导重截，形成死循环，模型退回手算坐标 → 点错
    （plan-365-1769 R2）。plan-365-1773 移除复用后编号时效保持不变。
    """
    ctx = _ctx()
    desktop_shots.store_shot(
        ctx, path="a.jpg", meta=_meta(),
        marks={7: {"index": 7, "name": "保存", "x": 10, "y": 20}})
    rec = desktop_shots.get_shot(ctx)
    assert rec is not None
    rec.created -= 30.0   # 模拟 30 秒前的截图（模型看完后过了一轮推理）

    ref = desktop_shots.lookup_ref(ctx, 7)             # 编号仍可解析
    assert ref is not None and ref["name"] == "保存" and ref["stale"] is False
    assert desktop_shots.get_shot(ctx) is not None     # 记录仍在（可继续按 id 引用）


def test_mark_stale_marks_ref_for_relocation():
    """写动作后：编号仍可解析但带 stale 标记——点击侧按元素名重定位而不是照旧坐标点。"""
    ctx = _ctx()
    desktop_shots.store_shot(
        ctx, path="a.jpg", meta=_meta(),
        marks={1: {"index": 1, "name": "确定", "x": 5, "y": 6}})
    ref = desktop_shots.lookup_ref(ctx, 1)
    assert ref is not None and ref["stale"] is False
    desktop_shots.mark_stale(ctx)
    ref = desktop_shots.lookup_ref(ctx, 1)
    assert ref is not None and ref["stale"] is True


def test_lookup_ref_prefers_newest_generation():
    """同编号出现在多代次时，优先返回最新一代（不误标 stale）。"""
    ctx = _ctx()
    desktop_shots.store_shot(
        ctx, path="a.jpg", meta=_meta(), shot_id="old",
        marks={1: {"index": 1, "name": "旧", "x": 1, "y": 1}})
    desktop_shots.store_shot(
        ctx, path="b.jpg", meta=_meta(), shot_id="new",
        marks={1: {"index": 1, "name": "新", "x": 2, "y": 2}})
    ref = desktop_shots.lookup_ref(ctx, 1)
    assert ref["name"] == "新" and ref["stale"] is False and ref["shot_id"] == "new"


def test_history_prunes_beyond_generations():
    """历史只保留最近 MAX_HISTORY 代：更老的编号不得再被解析出来。"""
    ctx = _ctx()
    for i in range(desktop_shots.MAX_HISTORY + 2):
        desktop_shots.store_shot(
            ctx, path=f"{i}.jpg", meta=_meta(), shot_id=f"s{i}",
            marks={i + 1: {"index": i + 1, "name": f"E{i}", "x": i, "y": i}})
    assert desktop_shots.lookup_ref(ctx, 1) is None           # 最早的编号已被挤出历史
    newest = desktop_shots.MAX_HISTORY + 1                     # 最新一代的编号可解析
    assert desktop_shots.lookup_ref(ctx, newest) is not None


# ══════════════════════════════════════════════════════════
# 7. 校验档细则（plan-365-1769 F5 回归）
# ══════════════════════════════════════════════════════════


def test_decide_click_blank_point_refused_unless_allowed():
    """空白点位：默认拒绝（坐标算错时立刻可诊断）；allow_blank 时才放行。"""
    items = [_item("搜索", 100, 100, 80, 30)]
    refused = desktop_marks.decide_click(items, 900, 900)
    assert refused.action == "refuse"
    assert "没有识别到元素" in refused.reason
    allowed = desktop_marks.decide_click(items, 900, 900, allow_blank=True)
    assert allowed.action == "proceed"


def test_find_by_label_matches_both_directions():
    """双向匹配：expect 比元素名长也能命中（模型常写「收费按钮」而元素名是「收费」）。"""
    items = [_item("收费", 100, 100, 80, 30), _item("发药", 300, 100, 80, 30)]
    assert len(desktop_marks.find_by_label(items, "收费按钮")) == 1
    assert len(desktop_marks.find_by_label(items, "收费")) == 1
    # 极短元素名不参与反向匹配，避免 "a" ⊂ "save" 式噪声命中
    short = [_item("a", 100, 100, 80, 30)]
    assert desktop_marks.find_by_label(short, "save") == []


def test_destructive_stays_strict_in_lenient_mode():
    """破坏性操作不受宽松档影响：三档下都必须是 relocate / refuse，绝不 proceed 到模糊目标。"""
    items = [_item("删除", 200, 300, 90, 34)]
    d = desktop_marks.decide_click(items, 205, 305, expect="删除", destructive=True)
    assert d.action in ("relocate", "refuse")
    # 未提供 expect 且点位无元素（空白）：破坏性操作必须拒绝
    blank = desktop_marks.decide_click([], 500, 500, destructive=True, allow_blank=True)
    assert blank.action == "refuse"


# ══════════════════════════════════════════════════════════
# 8. 批量输入参数透传（plan-365-1774 提速：多次往返合并成一次调用）
# ══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_click_forwards_batch_input_params(monkeypatch):
    """「点击 → 清空 → 输入 → 回车」的参数必须原样传到内核（一步完成整段操作）。

    提速的主刀是减少模型往返：地址栏输入这类操作若拆成三次调用，
    代价是三次模型往返；参数一旦在透传中丢失，模型只能退回多步走。
    """
    from app.core.config import settings
    from app.orchestration.tools import desktop as D

    captured: list[tuple[str, dict]] = []

    async def fake_call(op, params=None):
        captured.append((op, dict(params or {})))
        if op == "windows":
            return {"items": [], "count": 0}
        if op == "click":
            return {"clicked": True, "at": [100, 200], "target": "记事本",
                    "typed": 17, "keysError": ""}
        return {}

    async def fake_elements(self, ctx, win, *, fresh=False):
        return []

    monkeypatch.setattr(D, "_call", fake_call)
    monkeypatch.setattr(D.DesktopClickTool, "_elements", fake_elements)
    monkeypatch.setattr(settings, "desktop_enabled", True)
    monkeypatch.setattr(settings, "desktop_plain_ops_enabled", True)

    ctx = _ctx()
    desktop_shots.store_shot(
        ctx, path="a.jpg", meta=_meta(), shot_id="s1",
        marks={1: {"index": 1, "name": "地址和搜索栏", "x": 100, "y": 200}})

    res = await D.DesktopClickTool().run({
        "ref": 1, "text": "https://grok.com", "keys": "ENTER",
        "clear_first": True, "allow_blank": True, "title": "记事本",
    }, ctx)

    assert res.ok, res.error
    click_params = next(p for op, p in captured if op == "click")
    assert click_params["text"] == "https://grok.com"
    assert click_params["keys"] == "ENTER"
    assert click_params["clearFirst"] is True, "clear_first 未透传，地址栏覆盖式输入会失败"


@pytest.mark.asyncio
async def test_type_forwards_keys_and_clear_first(monkeypatch):
    """输入 + 提交（keys）也必须一次调用完成：模型不该为「回车」再跑一轮。"""
    from app.core.config import settings
    from app.orchestration.tools import desktop as D

    captured: list[tuple[str, dict]] = []

    async def fake_call(op, params=None):
        captured.append((op, dict(params or {})))
        if op == "type":
            return {"typed": 5, "target": "记事本", "focusedNow": False, "keysError": ""}
        return {}

    monkeypatch.setattr(D, "_call", fake_call)
    monkeypatch.setattr(settings, "desktop_enabled", True)
    monkeypatch.setattr(settings, "desktop_plain_ops_enabled", True)

    res = await D.DesktopTypeTool().run({
        "text": "hello", "keys": "ENTER", "clear_first": True,
    }, _ctx())

    assert res.ok, res.error
    type_params = next(p for op, p in captured if op == "type")
    assert type_params["text"] == "hello"
    assert type_params["keys"] == "ENTER", "keys 未透传，输入后无法一步提交"
    assert type_params["clearFirst"] is True

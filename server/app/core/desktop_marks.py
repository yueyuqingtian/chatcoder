"""截图元素编号与点前校验（plan-365-1757 A2/A3）。

**为什么需要这个模块**：此前「看图点击」把几何换算交给模型——工具结果里写着
「屏幕坐标 = 图片坐标 / 缩放比」，由模型自己算，再回报一串像素坐标；点下去之前，
没有任何一步核对那个坐标上到底是什么元素。结果就是：模型算错一次，或视觉定位偏几十像素，
点击就落到相邻控件上，而且点到之后没人发现。

业界验证最有效的两条对症手段都在这里落地：

1. **元素编号（Set-of-Marks）**：把可交互元素框出来并编号，模型只需要说「点 7 号」。
   微软 OmniParser 实测把图标正确标记率从 70.5% 提到 93.8%——编号把「估计坐标」变成
   「选择符号」，而坐标由确定性代码给出。
2. **点前校验**：执行前用同一份元素集合核对目标，普通元素纠偏到元素中心；
   破坏性操作不纠偏、改为重新定位确证，确证不了就拒绝。

本模块只做纯计算与图像标注，不接触内核、不发点击，因此可以完整单测。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# 一张截图最多标多少个元素。太多编号会糊满整屏、反而干扰模型。
MAX_MARKS = 60
# 点前校验的搜索半径（屏幕像素）：落在这个范围内的元素视为「附近」。
CORRECT_RADIUS_PX = 24
# 自动纠偏的最大位移（屏幕像素）。超过这个距离：strict 档拒绝；
# lenient 档仍按唯一命中的元素纠正——「普通元素直接纠正」是本产品的既定口径。
MAX_CORRECT_SHIFT_PX = 250
# 参与编号/校验的元素最小边长：1–2px 的碎片多是布局占位或装饰，编号只会产生噪声。
MIN_ELEMENT_SIZE = 6

# ── 点前校验档位（plan-365-1769 F5）──
# off     ：不校验（破坏性操作仍严格）；
# lenient ：默认——「找不到目标」告警放行，仅「空白点位」或「明显冲突且无唯一候选」时拒绝；
# strict  ：找不到目标即拒绝（plan-365-1757 的旧行为，保留给需要强约束的场景）。
VERIFY_MODE_OFF = "off"
VERIFY_MODE_LENIENT = "lenient"
VERIFY_MODE_STRICT = "strict"


@dataclass
class Mark:
    """一个被编号的元素。"""

    index: int                                  # 从 1 开始的编号（模型用它点名）
    name: str                                   # 元素名称（可能为空）
    ctype: str                                  # 控件类型（Button/Edit/Hyperlink…）
    rect: tuple[int, int, int, int]             # 屏幕矩形 x,y,w,h
    center: tuple[int, int]                     # 屏幕中心点

    @property
    def label(self) -> str:
        return self.name or "(无名称)"


@dataclass
class ClickDecision:
    """点前校验的结论。纯数据，便于在工具层直接落成结果文本与字段。"""

    action: str                                 # proceed / correct / relocate / refuse
    x: int
    y: int
    reason: str = ""
    target: dict[str, Any] | None = None
    delta: tuple[int, int] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def moved(self) -> bool:
        return self.action in ("correct", "relocate")


# ── 坐标换算（图片空间 ⇄ 屏幕空间）────────────────────────────────
#
# 截图元数据（shot / zoom 的返回）字段：
#   srcX/srcY 截图区域在屏幕上的原点，srcW/srcH 源区域尺寸，scale = 输出长边 / 源长边。
# 图片坐标 ix → 屏幕坐标 = srcX + ix / scale（缩放比是「输出/源」，所以是除法）。
# 这层换算过去由模型心算，是点错位置的确定性来源之一（plan-365-1757 R1）。


def _scale_of(meta: dict[str, Any]) -> float:
    try:
        s = float(meta.get("scale") or 0)
    except (TypeError, ValueError):
        s = 0.0
    return s if s > 0 else 1.0


def image_to_screen(meta: dict[str, Any], ix: float, iy: float) -> tuple[int, int]:
    """图片像素 → 屏幕物理像素。"""
    scale = _scale_of(meta)
    ox = int(meta.get("srcX") or 0)
    oy = int(meta.get("srcY") or 0)
    return (ox + int(round(ix / scale)), oy + int(round(iy / scale)))


def screen_to_image(meta: dict[str, Any], sx: float, sy: float) -> tuple[int, int]:
    """屏幕物理像素 → 图片像素。"""
    scale = _scale_of(meta)
    ox = int(meta.get("srcX") or 0)
    oy = int(meta.get("srcY") or 0)
    return (int(round((sx - ox) * scale)), int(round((sy - oy) * scale)))


def parse_normalized(meta: dict[str, Any], nx: float, ny: float) -> tuple[int, int]:
    """归一化坐标（0–1，相对图片）→ 屏幕物理像素。

    这是给模型用的坐标契约：模型只需按图片比例报小数，换算全部在代码里完成，
    不存在「除以 0.625 算错」这种问题（Anthropic 官方也要求由 harness 回算坐标）。
    """
    src_w = float(meta.get("srcW") or 0)
    src_h = float(meta.get("srcH") or 0)
    ox = float(meta.get("srcX") or 0)
    oy = float(meta.get("srcY") or 0)
    if src_w <= 0 or src_h <= 0:
        return (int(round(nx)), int(round(ny)))
    cx = max(0.0, min(1.0, float(nx)))
    cy = max(0.0, min(1.0, float(ny)))
    return (int(round(ox + cx * src_w)), int(round(oy + cy * src_h)))


def element_rect(item: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """取元素的屏幕矩形 (x, y, w, h)；缺矩形时返回 None。"""
    r = item.get("r")
    if isinstance(r, (list, tuple)) and len(r) >= 4:
        try:
            x, y, w, h = (int(r[0]), int(r[1]), int(r[2]), int(r[3]))
        except (TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)
    return None


def element_center(item: dict[str, Any]) -> tuple[int, int] | None:
    """取元素中心点：优先用矩形算，退化到感知通道直接给的 x/y。"""
    rect = element_rect(item)
    if rect is not None:
        x, y, w, h = rect
        return (x + w // 2, y + h // 2)
    try:
        return (int(item.get("x") or 0), int(item.get("y") or 0))
    except (TypeError, ValueError):
        return None


def element_name(item: dict[str, Any]) -> str:
    return str(item.get("n") or item.get("name") or "")


# ── 编号（A2）────────────────────────────────────────────────────


def build_marks(
    items: Sequence[dict[str, Any]],
    meta: dict[str, Any],
    *,
    limit: int = MAX_MARKS,
) -> list[Mark]:
    """把感知到的元素筛成一份可编号清单。

    筛选口径（都为了「模型一眼能对上」）：
    - 必须有可用矩形，且边长不小于 MIN_ELEMENT_SIZE（滤掉布局碎片）；
    - 必须与截图区域有交集（不在画面里的元素编号没有意义）；
    - 可交互或有名称（纯容器没有可点性，编号只会误导）；
    - 按 (上, 左) 排序后编号：编号顺序与阅读顺序一致，模型更容易把编号和画面位置对上。
    """
    src_x = int(meta.get("srcX") or 0)
    src_y = int(meta.get("srcY") or 0)
    src_w = int(meta.get("srcW") or 0)
    src_h = int(meta.get("srcH") or 0)
    region = None
    if src_w > 0 and src_h > 0:
        region = (src_x, src_y, src_x + src_w, src_y + src_h)

    picked: list[tuple[tuple[int, int], dict[str, Any], tuple[int, int, int, int]]] = []
    seen: set[tuple[str, int, int]] = set()
    for it in items:
        rect = element_rect(it)
        if rect is None:
            continue
        if rect[2] < MIN_ELEMENT_SIZE or rect[3] < MIN_ELEMENT_SIZE:
            continue
        if region is not None:
            if rect[0] + rect[2] <= region[0] or rect[0] >= region[2]:
                continue
            if rect[1] + rect[3] <= region[1] or rect[1] >= region[3]:
                continue
        interactive = bool(it.get("ix"))
        name = element_name(it)
        if not interactive and not name:
            continue
        key = (name, rect[0], rect[1])
        if key in seen:
            continue
        seen.add(key)
        picked.append(((rect[1], rect[0]), it, rect))

    picked.sort(key=lambda t: t[0])
    marks: list[Mark] = []
    for idx, (_pos, it, rect) in enumerate(picked[: max(1, limit)], start=1):
        marks.append(Mark(
            index=idx,
            name=element_name(it),
            ctype=str(it.get("ct") or ""),
            rect=rect,
            center=(rect[0] + rect[2] // 2, rect[1] + rect[3] // 2),
        ))
    return marks


def format_marks(marks: Sequence[Mark], meta: dict[str, Any] | None = None) -> list[str]:
    """把编号清单渲染成给模型看的多行文本。"""
    lines: list[str] = []
    for m in marks:
        parts = [f"#{m.index}", m.label]
        if m.ctype:
            parts.append(f"[{m.ctype}]")
        lines.append("  ".join(parts))
    return lines


def mark_index_map(marks: Sequence[Mark], meta: dict[str, Any] | None = None) -> dict[int, dict[str, Any]]:
    """编号 → 可执行目标（屏幕坐标 + 展示信息）。

    用可序列化的普通字典保存，便于挂在工具结果里跨调用传递（模型下次给 ref=7 时直接查表）。
    """
    out: dict[int, dict[str, Any]] = {}
    for m in marks:
        out[m.index] = {
            "index": m.index,
            "name": m.label,
            "ct": m.ctype,
            "x": m.center[0],
            "y": m.center[1],
            "rect": list(m.rect),
        }
    return out


# ── 图像标注 ─────────────────────────────────────────────────────
#
# 编号画在元素左上角并做半透明底色：既不遮住元素本身的内容，又能让模型把
# 「#7」与画面位置直接对应起来。标注失败一律不影响主流程——调用方拿到 False 时
# 退化成「原图 + 编号清单」，能力不丢。

_MARK_COLOR = (235, 64, 52)
_MARK_TEXT_COLOR = (255, 255, 255)

# 对外暴露编号配色：单测需要验证「编号真的画上去了」，靠像素颜色断言最直接。
MARK_COLOR = _MARK_COLOR
MARK_TEXT_COLOR = _MARK_TEXT_COLOR


def _load_font(size: int):
    """优先用系统中文字体，取不到就退回 PIL 内置位图字体（仅影响观感，不影响编号语义）。"""
    from PIL import ImageFont

    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001 - 字体缺失属正常情况，逐个尝试
            continue
    try:
        return ImageFont.load_default()
    except Exception:  # noqa: BLE001
        return None


def draw_marks(
    src_path: str,
    dst_path: str,
    marks: Sequence[Mark],
    meta: dict[str, Any],
    *,
    font_size: int = 16,
) -> bool:
    """把编号画到源截图的副本上。成功返回 True，任何异常都返回 False（不抛出）。"""
    if not marks:
        return False
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # noqa: BLE001 - Pillow 缺失时优雅降级
        logger.info("[desktop-marks] Pillow 不可用，跳过编号绘制：%s", exc)
        return False

    try:
        img = Image.open(src_path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.info("[desktop-marks] 打开截图失败：%s", exc)
        return False

    try:
        draw = ImageDraw.Draw(img, "RGBA")
        font = _load_font(font_size)
        w_img, h_img = img.size
        for m in marks:
            x0, y0 = screen_to_image(meta, m.rect[0], m.rect[1])
            x1, y1 = screen_to_image(meta, m.rect[0] + m.rect[2], m.rect[1] + m.rect[3])
            x0, x1 = sorted((max(0, min(w_img - 1, x0)), max(0, min(w_img - 1, x1))))
            y0, y1 = sorted((max(0, min(h_img - 1, y0)), max(0, min(h_img - 1, y1))))
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue
            draw.rectangle([x0, y0, x1, y1], outline=_MARK_COLOR, width=2)

            text = f"#{m.index}"
            try:
                box = draw.textbbox((0, 0), text, font=font)
                tw, th = box[2] - box[0], box[3] - box[1]
            except Exception:  # noqa: BLE001 - 老版本 Pillow 无 textbbox
                tw, th = 8 * len(text), font_size

            pad = 3
            # 标签放在矩形左上角外侧；贴边时翻到内侧，保证不被裁掉。
            bx0 = x0
            by0 = y0 - th - 2 * pad
            if by0 < 0:
                by0 = min(y0 + 2, max(0, h_img - th - 2 * pad))
            bx1 = min(w_img - 1, bx0 + tw + 2 * pad)
            by1 = min(h_img - 1, by0 + th + 2 * pad)
            draw.rectangle([bx0, by0, bx1, by1], fill=(*_MARK_COLOR, 235))
            if font is not None:
                draw.text((bx0 + pad, by0 + pad - 1), text, fill=_MARK_TEXT_COLOR, font=font)

        Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
        # JPEG 体积小、模型读编号足够；编号用纯色块，压缩带来的边缘噪点不影响识别。
        if dst_path.lower().endswith((".jpg", ".jpeg")):
            img.save(dst_path, quality=85)
        else:
            img.save(dst_path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("[desktop-marks] 编号绘制失败：%s", exc)
        return False
    finally:
        try:
            img.close()
        except Exception:  # noqa: BLE001
            pass


# ── 点前校验（A3）────────────────────────────────────────────────


def _distance_to_rect(rect: tuple[int, int, int, int], x: int, y: int) -> float:
    rx, ry, rw, rh = rect
    dx = max(rx - x, 0, x - (rx + rw))
    dy = max(ry - y, 0, y - (ry + rh))
    return (dx * dx + dy * dy) ** 0.5


def point_in_rect(rect: tuple[int, int, int, int], x: int, y: int) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x <= rx + rw and ry <= y <= ry + rh


def nearest_element(
    items: Sequence[dict[str, Any]], x: int, y: int, radius: int = CORRECT_RADIUS_PX,
) -> dict[str, Any] | None:
    """取该点处（或半径内最近）的元素。点在矩形内优先于「距离更近但很小」的元素。"""
    inside: list[tuple[int, dict[str, Any]]] = []
    nearby: list[tuple[float, dict[str, Any]]] = []
    for it in items:
        rect = element_rect(it)
        if rect is None:
            continue
        if point_in_rect(rect, x, y):
            inside.append((rect[2] * rect[3], it))
        elif _distance_to_rect(rect, x, y) <= radius:
            nearby.append((_distance_to_rect(rect, x, y), it))
    if inside:
        # 多个包含点（嵌套容器）时，取面积最小的那个——那才是真正的控件。
        inside.sort(key=lambda t: t[0])
        return inside[0][1]
    if nearby:
        nearby.sort(key=lambda t: t[0])
        return nearby[0][1]
    return None


def _normalize_text(text: str) -> str:
    return "".join(str(text or "").split()).lower()


def _label_matches(name: str, want: str) -> bool:
    """名称匹配（两侧均已归一化）：正向包含，或反向包含（元素名足够长时）。

    反向包含消除「模型给的期望值比元素名长」造成的误拒（元素名「收费」、
    模型写「收费按钮」）；但元素名很短时反向会引入噪声（"a" ⊂ "save"），
    因此要求元素名至少 2 个字符才允许反向匹配。
    """
    if not name or not want:
        return False
    if want in name:
        return True
    return len(name) >= 2 and name in want


def normalize_verify_mode(raw: Any) -> str:
    """校验档归一化（plan-365-1769 F5）：兼容旧布尔值。

    true → lenient（旧「开启」的意图是「有校验」，新默认档最贴近）；
    false → off（用户明确关过，尊重其选择）；
    无法识别的值一律回落 lenient——校验的默认姿态是宽容，不是拦截。
    """
    if isinstance(raw, bool):
        return VERIFY_MODE_LENIENT if raw else VERIFY_MODE_OFF
    s = str(raw or "").strip().lower()
    if s in (VERIFY_MODE_OFF, VERIFY_MODE_LENIENT, VERIFY_MODE_STRICT):
        return s
    if s in ("false", "0", "no", "none", "null"):
        return VERIFY_MODE_OFF
    return VERIFY_MODE_LENIENT


def find_by_label(items: Sequence[dict[str, Any]], expect: str) -> list[dict[str, Any]]:
    """按名称/文字找元素（忽略大小写与空白，双向包含匹配，plan-365-1769 F5）。"""
    want = _normalize_text(expect)
    if not want:
        return []
    hits: list[dict[str, Any]] = []
    for it in items:
        name = _normalize_text(element_name(it))
        alt = _normalize_text(str(it.get("val") or ""))
        if _label_matches(name, want) or _label_matches(alt, want):
            hits.append(it)
    return hits


def is_destructive(text: str, words: Iterable[str]) -> bool:
    """目标名称是否命中破坏性词表（用于选择「重新定位确证」而非自动纠偏）。"""
    hay = _normalize_text(text)
    if not hay:
        return False
    for w in words or ():
        needle = _normalize_text(w)
        if needle and needle in hay:
            return True
    return False


def split_words(raw: str | None) -> list[str]:
    """把逗号/换行分隔的词表拆成列表（设置页用户可以自由编辑）。"""
    if not raw:
        return []
    out: list[str] = []
    for chunk in str(raw).replace("\n", ",").replace("，", ",").split(","):
        w = chunk.strip()
        if w and w not in out:
            out.append(w)
    return out


def decide_click(
    items: Sequence[dict[str, Any]],
    x: int,
    y: int,
    *,
    expect: str = "",
    destructive: bool = False,
    mode: str = VERIFY_MODE_LENIENT,
    allow_blank: bool = False,
    radius: int = CORRECT_RADIUS_PX,
    max_shift: int = MAX_CORRECT_SHIFT_PX,
) -> ClickDecision:
    """点前校验决策（纯函数；plan-365-1757 A3 起，plan-365-1769 F5 改分档）。

    档位语义（mode，经 normalize_verify_mode 归一化）：
    - off     ：不校验，原样执行（破坏性操作除外——它始终严格）；
    - lenient ：默认档——「找不到目标」告警放行；仅「点位空白」或「与目标明显冲突
                且无唯一候选」时拒绝，尽量不把操作卡死（此前无条件拒绝是
                「操作反复失败」的主要来源之一）；
    - strict  ：找不到目标即拒绝（旧行为，保留给需要强约束的场景）。

    三档共同规则：
    - 破坏性操作始终严格：不做自动纠偏，按名称重新定位确证后执行，确证不了就拒绝；
    - 普通元素唯一命中「就直接纠正到元素中心」（lenient）；strict 档对超出
      max_shift 的远距离位移仍拒绝；未命中目标在 lenient 档只告警不拦截。
    """
    mode = normalize_verify_mode(mode)
    if mode == VERIFY_MODE_OFF and not destructive:
        return ClickDecision("proceed", x, y, "未启用点击前校验")

    hit = nearest_element(items, x, y, radius=radius)
    hit_name = element_name(hit) if hit is not None else ""
    notes: list[str] = []
    if hit is not None:
        notes.append(f"该位置是「{hit_name or '(无名称)'}」")
    else:
        notes.append("该位置未识别到元素（可能是空白或自绘区域）")

    # ── 破坏性操作：始终严格（与档位无关）──
    if destructive:
        cands = find_by_label(items, expect) if expect else []
        if len(cands) == 1:
            center = element_center(cands[0])
            if center is not None:
                cx, cy = center
                shift = abs(cx - x) + abs(cy - y)
                if shift <= max_shift:
                    return ClickDecision(
                        "relocate", cx, cy,
                        f"破坏性操作：已按「{element_name(cands[0]) or expect}」重新定位后执行",
                        cands[0], (cx - x, cy - y), notes,
                    )
                return ClickDecision(
                    "refuse", x, y,
                    f"破坏性操作：目标「{expect}」距离给定位置过远（{shift}px），请确认真实目标",
                    cands[0], None, notes,
                )
        if len(cands) > 1:
            return ClickDecision(
                "refuse", x, y,
                f"破坏性操作：匹配到 {len(cands)} 个「{expect}」，需确认具体目标后重试",
                None, None, notes,
            )
        if not expect and hit is not None:
            center = element_center(hit)
            if center is not None:
                cx, cy = center
                return ClickDecision(
                    "relocate", cx, cy,
                    f"破坏性操作：已按该位置实际元素「{hit_name or '(无名称)'}」确证后执行",
                    hit, (cx - x, cy - y), notes,
                )
        return ClickDecision(
            "refuse", x, y,
            "破坏性操作：无法确证目标元素，已拒绝执行（请给出 expect 或改用 ref/text_target）",
            hit, None, notes,
        )

    # ── 点位空白：三档一致拒绝（自绘界面的无名称图标/画布用 allow_blank 放行）──
    # 口径说明：坐标算错时，「这里什么都没有」是立刻可诊断的反馈；
    # 默默点空比拒绝更糟——模型会以为操作生效，继续往下走。
    if hit is None and not allow_blank:
        return ClickDecision(
            "refuse", x, y,
            "该位置没有识别到元素（空白或自绘区域）。请改用 ref/text_target 指定目标；"
            "确需点击无名称图标或空白处时，设 allow_blank=true",
            None, None, notes,
        )

    # ── 无期望值：原样执行，只回报该处实际是什么元素 ──
    if not expect:
        return ClickDecision("proceed", x, y, "", hit, None, notes)

    # ── 有期望值 ──
    cands = find_by_label(items, expect)
    if len(cands) == 1:
        center = element_center(cands[0])
        if center is None:
            return ClickDecision("proceed", x, y, "", cands[0], None, notes)
        cx, cy = center
        shift = abs(cx - x) + abs(cy - y)
        if shift == 0:
            return ClickDecision("proceed", x, y, "", cands[0], None, notes)
        if shift > max_shift and mode == VERIFY_MODE_STRICT:
            return ClickDecision(
                "refuse", x, y,
                f"「{expect}」距离给定位置 {shift}px，已拒绝执行以免点错目标",
                cands[0], None, notes,
            )
        label = element_name(cands[0]) or expect
        reason = (f"已纠偏到「{label}」中心" if shift <= max_shift
                  else f"「{label}」在全屏唯一命中但相距 {shift}px，已按它纠正")
        return ClickDecision(
            "correct", cx, cy, reason, cands[0], (cx - x, cy - y), notes,
        )
    if len(cands) > 1:
        # 多命中：若点位本就落在**某个匹配元素**上，说明模型已在正确位置，照点；
        # 否则属于「与目标明显冲突且无唯一候选」→ 拒绝（交给 ref 精确指定）。
        # 注意判据必须是 cands（匹配集合）而非 hit（最近元素）——后者会把
        # 「点在毫不相干的其他元素上」误判成「已经在目标上」。
        for cand in cands:
            rect = element_rect(cand)
            if rect is not None and point_in_rect(rect, x, y):
                return ClickDecision("proceed", x, y, "", cand, None, notes)
        return ClickDecision(
            "refuse", x, y,
            f"匹配到 {len(cands)} 个「{expect}」（无唯一候选），请改用 ref 指定编号后重试",
            None, None, notes,
        )

    # 零命中但点位落在别的具名元素上：严格档拒绝；宽松档告警放行（不把操作卡死）
    if mode == VERIFY_MODE_STRICT:
        return ClickDecision(
            "refuse", x, y,
            f"该位置附近没有找到「{expect}」，已拒绝执行",
            hit, None, notes,
        )
    notes.append(f"警告：附近没有找到「{expect}」，已按给定坐标执行（宽松档不拦截）")
    return ClickDecision(
        "proceed", x, y, f"未找到「{expect}」但宽松档放行", hit, None, notes,
    )

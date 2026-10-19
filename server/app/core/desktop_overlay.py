"""截图叠加层（plan-20261019-desktop-targeting）：点击落点标记、元素编号框、参考网格。

三者解决同一个问题——**让模型「看见」坐标与位置，而不是靠心算**：

1. **点击落点标记**：动作后的新截图上，在上一次点击的位置画一个醒目的圆环 + 十字。模型此前点完只收到
   「已点击」三个字，点偏了也不知道（实录里连点 20 多次都落在标题文字上，它一直以为点中了输入框）；
   现在偏了一眼可见，且标记画在图上的位置与它给的坐标同口径，错在哪一轴立刻能对出来。
2. **元素编号框**（marks，按需）：把清单里的可交互元素框出来、标上 e 编号。编号把「估计坐标」变成「选择符号」，
   业界实测（微软 OmniParser 等）对不擅长精确定位的模型提升最大。元素来自无障碍树（精确），不是识图猜的。
3. **参考网格**（grid，按需）：每 100 个坐标单位画一条淡线并在边缘标数值。没有元素可用的自绘界面里，
   模型可以对着图上印出来的数字读位置，不必凭空估一个像素值。

只在内存里画图、写回原文件，不改变图片尺寸，因此不影响「截图坐标 → 屏幕坐标」的换算。
任何绘制失败都返回 False 并保留原图——叠加层是增强，不能让截图本身失败。
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# 配色：高饱和红 + 白描边，在深色 / 浅色界面上都醒目
CLICK_COLOR = (255, 45, 85)
MARK_COLOR = (255, 45, 85)
GRID_COLOR = (255, 0, 80)


def _font(size: int):
    from PIL import ImageFont

    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001 — 字体缺失属正常情况，逐个尝试
            continue
    try:
        return ImageFont.load_default()
    except Exception:  # noqa: BLE001
        return None


def screen_to_image(meta: dict[str, Any], sx: float, sy: float) -> tuple[float, float]:
    """屏幕物理像素 → 图片像素（浮点，不取整）。"""
    src_w = float(meta.get("srcW") or 0) or 1.0
    src_h = float(meta.get("srcH") or 0) or 1.0
    w = float(meta.get("width") or 0) or src_w
    h = float(meta.get("height") or 0) or src_h
    return ((float(sx) - int(meta.get("srcX") or 0)) * w / src_w,
            (float(sy) - int(meta.get("srcY") or 0)) * h / src_h)


def _text_size(draw, text: str, font) -> tuple[int, int]:
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0], box[3] - box[1]
    except Exception:  # noqa: BLE001 — 老版本 Pillow 无 textbbox
        return 8 * len(text), 14


def _save(img, path: str, quality: int) -> None:
    if path.lower().endswith((".jpg", ".jpeg")):
        img.save(path, quality=max(60, min(100, int(quality or 92))), subsampling=0)
    else:
        img.save(path)


def _open(path: str):
    from PIL import Image

    return Image.open(path).convert("RGB")


# ── 点击落点标记 ─────────────────────────────────────────────────


def draw_clicks(
    path: str, meta: dict[str, Any], points: Sequence[tuple[int, int, str]], *, quality: int = 92,
) -> bool:
    """在 path 指向的截图上标出一批点击位置。points = [(屏幕 x, 屏幕 y, 标签)]，标签为空时多点按序号编号。

    落在图外的点忽略；全部忽略时不改文件。成功画了至少一个返回 True。
    """
    if not points:
        return False
    try:
        from PIL import ImageDraw

        img = _open(path)
    except Exception as exc:  # noqa: BLE001
        logger.info("[desktop-overlay] 打开截图失败：%s", exc)
        return False
    try:
        w_img, h_img = img.size
        draw = ImageDraw.Draw(img)
        font = _font(14)
        drawn = 0
        for i, (sx, sy, label) in enumerate(points, start=1):
            ix, iy = screen_to_image(meta, sx, sy)
            if not (-2 <= ix <= w_img + 2 and -2 <= iy <= h_img + 2):
                continue
            r, arm = 15, 9
            # 白色外描边 + 红色圆环 + 十字（中心留空，不盖住被点的元素）
            for color, width in (((255, 255, 255), 5), (CLICK_COLOR, 3)):
                draw.ellipse([ix - r, iy - r, ix + r, iy + r], outline=color, width=width)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    draw.line([ix + dx * (r - 3), iy + dy * (r - 3), ix + dx * (r + arm), iy + dy * (r + arm)],
                              fill=color, width=width)
            text = label or (str(i) if len(points) > 1 else "")
            if text:
                tw, th = _text_size(draw, text, font)
                bx, by = ix + r + 2, iy - r - th - 6
                bx = max(0, min(w_img - tw - 8, bx))
                by = max(0, min(h_img - th - 8, by))
                draw.rectangle([bx, by, bx + tw + 6, by + th + 6], fill=CLICK_COLOR)
                if font is not None:
                    draw.text((bx + 3, by + 2), text, fill=(255, 255, 255), font=font)
            drawn += 1
        if drawn:
            _save(img, path, quality)
        return drawn > 0
    except Exception as exc:  # noqa: BLE001
        logger.info("[desktop-overlay] 点击标记绘制失败：%s", exc)
        return False
    finally:
        try:
            img.close()
        except Exception:  # noqa: BLE001
            pass


# ── 元素编号框 ───────────────────────────────────────────────────


def draw_marks(
    path: str, meta: dict[str, Any], marks: Sequence[tuple[str, tuple[int, int, int, int]]],
    *, quality: int = 92,
) -> bool:
    """把元素编号框画到截图上。marks = [(编号, 屏幕矩形 x,y,w,h)]；标签放在框左上角，互相压住时错开。"""
    if not marks:
        return False
    try:
        from PIL import ImageDraw

        img = _open(path)
    except Exception as exc:  # noqa: BLE001
        logger.info("[desktop-overlay] 打开截图失败：%s", exc)
        return False
    try:
        w_img, h_img = img.size
        draw = ImageDraw.Draw(img, "RGBA")
        font = _font(13)
        placed: list[tuple[int, int, int, int]] = []
        drawn = 0
        for ref, (rx, ry, rw, rh) in marks:
            x0, y0 = screen_to_image(meta, rx, ry)
            x1, y1 = screen_to_image(meta, rx + rw, ry + rh)
            x0, x1 = sorted((max(0.0, min(w_img - 1.0, x0)), max(0.0, min(w_img - 1.0, x1))))
            y0, y1 = sorted((max(0.0, min(h_img - 1.0, y0)), max(0.0, min(h_img - 1.0, y1))))
            if x1 - x0 < 3 or y1 - y0 < 3:
                continue
            draw.rectangle([x0, y0, x1, y1], outline=(*MARK_COLOR, 255), width=2)
            tw, th = _text_size(draw, ref, font)
            lw, lh = tw + 6, th + 5
            # 候选位置：框左上角外侧 → 框内左上角 → 框内左下角
            cands = [(x0, y0 - lh), (x0, y0), (x0, y1 - lh)]
            pos = None
            for cx, cy in cands:
                cx = max(0, min(w_img - lw, cx))
                cy = max(0, min(h_img - lh, cy))
                box = (int(cx), int(cy), int(cx + lw), int(cy + lh))
                if all(box[2] <= p[0] or box[0] >= p[2] or box[3] <= p[1] or box[1] >= p[3] for p in placed):
                    pos = box
                    break
            if pos is None:
                pos = (int(max(0, min(w_img - lw, x0))), int(max(0, min(h_img - lh, y0 - lh))),
                       int(max(0, min(w_img - lw, x0)) + lw), int(max(0, min(h_img - lh, y0 - lh)) + lh))
            placed.append(pos)
            draw.rectangle(list(pos), fill=(*MARK_COLOR, 235))
            if font is not None:
                draw.text((pos[0] + 3, pos[1] + 1), ref, fill=(255, 255, 255), font=font)
            drawn += 1
        if drawn:
            _save(img, path, quality)
        return drawn > 0
    except Exception as exc:  # noqa: BLE001
        logger.info("[desktop-overlay] 编号框绘制失败：%s", exc)
        return False
    finally:
        try:
            img.close()
        except Exception:  # noqa: BLE001
            pass


# ── 参考网格 ─────────────────────────────────────────────────────


def draw_grid(path: str, meta: dict[str, Any], mode: str, *, quality: int = 92) -> bool:
    """画参考网格并在上边缘 / 左边缘标数值，数值与模型本次应给的坐标同口径。

    mode = norm1000：每 100 单位一条线（标 100…900）；mode = pixel：每 100 图上像素一条线。
    线条半透明、不遮挡内容；标签是红底白字的小块，只放在边缘。
    """
    try:
        from PIL import ImageDraw

        img = _open(path)
    except Exception as exc:  # noqa: BLE001
        logger.info("[desktop-overlay] 打开截图失败：%s", exc)
        return False
    try:
        w_img, h_img = img.size
        draw = ImageDraw.Draw(img, "RGBA")
        font = _font(12)
        line = (*GRID_COLOR, 70)
        norm = mode == "norm1000"
        xs = [(i * 100, i * 100 / 1000.0 * w_img) for i in range(1, 10)] if norm else \
             [(v, float(v)) for v in range(100, w_img, 100)]
        ys = [(i * 100, i * 100 / 1000.0 * h_img) for i in range(1, 10)] if norm else \
             [(v, float(v)) for v in range(100, h_img, 100)]
        for val, px in xs:
            draw.line([px, 0, px, h_img], fill=line, width=1)
            _label(draw, font, str(val), px + 2, 1, w_img, h_img)
        for val, py in ys:
            draw.line([0, py, w_img, py], fill=line, width=1)
            _label(draw, font, str(val), 1, py + 1, w_img, h_img)
        _save(img, path, quality)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("[desktop-overlay] 网格绘制失败：%s", exc)
        return False
    finally:
        try:
            img.close()
        except Exception:  # noqa: BLE001
            pass


def _label(draw, font, text: str, x: float, y: float, w_img: int, h_img: int) -> None:
    tw, th = _text_size(draw, text, font)
    x = max(0, min(w_img - tw - 5, x))
    y = max(0, min(h_img - th - 4, y))
    draw.rectangle([x, y, x + tw + 4, y + th + 3], fill=(*GRID_COLOR, 200))
    if font is not None:
        draw.text((x + 2, y), text, fill=(255, 255, 255), font=font)

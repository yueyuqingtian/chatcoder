"""电脑操控的坐标口径与换算（plan-20261019-desktop-precision）。

**一个坐标系**：模型给出的所有坐标都取自「最近一次截图」，换算回屏幕物理像素全部由这里完成，
模型不接触屏幕坐标（此前截图结果里同时出现图片坐标与屏幕坐标，两套口径混用是点错的来源之一）。

**口径跟随模型**：同类产品的做法一致——Anthropic / OpenAI 的 computer use 让模型报截图像素；
Gemini Computer Use、字节 Midscene.js 的模型适配层则表明 DeepSeek、Qwen3、GLM、豆包 / UI-TARS、
Gemini 等原生使用 0–1000 归一化坐标（Midscene 默认即 0–1000，只有 GPT 与 Qwen2.5-VL 用像素，
Kimi 用 0–1 小数）。强迫原生 0–1000 的模型报像素，等于让它心算换算。

**容差**（模型偶尔不按口径给值时，能判断的就按其真实意图换算，并在结果里说明）：
- 任何口径下 x、y 都在 0~1 且带小数 → 按比例理解（Kimi 等的 0–1 习惯）；
- 0–1000 口径下出现大于 1000、但落在图片尺寸内的值 → 按图片像素理解；
- 其余越界值一律报错，不猜。
"""
from __future__ import annotations

import math
import re
from typing import Any

PIXEL = "pixel"
NORM1000 = "norm1000"
MODES = (PIXEL, NORM1000)

# 原生按截图像素报坐标的模型族：Anthropic、OpenAI（含 o 系列与 computer-use 专用模型）、Qwen2.5-VL。
# 其余（含未知模型）走 0–1000，与 Midscene 默认口径一致。
_PIXEL_RE = re.compile(
    r"claude|anthropic|opus|sonnet|haiku|fable"
    r"|gpt|openai|chatgpt|codex|computer-use"
    r"|(?:^|[^a-z0-9])o[134](?:$|[^a-z0-9])"
    r"|qwen2(?:\.5)?-?vl"
)

# 模型实际看到的图片上限：多数视觉模型服务端会把超限的图再缩小（Anthropic 长边约 1568、
# 约 1.15 MP；OpenAI 高清模式短边 768）。像素口径的模型是在缩小后的图上读坐标的，
# 截图必须先缩到这个尺寸以内，模型读到的像素才与换算口径一致。
MODEL_LONG_MAX = 1568
MODEL_SHORT_MAX = 768
MODEL_MAX_PIXELS = 1_150_000


def coord_mode_for(model_name: str | None, override: str | None = "auto") -> str:
    """按模型名选择坐标口径；override 为 pixel / norm1000 时直接采用（隐藏配置 desktop_coord_mode）。"""
    forced = str(override or "").strip().lower()
    if forced in MODES:
        return forced
    name = str(model_name or "").strip().lower()
    if name and _PIXEL_RE.search(name):
        return PIXEL
    return NORM1000


def model_safe_max_dim(src_w: int, src_h: int, requested: int = 0) -> int:
    """按源区域尺寸给出传给内核的 maxDim（长边像素，0 = 不缩放）。

    同时满足：长边 ≤ 1568、短边 ≤ 768、总像素 ≤ 1.15 MP；显式设置更小时用设置值。
    plan-20261019-desktop-precision 补上总像素上限：细长窗口按旧规则会得到 768×1536（1.18 MP），
    仍会被服务端二次缩放，像素口径的模型因此整体偏移。
    """
    long_edge, short_edge = max(src_w, src_h), min(src_w, src_h)
    if long_edge <= 0 or short_edge <= 0:
        return requested if requested > 0 else MODEL_LONG_MAX
    aspect = long_edge / short_edge
    limit = min(
        MODEL_LONG_MAX,
        int(long_edge * MODEL_SHORT_MAX / short_edge),
        int(math.sqrt(MODEL_MAX_PIXELS * aspect)),
    )
    if requested > 0:
        return min(requested, limit)
    return limit if long_edge > limit else 0


def _num(v: Any) -> float:
    if isinstance(v, bool):
        raise ValueError("坐标必须是数字")
    return float(v)


def _is_fraction(x: float, y: float) -> bool:
    """两个值都在 0~1 且至少一个带小数：按比例理解（像素 0/1 这种整数不算）。"""
    return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and (x != int(x) or y != int(y))


class CoordError(ValueError):
    """坐标无法换算（越界 / 非数字）。消息面向模型，说明口径与正确取值范围。"""


def to_screen(meta: dict[str, Any], x: Any, y: Any, mode: str) -> tuple[int, int, str]:
    """把模型在截图上给出的坐标换算为屏幕物理像素，返回 (sx, sy, 说明)。

    meta：截图元数据（srcX/srcY 截图区域在屏幕上的原点，srcW/srcH 源区域尺寸，width/height 图片尺寸）。
    说明为空表示按口径正常换算；非空时是容差换算的提示，应附在工具结果里。
    """
    try:
        fx, fy = _num(x), _num(y)
    except (TypeError, ValueError) as exc:
        raise CoordError("x / y 必须是数字") from exc
    w = float(meta.get("width") or 0)
    h = float(meta.get("height") or 0)
    src_w = float(meta.get("srcW") or 0)
    src_h = float(meta.get("srcH") or 0)
    if w <= 0 or h <= 0 or src_w <= 0 or src_h <= 0:
        raise CoordError("最近一次截图缺少尺寸信息，请重新 desktop_screenshot")
    note = ""
    if _is_fraction(fx, fy):
        rx, ry = fx, fy
        note = "坐标是 0~1 的小数，已按比例换算"
    elif mode == NORM1000:
        if 0 <= fx <= 1000 and 0 <= fy <= 1000:
            rx, ry = fx / 1000.0, fy / 1000.0
        elif 0 <= fx <= w and 0 <= fy <= h:
            rx, ry = fx / w, fy / h
            note = f"坐标超出 0–1000，已按图片像素（{int(w)}×{int(h)}）理解；请按 0–1000 比例给坐标"
        else:
            raise CoordError(
                f"坐标 ({fx:g},{fy:g}) 超出范围：本次截图的坐标口径是 0–1000 比例"
                "（左上角 0,0、右下角 1000,1000），请按目标在图中的相对位置给值")
    else:
        if 0 <= fx <= w and 0 <= fy <= h:
            rx, ry = fx / w, fy / h
        else:
            raise CoordError(
                f"坐标 ({fx:g},{fy:g}) 超出截图范围 {int(w)}×{int(h)}：本次截图的坐标口径是图上像素，"
                "请填你在图上量到的位置")
    ox = int(meta.get("srcX") or 0)
    oy = int(meta.get("srcY") or 0)
    sx = ox + min(int(src_w) - 1, max(0, int(round(rx * src_w))))
    sy = oy + min(int(src_h) - 1, max(0, int(round(ry * src_h))))
    return sx, sy, note


def from_screen(meta: dict[str, Any], sx: float, sy: float, mode: str) -> tuple[int, int]:
    """屏幕物理像素 → 模型口径的截图坐标（用于在结果里回显位置）。"""
    src_w = float(meta.get("srcW") or 0) or 1.0
    src_h = float(meta.get("srcH") or 0) or 1.0
    rx = (float(sx) - int(meta.get("srcX") or 0)) / src_w
    ry = (float(sy) - int(meta.get("srcY") or 0)) / src_h
    if mode == NORM1000:
        return int(round(rx * 1000)), int(round(ry * 1000))
    return int(round(rx * float(meta.get("width") or 0))), int(round(ry * float(meta.get("height") or 0)))


def parse_region(meta: dict[str, Any], raw: Any, mode: str) -> tuple[int, int, int, int]:
    """解析截图坐标系下的矩形 "x0,y0,x1,y1"（或四元数组）为屏幕矩形 (x, y, w, h)。"""
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = [p.strip() for p in str(raw or "").replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise CoordError('区域格式应为 "x0,y0,x1,y1"（左上角与右下角，取自最近一次截图）')
    x0, y0, _ = to_screen(meta, parts[0], parts[1], mode)
    x1, y1, _ = to_screen(meta, parts[2], parts[3], mode)
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    return left, top, max(1, right - left), max(1, bottom - top)


def convention_line(mode: str, width: int, height: int) -> str:
    """截图结果里的「坐标口径」行：模型据此给坐标。"""
    if mode == PIXEL:
        return (f"坐标口径：图上像素——x/y 填你在这张图上量到的位置（图宽 {width}、高 {height}，"
                "左上角为 0,0）。换算由工具完成，不要自己换算成屏幕坐标。")
    return ("坐标口径：0–1000 比例——x/y 填目标在这张图中的相对位置（左上角 0,0、右下角 1000,1000，"
            "与图片像素尺寸无关）。换算由工具完成。")

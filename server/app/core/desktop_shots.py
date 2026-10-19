"""任务级坐标系与元素编号表（plan-20261019-desktop-precision）。

两类状态，按 (session_id, task_id) 隔离——主代理与子代理各持一份，互不干扰：

1. **坐标系（Frame）**：最近一次截图（desktop_screenshot、动作后自动截图、批量动作结尾截图）的
   换算元数据。模型给出的所有坐标都按它换算回屏幕；放大查看只读，不改变坐标系——与
   Anthropic / Claude computer use 的约定一致。此前放大图也会成为新的坐标参照，模型在两张图之间
   换来换去是点偏的来源之一。
2. **元素编号（e1…eN）**：desktop_snapshot 读到的 UIA / CDP 元素。按编号点击直接取元素中心，
   不经过任何坐标换算；写动作后编号标记为过期，再点时由调用方按名称在当前界面重新定位。

取代 plan-365-1757 起的截图编号表（Set-of-Mark）、同画面沿用编号与 OCR 元素复用。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# 坐标系 / 元素编号的有效期（秒）：覆盖模型一次较长的推理；task_id 即 turn_id，跨轮天然失效
FRAME_TTL_S = 900.0
ELEMENT_TTL_S = 900.0
# 「最近一次点击位置」的有效期：输入 / 按键时据此判断局部画面有没有变化
LAST_POINT_TTL_S = 60.0


@dataclass
class Frame:
    """一次截图的坐标系：模型坐标 → 屏幕物理像素的全部依据。"""

    shot_id: str
    path: str
    meta: dict[str, Any]
    mode: str                     # 截图时告诉模型的坐标口径（pixel / norm1000）
    window: str = ""              # 目标窗口标题（整屏截图为空）
    handle: int = 0               # 目标窗口句柄（整屏截图为 0）
    region_source: str = "full"   # full = 整屏 / window = 目标窗口
    created: float = field(default_factory=time.monotonic)

    def age(self) -> float:
        return max(0.0, time.monotonic() - self.created)

    def region(self) -> tuple[int, int, int, int]:
        m = self.meta
        return (int(m.get("srcX") or 0), int(m.get("srcY") or 0),
                int(m.get("srcW") or 0), int(m.get("srcH") or 0))


@dataclass
class ElementTable:
    """一次 desktop_snapshot 的元素编号表。"""

    items: dict[str, dict[str, Any]]
    handle: int = 0
    title: str = ""
    channel: str = ""
    created: float = field(default_factory=time.monotonic)
    stale: bool = False

    def age(self) -> float:
        return max(0.0, time.monotonic() - self.created)


_lock = threading.Lock()
_frames: dict[tuple[int, int], Frame] = {}
_elements: dict[tuple[int, int], ElementTable] = {}
_last_points: dict[tuple[int, int], tuple[int, int, float]] = {}


def _key(ctx: Any) -> tuple[int, int]:
    return (int(getattr(ctx, "session_id", 0) or 0), int(getattr(ctx, "task_id", 0) or 0))


def new_shot_id() -> str:
    """短 id：够区分同名文件，也不至于占满结果文本。"""
    return uuid.uuid4().hex[:8]


def store_frame(
    ctx: Any, *, path: str, meta: dict[str, Any], mode: str,
    window: str = "", handle: int = 0, region_source: str = "full", shot_id: str = "",
) -> Frame:
    """登记新的坐标系（覆盖上一张）。"""
    frame = Frame(
        shot_id=shot_id or new_shot_id(), path=str(path or ""), meta=dict(meta or {}),
        mode=mode, window=str(window or ""), handle=int(handle or 0), region_source=region_source,
    )
    with _lock:
        _frames[_key(ctx)] = frame
    return frame


def get_frame(ctx: Any) -> Frame | None:
    """当前坐标系；超过有效期视为没有（必须重新截图）。"""
    with _lock:
        frame = _frames.get(_key(ctx))
        if frame is not None and frame.age() > FRAME_TTL_S:
            _frames.pop(_key(ctx), None)
            return None
        return frame


def store_elements(
    ctx: Any, items: list[dict[str, Any]], *, handle: int = 0, title: str = "", channel: str = "",
) -> dict[str, dict[str, Any]]:
    """按顺序给元素编号 e1…eN 并登记（覆盖上一份），返回 编号 → 元素。"""
    mapping = {f"e{i}": dict(it) for i, it in enumerate(items, start=1)}
    with _lock:
        _elements[_key(ctx)] = ElementTable(
            items=mapping, handle=int(handle or 0), title=str(title or ""), channel=channel)
    return mapping


def lookup_element(ctx: Any, ref: str) -> tuple[dict[str, Any], ElementTable] | None:
    """编号 → (元素, 所在编号表)；编号不存在或已超期返回 None。stale 由调用方判断是否需要重新定位。"""
    ref = str(ref or "").strip().lower()
    if ref and not ref.startswith("e") and ref.isdigit():
        ref = "e" + ref
    with _lock:
        table = _elements.get(_key(ctx))
        if table is None or table.age() > ELEMENT_TTL_S:
            return None
        item = table.items.get(ref)
        if item is None:
            return None
        return dict(item), table


def mark_stale(ctx: Any) -> None:
    """写动作后元素编号过期：界面可能已变，再点时需要按名称重新定位。坐标系不受影响。"""
    with _lock:
        table = _elements.get(_key(ctx))
        if table is not None:
            table.stale = True


def note_point(ctx: Any, x: int, y: int) -> None:
    """记下最近一次点击的屏幕位置：随后的输入 / 按键据此观察局部画面变化。"""
    with _lock:
        _last_points[_key(ctx)] = (int(x), int(y), time.monotonic())


def last_point(ctx: Any) -> tuple[int, int] | None:
    with _lock:
        hit = _last_points.get(_key(ctx))
    if hit is None or time.monotonic() - hit[2] > LAST_POINT_TTL_S:
        return None
    return hit[0], hit[1]


def clear(ctx: Any | None = None) -> None:
    """清空状态（ctx 为空时清全部）——测试与任务结束清理用。"""
    with _lock:
        if ctx is None:
            _frames.clear()
            _elements.clear()
            _last_points.clear()
            return
        key = _key(ctx)
        _frames.pop(key, None)
        _elements.pop(key, None)
        _last_points.pop(key, None)

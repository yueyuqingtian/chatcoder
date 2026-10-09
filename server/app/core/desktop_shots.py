"""任务级截图编号表（plan-365-1757 A1/A2；plan-365-1769 F2/F4；plan-365-1773 去掉复用）。

**存在的两个理由**：

1. 编号点击需要一份「编号 → 元素」的查表：#7 是什么，只有截那一刻才知道；
2. 动作后界面可能已经变了，动作前的编号不能直接照点，需要按元素名二次确认（stale 标记）。

**不再提供「截图复用」**（plan-365-1773 移除）：旧实现允许 1.2 秒内的旧图被当作
「当前画面」直接返回，也允许 find_text / 点前校验共用这张旧图。实测这个窗口足以让模型
看到操作之前的画面、按旧图上的坐标点击——表现为「点偏」「以为没生效而重复操作」。
现在每次截图与文字定位都取当前画面：截图约 110 ms 的成本，远低于一次误点。

**编号时效**：`REF_TTL_S`（300 秒）覆盖模型一次推理往返；编号绑定「任务最近几代截图」：
命中最新一代可直接点击，命中较旧代次（或最新截图已被写动作标记失效）则交由调用方
**按元素名在当前画面重新定位**。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 编号时效：编号/图片坐标引用的有效期（秒）——覆盖模型一次推理往返
REF_TTL_S = 300.0
# 每任务保留的截图代数：编号可跨代次按名复用；再老的一律丢弃
MAX_HISTORY = 3


@dataclass
class ShotRecord:
    """一次截图的完整现场：图片、换算元数据、编号表、元素集合、关联窗口。"""

    shot_id: str
    path: str
    meta: dict[str, Any] = field(default_factory=dict)
    marks: dict[int, dict[str, Any]] = field(default_factory=dict)
    mark_path: str = ""          # 叠加编号后的图片路径（模型看到的就是它）
    window: str = ""             # 关联窗口标题：窗口变了就不该复用
    # 该次感知到的元素集合（含矩形）：点前校验直接用它，不必为一次校验再跑一遍感知
    items: list[dict[str, Any]] = field(default_factory=list)
    # 画面相略指纹（灰度缩略图字节，plan-365-1774）：用于判定「这次看到的画面
    # 与上一次是否基本相同」。桌面截图总在微变（时钟、光标、压缩伪影），
    # 逐字节比较几乎永不命中；灰度缩略图比较能容忍这些噪声。
    signature: bytes = b""
    created: float = field(default_factory=time.monotonic)
    # 写动作执行后置位（plan-365-1769 F4）：不可再当「当前画面」返回；
    # 编号仍可解析，但调用方需按元素名二次确认后重新定位。
    stale: bool = False

    def age(self) -> float:
        return max(0.0, time.monotonic() - self.created)

    def meta_with_marks(self) -> dict[str, Any]:
        """换算元数据 + 本次编号表，供工具层一次性传给模型与后续调用。"""
        out = dict(self.meta)
        out["shot_id"] = self.shot_id
        out["mark_count"] = len(self.marks)
        return out


_lock = threading.Lock()
# 键：(session_id, task_id) → 截图历史（[0] 为最新）。子代理与主代理各持一份，互不干扰。
_cache: dict[tuple[int, int], list[ShotRecord]] = {}


def _key(ctx: Any) -> tuple[int, int]:
    return (int(getattr(ctx, "session_id", 0) or 0), int(getattr(ctx, "task_id", 0) or 0))


def new_shot_id() -> str:
    """短 id：够区分同名文件，也不至于占满结果文本。"""
    return uuid.uuid4().hex[:8]


def _prune(records: list[ShotRecord]) -> list[ShotRecord]:
    """丢弃超期与超代数记录。调用方必须已持有 _lock（或确认无并发）。"""
    now = time.monotonic()
    keep = [r for r in records if now - r.created <= REF_TTL_S][:MAX_HISTORY]
    return keep


def store_shot(
    ctx: Any,
    *,
    path: str,
    meta: dict[str, Any],
    marks: dict[int, dict[str, Any]] | None = None,
    mark_path: str = "",
    window: str = "",
    shot_id: str = "",
    items: list[dict[str, Any]] | None = None,
    signature: bytes = b"",
) -> ShotRecord:
    """登记一次截图（压入该任务的截图历史，最新在前）。"""
    rec = ShotRecord(
        shot_id=shot_id or new_shot_id(),
        path=str(path or ""),
        meta=dict(meta or {}),
        marks=dict(marks or {}),
        mark_path=str(mark_path or ""),
        window=str(window or ""),
        items=list(items or []),
        signature=bytes(signature or b""),
    )
    with _lock:
        history = _cache.get(_key(ctx), [])
        history = _prune([rec, *history])
        _cache[_key(ctx)] = history
    return rec


def _history(ctx: Any) -> list[ShotRecord]:
    key = _key(ctx)
    with _lock:
        keep = _prune(_cache.get(key, []))
        if keep:
            _cache[key] = keep
        else:
            _cache.pop(key, None)
        return list(keep)


def get_shot(ctx: Any, shot_id: str = "") -> ShotRecord | None:
    """取截图：给了 id 就在历史里找（供显式引用旧 shot_id 的场景）；否则取最新一张。

    注意：本函数只按「还在编号有效期内」检索，不做新鲜度判断——
    截图实时性由调用方保证（plan-365-1773 起每次截图都取当前画面）。
    """
    history = _history(ctx)
    if not history:
        return None
    if not shot_id:
        return history[0]
    for rec in history:
        if rec.shot_id == shot_id:
            return rec
    return None


def latest_with_hash(ctx: Any, digest: str) -> ShotRecord | None:
    """按画面内容摘要找最近的同画面截图（plan-365-1774 的「同画面沿用编号」用）。

    摘要由工具层对截图文件算 MD5 得到（存在 meta["content_hash"]）。逐字节相同的画面
    ⇒ 元素位置、编号全部仍然有效，可以直接沿用上一张的编号，省掉元素采集与画编号。
    """
    if not digest:
        return None
    for rec in _history(ctx):
        if str(rec.meta.get("content_hash") or "") == digest and rec.marks:
            return rec
    return None


def latest_similar(
    ctx: Any, signature: bytes, *, window: str = "", max_age: float = 30.0,
    max_mean_diff: float = 3.0, max_changed_ratio: float = 0.06,
) -> ShotRecord | None:
    """找最近的「画面基本相同」的截图（plan-365-1774 提速主路径）。

    为什么需要：桌面截图总在微变（任务栏时钟、鼠标光标、JPEG 压缩伪影），逐字节哈希
    几乎永不命中，于是每次截图都重跑一遍完整链路（元素采集 + 画编号 ≈ 0.5-0.7 s）。
    而「看一眼确认」的连续截图实际内容一致，沿用旧编号既快又准。

    判定：灰度缩略图逐像素比较，平均差与变化像素占比双双在阈值内才算「同一画面」。
    阈值偏保守（宁可重算）：界面真的变了（弹窗、跳页）会大面积改变像素，必然超过阈值。
    只有带编号的截图才能被沿用（无编号可沿用就无意义）。
    """
    if not signature:
        return None
    for rec in _history(ctx):
        if not rec.marks or not rec.signature:
            continue
        if max_age > 0 and rec.age() > max_age:
            continue
        if window and rec.window and window.strip() != rec.window.strip():
            continue
        mean_diff, changed = signature_diff(rec.signature, signature)
        if mean_diff <= max_mean_diff and changed <= max_changed_ratio:
            return rec
    return None


def signature_diff(a: bytes, b: bytes) -> tuple[float, float]:
    """两张灰度缩略图的差异：返回（平均像素差 0-255，变化像素占比 0-1）。

    长度不一致（不同尺寸的指纹）视为完全不同，避免错配。
    """
    if not a or not b or len(a) != len(b):
        return (255.0, 1.0)
    total = 0
    changed = 0
    n = len(a)
    for i in range(n):
        d = a[i] - b[i] if a[i] >= b[i] else b[i] - a[i]
        total += d
        if d > 12:
            changed += 1
    return (total / n, changed / n)


def mark_stale(ctx: Any) -> None:
    """写动作（点击/输入/按键/滚轮）执行后标记最新截图「已失效」（plan-365-1769 F4）。

    作用（plan-365-1773 起收敛为「编号可信度」）：动作往往立即改变界面，动作前的编号
    不再代表元素当前的位置——点击时须先按元素名在当前画面重定位，确认唯一命中后才落点，
    而不是照旧坐标点下去（lookup_ref 会把 stale 透出给调用方判断）。
    """
    with _lock:
        history = _cache.get(_key(ctx))
        if history:
            history[0].stale = True


def update_marks(
    ctx: Any, marks: dict[int, dict[str, Any]], *, shot_id: str = "", mark_path: str = "",
) -> None:
    """把编号表写回指定（或最新）截图。"""
    with _lock:
        history = _cache.get(_key(ctx), [])
        for rec in history:
            if shot_id and rec.shot_id != shot_id:
                continue
            rec.marks = dict(marks or {})
            if mark_path:
                rec.mark_path = mark_path
            return


def latest_marks(ctx: Any, *, ttl: float = REF_TTL_S) -> dict[int, dict[str, Any]]:
    """取最新一张截图的编号表（按编号时效，不做「当前画面」判定）。"""
    rec = get_shot(ctx)
    if rec is None or (ttl > 0 and rec.age() > ttl):
        return {}
    return dict(rec.marks)


def lookup_ref(ctx: Any, index: int, *, ttl: float = REF_TTL_S) -> dict[str, Any] | None:
    """编号 → 屏幕目标（跨最近几代截图检索，plan-365-1769 F2）。

    返回值比旧版多两个字段：
    - `shot_id`：该编号来自哪一代截图；
    - `stale`：**True 表示不能直接照点**——编号来自较旧的一代，或最新截图已被写动作
      标记失效；调用方应先用元素名在当前画面重新定位（唯一命中才点）。
    """
    if not index:
        return None
    history = _history(ctx)
    for pos, rec in enumerate(history):
        if ttl > 0 and rec.age() > ttl:
            continue
        target = rec.marks.get(int(index))
        if target is None:
            continue
        out = dict(target)
        out["shot_id"] = rec.shot_id
        out["stale"] = (pos > 0) or rec.stale
        return out
    return None


def clear(ctx: Any | None = None) -> None:
    """清空缓存（ctx 为空时清全部）——测试与任务结束清理用。"""
    with _lock:
        if ctx is None:
            _cache.clear()
        else:
            _cache.pop(_key(ctx), None)


def describe(rec: ShotRecord | None) -> str:
    """一行式描述，用于在工具结果里说明坐标依据。"""
    if rec is None:
        return "无可用截图"
    flag = "，已失效" if rec.stale else ""
    return f"截图 {rec.shot_id}（{rec.age() * 1000:.0f} ms 前，{len(rec.marks)} 个编号{flag}）"

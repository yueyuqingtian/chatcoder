"""操作路线自动召回（plan-20261018-operator-boost）。

【为什么由工具自动召回】
「操作前先查已沉淀的路线」原本只写在提示词里，模型经常跳过，直接重新摸索界面——
既慢又容易点错。GUI Agent 的经验复用研究（MAGNET / EchoTrail-GUI 等）的共同做法是：
**检索由系统在任务入口完成，而不是指望模型想起来去查**。

因此在「已经确定了操作目标」的那一刻（电脑操控聚焦 / 启动 / 观察某个应用窗口，内置浏览器打开某个站点），
工具按目标名匹配路线表，把原则与注意事项直接附在工具结果里：模型零额外调用就能看到可复用的做法。

同一会话同一条路线只附一次（_SHOWN 记录），避免每次截图都重复塞同样的文字。
附带不计入使用次数——模型未必采用；显式 recall 才计数（见 desktop_recipe_service）。
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)

# 会话 id → (最近一次附带时间, 已附带过的路线 id)。会话闲置超过 TTL 后重置，下个任务可再次附带。
_SHOWN: dict[int, tuple[float, set[int]]] = {}
_SHOWN_TTL_S = 45 * 60
_MAX_HINTS = 3


def host_of(url: str) -> str:
    """网址 → 站点 host（含端口）；解析失败返回空串。"""
    try:
        u = (url or "").strip()
        parsed = urlparse(u if "://" in u else "http://" + u)
        return (parsed.netloc or "").lower()
    except ValueError:
        return ""


def _shown_set(session_id: int) -> set[int]:
    now = time.monotonic()
    ts, ids = _SHOWN.get(session_id, (0.0, set()))
    if now - ts > _SHOWN_TTL_S:
        ids = set()
    _SHOWN[session_id] = (now, ids)
    return ids


def note_recalled(session_id: int, ids: list[int]) -> None:
    """显式 recall 过的路线也记为「已展示」，之后不再自动重复附带。"""
    _shown_set(session_id).update(int(i) for i in ids if i)


async def route_hint(ctx: Any, *, scope: str, names: list[str], target_label: str = "") -> str:
    """按操作目标匹配已沉淀路线，返回可直接拼进工具结果的文本；无命中 / 功能关闭时返回空串。"""
    if not getattr(settings, "desktop_recipe_enabled", True):
        return ""
    db = getattr(ctx, "db", None)
    if db is None:
        return ""
    clean = [n for n in names if n and str(n).strip()]
    if not clean:
        return ""
    try:
        from app.services import desktop_recipe_service as svc

        hits = await svc.match_for_target(db, scope=scope, names=clean, limit=_MAX_HINTS + 2)
    except Exception:  # noqa: BLE001 — 召回只是锦上添花，失败不能影响工具本身
        logger.debug("[route-hint] 匹配路线失败 scope=%s names=%s", scope, clean, exc_info=True)
        return ""
    shown = _shown_set(int(getattr(ctx, "session_id", 0) or 0))
    fresh = [h for h in hits if h.id not in shown][:_MAX_HINTS]
    if not fresh:
        return ""
    shown.update(h.id for h in fresh)
    tool = "browser_recipe" if scope == "browser" else "desktop_recipe"
    label = target_label or clean[0]
    lines = [f"\n\n[已沉淀的操作路线 · {label}] 按原则直接执行，不必重新摸索界面；界面与描述不符时再观察："]
    for h in fresh:
        lines.append(f"- 意图：{h.intent}")
        lines.append(f"  原则：{h.principle}")
        if h.pitfalls:
            lines.append(f"  注意：{h.pitfalls}")
    lines.append(f"（本次做法若有改进或新坑点，完成后用 {tool} action=save 以相同 intent 更新。）")
    return "\n".join(lines)

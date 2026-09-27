"""电脑操控操作路线的业务层（plan-334-1661）。

对外提供：按 (app_name, intent) 去重保存、检索命中、列表/编辑/批量删除、导入导出。

去重语义（本模块的核心）：同一 (app_name, intent) 已存在时**不新增**，而是：
- 累加 usage_count、刷新 last_used_at —— 让「常用路线」浮上来；
- principle/pitfalls 非空时覆盖为较新版本（模型观察得更准时自动纠正）；
- steps 一并刷新为最近一次的实际序列。
这样表不会被同类路线堆满，同时知识保持最新。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.desktop_recipe import DesktopRecipe

logger = logging.getLogger(__name__)

# 单次导入上限：防止一个异常大的 JSON 把库塞满
MAX_IMPORT_ITEMS = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: str | None) -> str:
    return (value or "").strip()


def _key(app_name: str, intent: str) -> tuple[str, str]:
    """归一化去重键：忽略大小写与首尾空白，避免「QQMusic」与「qqmusic」算两条。"""
    return (app_name.strip().casefold(), intent.strip().casefold())


def to_dict(row: DesktopRecipe) -> dict[str, Any]:
    return {
        "id": row.id,
        "app_name": row.app_name,
        "intent": row.intent,
        "principle": row.principle,
        "pitfalls": row.pitfalls or "",
        "steps": row.steps or [],
        "usage_count": row.usage_count or 0,
        "last_used_at": row.last_used_at,
        "source": row.source or "agent",
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def save_recipe(
    db: AsyncSession,
    *,
    app_name: str,
    intent: str,
    principle: str,
    pitfalls: str = "",
    steps: list | None = None,
    source: str = "agent",
) -> tuple[DesktopRecipe, bool]:
    """保存一条路线。返回 (记录, 是否新建)。

    已存在同 (app, intent) 时不新建，而是累加使用次数并更新知识内容。
    """
    app_name = _norm(app_name)
    intent = _norm(intent)
    principle = _norm(principle)
    if not app_name:
        raise ValueError("app_name 不能为空")
    if not intent:
        raise ValueError("intent 不能为空")
    if not principle:
        raise ValueError("principle（通用原则）不能为空——它是本功能的核心价值")

    rows = list((await db.execute(select(DesktopRecipe))).scalars().all())
    target_key = _key(app_name, intent)
    existing = next((r for r in rows if _key(r.app_name, r.intent) == target_key), None)

    now = _now()
    if existing is not None:
        existing.usage_count = (existing.usage_count or 0) + 1
        existing.last_used_at = now
        existing.updated_at = now
        # 新知识覆盖旧知识：模型这次的观察通常比上次更完整
        existing.principle = principle
        if _norm(pitfalls):
            existing.pitfalls = _norm(pitfalls)
        if steps:
            existing.steps = steps
        # 用户手写过的路线不因 AI 更新而降级为 agent
        if source == "user":
            existing.source = "user"
        await db.flush()
        return existing, False

    row = DesktopRecipe(
        app_name=app_name,
        intent=intent,
        principle=principle,
        pitfalls=_norm(pitfalls) or None,
        steps=steps or [],
        usage_count=1,
        last_used_at=now,
        source=source,
        updated_at=now,
    )
    db.add(row)
    await db.flush()
    return row, True


async def recall_recipe(
    db: AsyncSession,
    *,
    app_name: str = "",
    intent: str = "",
    limit: int = 3,
) -> list[DesktopRecipe]:
    """按应用名与/或意图检索路线，命中即累加使用次数。

    匹配策略（宽到窄）：应用名与意图都命中 > 仅应用名命中；两边都是子串匹配，
    因为调用方给出的措辞不会与保存时完全一致。
    """
    rows = list((await db.execute(select(DesktopRecipe))).scalars().all())
    if not rows:
        return []

    app_q = _norm(app_name).casefold()
    intent_q = _norm(intent).casefold()

    scored: list[tuple[int, DesktopRecipe]] = []
    for r in rows:
        r_app = (r.app_name or "").casefold()
        r_intent = (r.intent or "").casefold()
        score = 0
        if app_q:
            if app_q == r_app:
                score += 6
            elif app_q in r_app or r_app in app_q:
                score += 4
            else:
                continue
        if intent_q:
            if intent_q == r_intent:
                score += 6
            elif intent_q in r_intent or r_intent in intent_q:
                score += 3
            else:
                # 只给了 intent 却完全不匹配时丢弃；同时给了 app 时保留（同应用即可参考）
                if not app_q:
                    continue
        if score == 0 and not app_q and not intent_q:
            continue
        # 常用的路线优先
        score += min(3, (r.usage_count or 0) // 2)
        scored.append((score, r))

    scored.sort(key=lambda x: (-x[0], -(x[1].usage_count or 0)))
    hits = [r for _s, r in scored[: max(1, limit)]]

    if hits:
        now = _now()
        for r in hits:
            r.usage_count = (r.usage_count or 0) + 1
            r.last_used_at = now
        await db.flush()

    return hits


async def list_recipes(
    db: AsyncSession,
    *,
    keyword: str = "",
    limit: int = 200,
) -> list[DesktopRecipe]:
    rows = list((await db.execute(select(DesktopRecipe))).scalars().all())
    kw = _norm(keyword).casefold()
    if kw:
        rows = [
            r for r in rows
            if kw in (r.app_name or "").casefold()
            or kw in (r.intent or "").casefold()
            or kw in (r.principle or "").casefold()
            or kw in (r.pitfalls or "").casefold()
        ]
    # 常用且最近的排前面
    rows.sort(key=lambda r: (-(r.usage_count or 0), r.updated_at or r.created_at or ""), reverse=False)
    return rows[:limit]


async def get_recipe(db: AsyncSession, recipe_id: int) -> DesktopRecipe | None:
    return await db.get(DesktopRecipe, recipe_id)


async def update_recipe(
    db: AsyncSession,
    recipe_id: int,
    *,
    app_name: str | None = None,
    intent: str | None = None,
    principle: str | None = None,
    pitfalls: str | None = None,
) -> DesktopRecipe | None:
    """用户编辑一条路线。编辑后 source 标记为 user，避免被后续 AI 沉淀覆盖。"""
    row = await db.get(DesktopRecipe, recipe_id)
    if row is None:
        return None

    if app_name is not None:
        v = _norm(app_name)
        if not v:
            raise ValueError("app_name 不能为空")
        row.app_name = v
    if intent is not None:
        v = _norm(intent)
        if not v:
            raise ValueError("intent 不能为空")
        row.intent = v
    if principle is not None:
        v = _norm(principle)
        if not v:
            raise ValueError("principle 不能为空")
        row.principle = v
    if pitfalls is not None:
        row.pitfalls = _norm(pitfalls) or None

    row.source = "user"
    row.updated_at = _now()
    await db.flush()
    return row


async def delete_recipes(db: AsyncSession, ids: list[int]) -> int:
    """批量删除。返回实际删除条数。"""
    clean = [int(i) for i in (ids or []) if str(i).strip()]
    if not clean:
        return 0
    result = await db.execute(delete(DesktopRecipe).where(DesktopRecipe.id.in_(clean)))
    await db.flush()
    return result.rowcount or 0


async def delete_all(db: AsyncSession) -> int:
    """清空全部路线（用户主动操作，需前端二次确认）。"""
    total = (await db.execute(select(func.count()).select_from(DesktopRecipe))).scalar() or 0
    await db.execute(delete(DesktopRecipe))
    await db.flush()
    return int(total)


async def export_recipes(db: AsyncSession) -> dict:
    """导出为可移植 JSON（不含 id/时间戳等本地信息）。"""
    rows = list((await db.execute(select(DesktopRecipe))).scalars().all())
    return {
        "version": 1,
        "exported_at": _now(),
        "count": len(rows),
        "items": [
            {
                "app_name": r.app_name,
                "intent": r.intent,
                "principle": r.principle,
                "pitfalls": r.pitfalls or "",
                "steps": r.steps or [],
            }
            for r in rows
        ],
    }


async def import_recipes(db: AsyncSession, payload: dict) -> dict:
    """导入路线。按 (app_name, intent) 去重合并，返回新增/更新计数。"""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("导入内容格式不正确：缺少 items 数组")

    if len(items) > MAX_IMPORT_ITEMS:
        raise ValueError(f"一次最多导入 {MAX_IMPORT_ITEMS} 条，当前 {len(items)} 条")

    created = 0
    updated = 0
    skipped = 0
    for it in items:
        if not isinstance(it, dict):
            skipped += 1
            continue
        try:
            _row, is_new = await save_recipe(
                db,
                app_name=str(it.get("app_name") or ""),
                intent=str(it.get("intent") or ""),
                principle=str(it.get("principle") or ""),
                pitfalls=str(it.get("pitfalls") or ""),
                steps=it.get("steps") if isinstance(it.get("steps"), list) else None,
                source="user",
            )
            if is_new:
                created += 1
            else:
                updated += 1
        except ValueError:
            # 缺必填字段的条目跳过，不因单条脏数据中断整批导入
            skipped += 1

    return {"created": created, "updated": updated, "skipped": skipped}

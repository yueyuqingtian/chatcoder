"""挂靠会话的只读查询（plan-549-2281）。

【为什么单独一个模块】
plan-549-2281 下线了「创建与控制子会话」的全部能力，并行执行统一交给子代理。
但**存量数据必须继续可见**：用户左栏里已有的执行子会话仍要能列出、能打开、能删除。
因此把原先 `subsession_service` 里纯查询的那部分搬到这里，与「执行动作」彻底分开——
本模块只有 SELECT，没有任何写库、投递、唤醒、中断。

【字段口径（保留，不要删）】
`sessions.side_parent_id` / `side_kind` 是历史挂靠关系的唯一事实源：
  · `side_kind == "sub"`：执行子会话（左栏挂在主会话下方）
  · `side_kind == "side"`：侧边对话
`list_sessions` 已过滤 `side_parent_id IS NULL`，所以挂靠会话不会污染主会话列表。

【一层扁平】
任何挂靠会话的 `side_parent_id` 都指向**根主会话 id**，结构永远两层。
`root_parent_of` 是这个锚点的唯一实现。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import SideKind

logger = logging.getLogger(__name__)

# 有运行中 turn 即「运行中」（与左栏状态判定同一口径）
STATUS_RUNNING = "running"


class ChildSessionError(Exception):
    """挂靠会话查询的业务错误（路由转 400）。"""


# ── 查询辅助 ────────────────────────────────────────────────


async def _get_session_row(db: AsyncSession, session_id: int):
    from app.persistence.models.message import Session

    return await db.get(Session, session_id)


async def root_parent_of(db: AsyncSession, session_id: int) -> int:
    """返回该会话所属的**根主会话 id**（一层扁平的锚点）。

    主会话返回自身；挂靠会话返回它挂在的那个主会话。
    """
    row = await _get_session_row(db, session_id)
    if row is None:
        raise ChildSessionError("会话不存在")
    return row.side_parent_id or row.id


async def running_turn_id(db: AsyncSession, session_id: int) -> int | None:
    """该会话当前运行中的 turn id（没有则 None）。"""
    from app.persistence.models.turn import Turn

    res = await db.execute(
        select(Turn.id)
        .where(Turn.session_id == session_id, Turn.status == STATUS_RUNNING)
        .order_by(Turn.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def list_children(db: AsyncSession, parent_session_id: int) -> list:
    """某主会话下的全部执行子会话（按创建先后；同级共享同一父）。

    **调用约定：只能传根主会话 id。** 本函数内部经 `root_parent_of` 归一，传入挂靠会话
    id 时会解析成它的父会话——返回的是**父会话**名下的全部执行子会话，而不是「该挂靠
    会话的子会话」（一层扁平结构下挂靠会话根本没有子会话，见模块头【一层扁平】）。

    plan-156-621 复盘修复：删除路径的调用点已按 `side_parent_id is None` 先做根会话判定
    （见 `sessions.delete_session`），误删兄弟会话的防线在那里；本函数的签名与语义保持不变。
    """
    from app.persistence.models.message import Session

    root = await root_parent_of(db, parent_session_id)
    res = await db.execute(
        select(Session)
        .where(Session.side_parent_id == root, Session.side_kind == SideKind.SUB.value)
        .order_by(Session.id)
    )
    return list(res.scalars().all())


async def list_all_children(db: AsyncSession, project_id: int | None = None) -> list:
    """全部未归档的执行子会话（左栏一次性重建用）。

    plan-156-620：此前左栏对每个主会话各发一次 `/subsessions`（上百个请求），
    启动与切会话时把后端请求队列塞满，表现为「卡片加载很慢、点什么都慢」。
    """
    from sqlalchemy.orm import aliased

    from app.persistence.models.message import Session

    # plan-156-621：父会话已归档 / 已被永久删除时不再返回其子会话——
    # 左栏要把子会话挂在父行下方，父行不在了就无处可挂，用户看到的是无法归位的孤儿行。
    parent = aliased(Session)
    stmt = (
        select(Session)
        .join(parent, parent.id == Session.side_parent_id)
        .where(
            Session.side_kind == SideKind.SUB.value,
            Session.status != "archived",
            parent.status != "archived",
        )
    )
    if project_id is not None:
        stmt = stmt.where(Session.project_id == project_id)
    res = await db.execute(stmt.order_by(Session.id))
    return list(res.scalars().all())


async def list_running_counts_by_parent() -> dict[int, int]:
    """各主会话当前运行中的执行子会话数（只读快照）。

    plan-549-2281：子会话的**创建**已下线，本接口只剩「展示存量占用」的意义——
    仍按父分桶返回未归档子会话的运行数，返回里保留运行数为 0 的父，
    方便前端一次拿全量占用。
    复盘修复：一次聚合查询替代原先逐会话 `running_turn_id` 的 N+1（子会话多时
    每个会话一条 SQL）。
    """
    from sqlalchemy import func

    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Session
    from app.persistence.models.turn import Turn

    counts: dict[int, int] = {}
    async with async_session_factory() as db:
        res = await db.execute(
            select(Session.side_parent_id, func.count(Turn.id))
            .outerjoin(
                Turn,
                (Turn.session_id == Session.id) & (Turn.status == STATUS_RUNNING),
            )
            .where(
                Session.side_kind == SideKind.SUB.value,
                Session.status != "archived",
            )
            .group_by(Session.side_parent_id)
        )
        for parent_id, running in res.all():
            if parent_id is None:
                continue
            counts[int(parent_id)] = int(running or 0)
    return counts

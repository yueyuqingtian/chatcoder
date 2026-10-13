"""执行清单续接：新一轮（或运行中补充内容）开始时，把仍未完成的清单交给模型，由它判断续接还是调整。

背景：todo_write 的清单按轮次落库（每轮一个「任务清单」组）。用户停止后补充内容再发送，新一轮里模型看不到
上一轮的清单；前端也只能显示残留的旧步骤，直到模型第一次更新清单才切换——中间既乱又没有判断。
这里做两件事：
- 新一轮开始：把上一轮未完成的清单（含各项状态）作为一条只进入模型上下文的提示注入本轮，要求它先对照实际情况
  判断续接或调整，并立即提交完整清单；同时以 carried=true 广播给前端，让界面马上显示「待续接」的旧清单；
- 运行中补充内容：同样注入提示（不新开轮次），要求模型据此重新核对清单。
提示只进入模型上下文，不落库、不显示为用户消息。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.task import Task
from app.persistence.models.turn import Turn

# 与 tools/todo.py 的 _TODO_GROUP_TITLE 保持一致（清单组的标题）
TODO_GROUP_TITLE = "任务清单"

_STATUS_LABEL = {
    "pending": "未开始",
    "running": "上次未标记完成",
    "cancelled": "上次中断",
    "failed": "上次失败",
    "done": "已完成",
}


async def _todo_group(db: AsyncSession, turn_id: int) -> Task | None:
    res = await db.execute(
        select(Task).where(
            Task.turn_id == turn_id, Task.kind == "group", Task.title == TODO_GROUP_TITLE,
        ).order_by(Task.id.desc()).limit(1)
    )
    return res.scalars().first()


async def _visible_steps(db: AsyncSession, group_id: int) -> list[Task]:
    res = await db.execute(
        select(Task).where(
            Task.parent_task_id == group_id, Task.kind == "step", Task.is_hidden.is_(False),
        ).order_by(Task.priority.asc(), Task.id.asc())
    )
    return list(res.scalars().all())


async def load_carry_steps(db: AsyncSession, *, session_id: int, turn_id: int, mid_turn: bool) -> list[Task] | None:
    """需要续接的清单步骤（按顺序）；没有未完成项则返回 None。

    新一轮（mid_turn=False）只看紧邻的上一轮；运行中补充（mid_turn=True）先看本轮已建立的清单，没有则看上一轮。
    找到的第一份清单若已全部完成，就不再往更早的清单里找。
    """
    candidates: list[int] = [turn_id] if mid_turn else []
    prev = (await db.execute(
        select(Turn.id).where(Turn.session_id == session_id, Turn.id < turn_id)
        .order_by(Turn.id.desc()).limit(1)
    )).scalars().first()
    if prev is not None:
        candidates.append(prev)
    for tid in candidates:
        group = await _todo_group(db, tid)
        if group is None:
            continue
        steps = await _visible_steps(db, group.id)
        if steps and any(s.status != "done" for s in steps):
            return steps
        return None
    return None


def reminder_text(steps: list[Task], *, mid_turn: bool) -> str:
    """注入模型上下文的续接提示（不落库、不显示给用户）。"""
    lines = [f"{i}. [{_STATUS_LABEL.get(s.status, s.status)}] {s.title}" for i, s in enumerate(steps, 1)]
    head = (
        "用户在本轮执行中补充了内容。"
        if mid_turn
        else "上一轮的执行清单仍有未完成项（上一轮可能被用户停止，或未收尾）。"
    )
    return (
        f"【执行清单续接】{head}清单最近一次的内容如下：\n" + "\n".join(lines) + "\n"
        "请先对照实际完成情况与当前请求判断：仍需做的沿用原文并保持顺序；已完成的标记 completed；"
        "不再需要的删除；需要新增的追加。若当前请求与这份清单无关，改用新清单并简要说明原因。\n"
        "然后立即调用 todo_write 提交完整清单（不要等到后面再更新），再继续执行。"
    )


def as_todos(steps: list[Task]) -> list[dict[str, str]]:
    """续接清单的前端形态：已完成保持 completed，其余统一为 pending（前端按「待续接」展示）。"""
    return [
        {"content": s.title, "activeForm": "", "status": "completed" if s.status == "done" else "pending"}
        for s in steps
    ]

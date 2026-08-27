"""todo_write 工具：模型执行中自主维护任务清单（对齐 Codex update_plan）。

行为：
- 校验：1~12 项、content 非空、至多 1 个 in_progress；
- 若当前 turn 存在引擎管理的任务区块（拆分提案产生的 group），v23 起不再跳过：
  按标题匹配（精确优先、互相包含兜底）把清单状态同步到引擎步骤并广播 task.updated，
  让任务卡/贴条实时反映主代理的真实实现进度（替代已移除的探索子代理驱动）；
  引擎步骤只更新状态，不新增、不隐藏（拆分区块是权威计划）；
- 否则持久化到 Task 表：自建 kind=group、标题「任务清单」的区块，
  steps 按 title 匹配更新，消失的项标记隐藏，状态映射
  pending→pending / in_progress→running / completed→done，
  并广播 task.updated 让任务卡片实时刷新。
"""
from typing import Any
import logging
import re

from sqlalchemy import select

from app.orchestration.agent_events import broadcast
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.persistence.database import async_session_factory

logger = logging.getLogger(__name__)

_TODO_GROUP_TITLE = "任务清单"
_MAX_TODOS = 12
_STATUS_MAP = {"pending": "pending", "in_progress": "running", "completed": "done"}


def _norm_title(s: str) -> str:
    """标题归一化：去全部空白并转小写，用于清单项 ↔ 引擎步骤的匹配。"""
    return re.sub(r"\s+", "", (s or "").strip().lower())


class TodoWriteTool(Tool):
    name = "todo_write"
    risk_level = "low"
    description = (
        "维护当前任务的分步执行清单。复杂任务开始时创建清单；每完成一步立即更新状态；"
        "计划理解发生变化时先更新清单再继续执行。简单任务（一两次工具调用可完成）不要使用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "todos": {
                            "type": "array",
                            "description": "完整任务清单（每次提交全量，而非增量）",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {
                                        "type": "string",
                                        "description": "步骤描述，一句话，建议「文件: 动作」格式",
                                    },
                                    "activeForm": {
                                        "type": "string",
                                        "description": "进行中时的动态描述（可选），如「正在改写 build_api_copy」",
                                    },
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "completed"],
                                    },
                                },
                                "required": ["content", "status"],
                            },
                        },
                    },
                    "required": ["todos"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = args.get("todos")
        if not isinstance(raw, list) or not raw:
            return ToolResult(ok=False, output="", error="todos 不能为空")
        if len(raw) > _MAX_TODOS:
            return ToolResult(ok=False, output="", error=f"清单最多 {_MAX_TODOS} 项")

        todos: list[dict[str, str]] = []
        seen: set[str] = set()
        seen_in_progress = False
        for item in raw:
            if not isinstance(item, dict):
                return ToolResult(ok=False, output="", error="清单项格式错误")
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "")).strip()
            if not content:
                return ToolResult(ok=False, output="", error="清单项 content 不能为空")
            if status not in _STATUS_MAP:
                return ToolResult(ok=False, output="", error=f"非法状态: {status}")
            if content in seen:
                continue
            seen.add(content)
            # 宽容处理：多个 in_progress 时仅保留第一个，其余降为 pending，
            # 避免整体拒绝导致前端清单缺失（todo.updated 未广播、无转圈动画）。
            if status == "in_progress":
                if seen_in_progress:
                    status = "pending"
                seen_in_progress = True
            todos.append({
                "content": content[:200],
                "activeForm": str(item.get("activeForm", "")).strip()[:200],
                "status": status,
            })

        turn_id = ctx.task_id  # ToolContext.task_id 传入的是 turn_id
        persisted = False
        try:
            persisted = await self._sync_to_db(ctx, turn_id, todos)
        except Exception:
            # P0 修复 B: 落库失败必须留痕（此前静默吞掉，database is locked 无从排查）
            logger.warning("[todo] _sync_to_db 落库失败 turn=%s", turn_id, exc_info=True)
            persisted = False

        await broadcast(ctx.session_id, {
            "event": "todo.updated",
            "payload": {"turn_id": turn_id, "todos": todos, "persisted": persisted},
        })
        done_count = sum(1 for t in todos if t["status"] == "completed")
        return ToolResult(
            ok=True,
            output=f"任务清单已更新（{done_count}/{len(todos)} 完成）。",
            data={"todos": todos, "persisted": persisted},
        )

    async def _sync_to_db(self, ctx: ToolContext, turn_id: int, todos: list[dict[str, str]]) -> bool:
        """把清单写入 Task 表；存在引擎管理 group 时跳过（返回 False）。

        P0 修复 A: 优先用 ctx.db（与 turn 主循环同连接，避免 SQLite 跨连接写锁），
        写入后立即 commit（对齐 engine v9 立即提交模式，前端 refreshTasks 立刻可查）；
        ctx.db 为空时回退独立 session（兼容 review/子代理等路径）。
        """
        from app.persistence.models.task import Task

        async def _do_sync(db) -> bool:
            groups = list((await db.execute(
                select(Task).where(
                    Task.turn_id == turn_id, Task.kind == "group", Task.is_hidden == False,  # noqa: E712
                ).order_by(Task.id.asc())
            )).scalars().all())
            todo_group = next((g for g in groups if g.title == _TODO_GROUP_TITLE), None)
            engine_group = next((g for g in groups if g.title != _TODO_GROUP_TITLE), None)
            if engine_group is not None and todo_group is None:
                # v23: 引擎拆分区块——清单状态按标题匹配同步到引擎步骤（只改状态，不增删步骤），
                # 拆分路径的每步进度由主代理真实实现进度驱动（替代已移除的探索子代理驱动）。
                eng_steps = list((await db.execute(
                    select(Task).where(
                        Task.parent_task_id == engine_group.id, Task.is_hidden == False,  # noqa: E712
                    ).order_by(Task.priority.asc(), Task.id.asc())
                )).scalars().all())
                by_exact = {_norm_title(s.title): s for s in eng_steps}
                eng_changed: list[Task] = []
                matched_ids: set[int] = set()
                for todo in todos:
                    key = _norm_title(todo["content"])
                    step = by_exact.get(key)
                    if step is None or step.id in matched_ids:
                        step = next(
                            (s for s in eng_steps
                             if s.id not in matched_ids
                             and _norm_title(s.title) and key
                             and (_norm_title(s.title) in key or key in _norm_title(s.title))),
                            None,
                        )
                    if step is None or step.id in matched_ids:
                        continue
                    matched_ids.add(step.id)
                    new_status = _STATUS_MAP[todo["status"]]
                    if step.status != new_status:
                        step.status = new_status
                        eng_changed.append(step)
                if eng_changed:
                    engine_group.status = (
                        "done" if eng_steps and all(s.status == "done" for s in eng_steps) else "running"
                    )
                    await db.commit()
                return eng_changed

            if todo_group is None:
                request_task = (await db.execute(
                    select(Task).where(
                        Task.turn_id == turn_id, Task.kind == "request",
                    ).order_by(Task.id.asc()).limit(1)
                )).scalars().first()
                todo_group = Task(
                    session_id=ctx.session_id, turn_id=turn_id,
                    title=_TODO_GROUP_TITLE, description="模型自主维护的执行清单",
                    parent_task_id=request_task.id if request_task else None,
                    kind="group", status="running", priority=0,
                )
                db.add(todo_group)
                await db.flush()

            steps = list((await db.execute(
                select(Task).where(Task.parent_task_id == todo_group.id).order_by(Task.priority.asc(), Task.id.asc())
            )).scalars().all())
            by_title = {s.title: s for s in steps}
            used: set[int] = set()
            changed: list[Task] = []
            for index, todo in enumerate(todos):
                step = by_title.get(todo["content"])
                new_status = _STATUS_MAP[todo["status"]]
                if step is None:
                    step = Task(
                        session_id=ctx.session_id, turn_id=turn_id,
                        parent_task_id=todo_group.id, kind="step",
                        title=todo["content"], priority=index, status=new_status,
                    )
                    db.add(step)
                    await db.flush()
                else:
                    step.priority = index
                    step.is_hidden = False
                    if step.status != new_status:
                        step.status = new_status
                used.add(step.id)
                changed.append(step)
            for step in steps:
                if step.id not in used and not step.is_hidden:
                    step.is_hidden = True
                    step.status = "cancelled"
                    changed.append(step)

            done_count = sum(1 for t in todos if t["status"] == "completed")
            todo_group.status = "done" if done_count == len(todos) else "running"
            # P0 修复 A: 立即提交（同连接场景同时提交 turn 主循环 flush 的数据，幂等安全）
            await db.commit()
            return changed

        if ctx.db is not None:
            changed = await _do_sync(ctx.db)
            if changed is False:
                return False
            for step in changed:
                await broadcast(ctx.session_id, {
                    "event": "task.updated",
                    "payload": {"task_id": step.id, "status": step.status, "note": step.note or ""},
                })
            logger.info("[todo] _sync_to_db 同连接落库成功 turn=%s items=%d", turn_id, len(todos))
            return True

        # 回退路径：独立 session（review/子代理等无 ctx.db 场景）
        async with async_session_factory() as db:
            changed = await _do_sync(db)
            if changed is False:
                return False
            for step in changed:
                await broadcast(ctx.session_id, {
                    "event": "task.updated",
                    "payload": {"task_id": step.id, "status": step.status, "note": step.note or ""},
                })
            logger.info("[todo] _sync_to_db 独立连接落库成功 turn=%s items=%d", turn_id, len(todos))
            return True

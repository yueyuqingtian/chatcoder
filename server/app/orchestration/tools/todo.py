"""todo_write 工具：模型执行中自主维护任务清单（对齐 Codex update_plan）。

plan-482: 分步决策权完全在模型——本工具只做清单存取，不做任何"智能匹配/
自动拆分/标题对齐引擎步骤"的加工。模型提交什么清单，任务卡就显示什么。

行为：
- 校验：1~20 项、content 非空、至多 1 个 in_progress（多余降级为 pending）；
  超出上限时先折叠最早已完成的项，只有未完成项本身就超上限才报错（plan-20261020-pts）；
- 落库到 Task 表：自建 kind=group、标题「任务清单」的区块，steps 按模型提交的
  content 一字不差增删改（消失的项标记隐藏），状态映射
  pending→pending / in_progress→running / completed→done；
- 广播 todo.updated（前端清单/贴条）与 task.updated（任务卡实时刷新）。
"""
from typing import Any
import logging

from sqlalchemy import select

from app.orchestration.agent_events import broadcast
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.persistence.database import async_session_factory, commit_with_retry

logger = logging.getLogger(__name__)

_TODO_GROUP_TITLE = "任务清单"
# plan-20261020-pts：12 → 20。真实失败全部是「13 项，其中 9～11 项已完成」——长任务做到一半，
# 已完成的步骤把清单撑满，模型每次都要删掉历史再重提，白白失败一轮。
_MAX_TODOS = 20
_STATUS_MAP = {"pending": "pending", "in_progress": "running", "completed": "done"}


class TodoWriteTool(Tool):
    name = "todo_write"
    risk_level = "low"
    description = (
        "维护当前任务的分步执行清单，是否分步与分几步由你自主判断："
        "预计需要 3 步以上、跨多个文件、或需要边改边验证的任务，动手前先建清单；"
        "一两次工具调用就能完成的简单任务直接做，不要建清单（否则只是噪音）。"
        "执行中理解发生变化时先更新清单再继续——可以拆分、合并、重排或新增条目；"
        "每完成一步立即更新状态，不要事后批量补标。"
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
                                        "description": (
                                            "步骤描述，一句话，建议「文件: 动作」格式；"
                                            "每条对应一个可独立验证的交付物"
                                        ),
                                    },
                                    "activeForm": {
                                        "type": "string",
                                        "description": "进行中时的动态描述（可选），如「正在改写 build_api_copy」",
                                    },
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "completed"],
                                    },
                                    # plan-156-619 S6：步骤执行者。交给子代理并行时填子代理 id，
                                    # 不填 = 你自己执行（进程行不显示归属 chip）。
                                    # plan-549-2281：子会话下线后，owner 的语义从「子会话 id」改为「子代理 id」。
                                    "owner": {
                                        "type": "integer",
                                        "description": (
                                            "执行该步骤的子代理 id（spawn_subagent 的返回值）。"
                                            "只有你把这一步派给子代理时才填；自己执行的步骤不填。"
                                        ),
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

        # plan-560-2311 D8：必须**先按 content 去重、再判断 _MAX_TODOS**。
        # 原顺序是先按「超过上限」折叠最早的已完成项、再在下方按 content 去重——
        # 同 content 的重复项会先占掉名额把别的项挤进 folded 名单，去重后清单实际
        # 只剩十几项，被折叠的项随后在 _sync_to_db 被置 cancelled + is_hidden 并广播。
        # 去重不改变模型可见语义（重复项本来就会丢），所以先做无副作用。
        deduped: list[Any] = []
        _seen_content: set[str] = set()
        for it in raw:
            if isinstance(it, dict):
                _c = str(it.get("content", "")).strip()
                if _c:
                    if _c in _seen_content:
                        continue
                    _seen_content.add(_c)
            deduped.append(it)
        raw = deduped

        folded = 0
        if len(raw) > _MAX_TODOS:
            # 先折叠最早已完成的项（它们只是历史记录），未完成项一个都不丢
            open_count = sum(
                1 for it in raw
                if not (isinstance(it, dict) and str(it.get("status", "")).strip() == "completed")
            )
            if open_count > _MAX_TODOS:
                return ToolResult(
                    ok=False, output="",
                    error=f"清单最多 {_MAX_TODOS} 项，未完成的步骤就有 {open_count} 项：请合并相近步骤后再提交",
                )
            folded = len(raw) - _MAX_TODOS
            kept: list[Any] = []
            to_drop = folded
            for it in raw:
                if to_drop > 0 and isinstance(it, dict) and str(it.get("status", "")).strip() == "completed":
                    to_drop -= 1
                    continue
                kept.append(it)
            raw = kept
            # plan-560-2311 D8：被折叠的项要记下来，_sync_to_db 只把它们隐藏、不改状态——
            # 它们曾经真的完成过，把历史记录改成 cancelled 是事实失真，前端还会因此
            # 收到一批「已取消」事件。
            # plan-562-2318 L1：`folded_contents` 必须用**截断后**的 content。
            # 落库时 `title` 取的是 `content[:200]`（见下方 todos 构造），若这里存原串，
            # 超过 200 字的那一项在后面 `step.title not in _folded` 判定时对不上，
            # 会被当成「模型删掉的步骤」置成 cancelled——正是 D8 想避免的历史失真。
            folded_contents = {
                str(it.get("content", "")).strip()[:200] for it in deduped
            } - {str(it.get("content", "")).strip()[:200] for it in raw}
        else:
            folded_contents = set()

        todos: list[dict[str, Any]] = []
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
            # plan-156-619 S6：步骤执行者（子会话 id）。非法值静默丢弃（不因此拒整份清单）
            # plan-560-2311 D8：两个分支口径统一且都排除 bool / 非正数：
            # bool 是 int 的子类，`True` 会被当成 agent_id=1（凭空指向另一个子代理）；
            # 字符串分支的 .isdigit() 本来就挡住 "-5"，int 分支却会放过 -5，两套口径不一致。
            owner_raw = item.get("owner")
            owner_id: int | None = None
            if isinstance(owner_raw, bool):
                owner_id = None
            elif isinstance(owner_raw, int):
                owner_id = owner_raw if owner_raw > 0 else None
            elif isinstance(owner_raw, str):
                _s = owner_raw.strip()
                if _s.isdigit():
                    owner_id = int(_s) or None
            todos.append({
                "content": content[:200],
                "activeForm": str(item.get("activeForm", "")).strip()[:200],
                "status": status,
                "owner": owner_id,
            })

        turn_id = ctx.task_id  # ToolContext.task_id 传入的是 turn_id
        persisted = False
        try:
            persisted = await self._sync_to_db(ctx, turn_id, todos, folded_contents)
        except Exception:
            # P0 修复 B: 落库失败必须留痕（此前静默吞掉，database is locked 无从排查）
            logger.warning("[todo] _sync_to_db 落库失败 turn=%s", turn_id, exc_info=True)
            persisted = False

        await broadcast(ctx.session_id, {
            "event": "todo.updated",
            # plan-562-2318 B10：带上执行者身份。子代理的 ToolContext.session_id 就是**父会话**，
            # 而前端「进程」栏只该反映主代理的清单——不加这个标记时，子代理每写一次清单
            # 就会把主会话的进度整份覆盖掉（用户看到「进程」在 15 与 5 之间反复跳）。
            # 子代理自己的记录走它自己的线程（agent 消息流），不靠这条广播。
            "payload": {
                "turn_id": turn_id, "todos": todos, "persisted": persisted,
                "is_subagent": bool(getattr(ctx, "is_subagent", False)),
                "agent_id": ctx.agent_id,
            },
        })
        done_count = sum(1 for t in todos if t["status"] == "completed")
        return ToolResult(
            ok=True,
            output=f"任务清单已更新（{done_count}/{len(todos)} 完成）。"
            + (f"清单超过 {_MAX_TODOS} 项，已折叠最早完成的 {folded} 项。" if folded else ""),
            data={"todos": todos, "persisted": persisted},
        )

    async def _sync_to_db(
        self, ctx: ToolContext, turn_id: int, todos: list[dict[str, Any]],
        folded_contents: set[str] | None = None,
    ) -> bool:
        """把模型提交的清单如实写入 Task 表（WriteEngine 单写线程，无锁单写者）。

        plan-482: 删除"与引擎 group 按标题匹配/追加步骤"的加工逻辑——
        系统不再预拆分，清单即权威，模型提交什么就落什么。

        plan-156-619 S6：新增 `owner` → 归属列写入。
        主代理把某一步交给子代理并行时填该子代理 id，进程栏目据此显示归属 chip 并可点击跳转；
        不填（None）表示主代理自己执行。写入时与 status 同批次提交，保证「清单更新」与
        「归属更新」不会出现半更新状态。

        plan-549-2281 复盘修复：子会话下线后 `owner` 的语义已改为**子代理 id**，
        但这里仍在写 `Task.subsession_id`——两列语义不同（见 models/task.py：
        agent_id 指子代理、subsession_id 指执行子会话），写错列的直接后果是
        前端 taskProgress 用 `agent_id ?? subsession_id` 判定归属，会把子代理 id
        当成「历史子会话」并执行 switchSession(子代理id)，跳到不相干的会话。
        现在 owner 落 `Task.agent_id`；历史数据里只有 subsession_id 的步骤由前端兜底展示。

        plan-560-2311 D8：新增 `folded_contents`——因超过 _MAX_TODOS 被折叠的已完成项标题。
        这类项只置 `is_hidden`，不再把状态改成 cancelled（见下方循环内的说明）。
        """
        from app.persistence.database import run_write_locked

        _folded: set[str] = folded_contents or set()

        def _do(s) -> list[tuple]:
            from app.persistence.models.task import Task

            todo_group = s.execute(
                select(Task).where(
                    Task.turn_id == turn_id, Task.kind == "group", Task.title == _TODO_GROUP_TITLE,
                ).order_by(Task.id.asc()).limit(1)
            ).scalars().first()
            if todo_group is None:
                request_task = s.execute(
                    select(Task).where(Task.turn_id == turn_id, Task.kind == "request")
                    .order_by(Task.id.asc()).limit(1)
                ).scalars().first()
                todo_group = Task(
                    session_id=ctx.session_id, turn_id=turn_id,
                    title=_TODO_GROUP_TITLE, description="模型自主维护的执行清单",
                    parent_task_id=request_task.id if request_task else None,
                    kind="group", status="running", priority=0,
                )
                s.add(todo_group)
                s.flush()

            steps = list(s.execute(
                select(Task).where(Task.parent_task_id == todo_group.id).order_by(Task.priority.asc(), Task.id.asc())
            ).scalars().all())
            by_title = {st.title: st for st in steps}
            used: set[int] = set()
            changed: list[tuple] = []
            for index, todo in enumerate(todos):
                step = by_title.get(todo["content"])
                new_status = _STATUS_MAP[todo["status"]]
                owner_id = todo.get("owner")
                if step is None:
                    step = Task(
                        session_id=ctx.session_id, turn_id=turn_id,
                        parent_task_id=todo_group.id, kind="step",
                        title=todo["content"], priority=index, status=new_status,
                        agent_id=owner_id,
                    )
                    s.add(step)
                    s.flush()
                else:
                    step.priority = index
                    step.is_hidden = False
                    if step.status != new_status:
                        step.status = new_status
                    # 归属允许被清空（模型把该步收回自己做）：None 也要写入，不能跳过
                    if step.agent_id != owner_id:
                        step.agent_id = owner_id
                used.add(step.id)
                changed.append((step.id, step.status, step.note or "", step.agent_id))
            for step in steps:
                if step.id not in used and not step.is_hidden:
                    step.is_hidden = True
                    # plan-560-2311 D8：因超过 _MAX_TODOS 被折叠的已完成项只隐藏、不改状态。
                    # 它们确实完成过，改成 cancelled 是历史失真，前端还会因此收到一批
                    # 「已取消」事件；模型真正删掉的步骤（不在 folded_contents 里）才置 cancelled。
                    if step.title not in _folded:
                        step.status = "cancelled"
                    changed.append((step.id, step.status, step.note or "", step.agent_id))

            done_count = sum(1 for t in todos if t["status"] == "completed")
            todo_group.status = "done" if done_count == len(todos) else "running"
            s.commit()
            return changed

        try:
            changed = await run_write_locked(_do, label="todo.write")
        except Exception:
            raise
        for tid, status, note, agent_id in changed:
            await broadcast(ctx.session_id, {
                "event": "task.updated",
                # plan-156-619 S6：归属随事件下发，前端进度行无需额外请求即可显示归属 chip。
                # plan-549-2281 复盘修复：归属改落 agent_id（子代理列），但前端 store
                # （client/src/store/chat.ts）仍是 `"subsession_id" in payload` 分支消费，
                # 且不能改前端。因此 payload 两个字段都给：agent_id 给新语义，subsession_id
                # 显式传 None，让旧口径取到的永远是空值（不会把子代理 id 误显示成历史子会话）。
                # 前端 page 刷新走任务列表接口，那里的 agent_id 才是权威归属。
                "payload": {"task_id": tid, "status": status, "note": note,
                            "agent_id": agent_id, "subsession_id": None},
            })
        logger.info("[todo] _sync_to_db 写引擎落库成功 turn=%s items=%d", turn_id, len(todos))
        return True

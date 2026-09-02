"""goal_complete 工具：模型标记会话目标已达成（对齐 zcode goal_completion_verification）。

目标模式（plan-671）：会话持有活动目标时，turn 完成而模型未调用本工具会触发自动续跑。
本工具是唯一的"目标完成"信号——调用后 goal_status 置 completed、广播 goal.completed，
turn 收尾的续跑判定随之自然停止。
"""
import logging
from typing import Any

from sqlalchemy import select

from app.orchestration.agent_events import broadcast
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.persistence.database import async_session_factory

logger = logging.getLogger(__name__)


class GoalCompleteTool(Tool):
    name = "goal_complete"
    risk_level = "low"
    description = (
        "标记当前会话目标已达成。仅当会话目标模式激活（上下文含「Current Goal」段）"
        "且你确认目标描述的工作已全部完成时调用；调用后系统停止自动续跑。"
        "目标未完成时不要调用——系统会在 turn 结束后自动续跑推进剩余工作。"
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
                        "summary": {
                            "type": "string",
                            "description": "完成总结：目标达成的关键结果与验证方式，面向用户展示",
                        },
                    },
                    "required": ["summary"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        summary = str(args.get("summary", "")).strip()
        if not summary:
            return ToolResult(ok=False, output="", error="summary 不能为空")

        from app.persistence.models.message import Session

        async def _do_complete(db) -> Session | None:
            session = (await db.execute(
                select(Session).where(Session.id == ctx.session_id)
            )).scalars().first()
            if session is None or session.goal_status != "active":
                return None
            session.goal_status = "completed"
            await db.commit()
            return session

        # 优先 ctx.db（与 turn 主循环同连接，避免 SQLite 跨连接写锁），回退独立 session
        if ctx.db is not None:
            session = await _do_complete(ctx.db)
        else:
            async with async_session_factory() as db:
                session = await _do_complete(db)

        if session is None:
            return ToolResult(
                ok=False,
                output="",
                error="当前会话没有激活的目标（goal_status 非 active），无需调用 goal_complete。",
            )

        await broadcast(ctx.session_id, {
            "event": "goal.completed",
            "payload": {"turn_id": ctx.task_id, "summary": summary},
        })
        logger.info("[goal] 目标已完成 session=%s turn=%s", ctx.session_id, ctx.task_id)
        return ToolResult(
            ok=True,
            output=f"目标已标记完成：{summary}",
            data={"goal_status": "completed", "summary": summary},
        )

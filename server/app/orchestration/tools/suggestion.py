"""后续任务建议工具（plan-156-619 S10）。

会话收尾时调用一次，把「下一步可以做什么」交给用户决定：
接受 → 新开一个子会话执行；取消 → 卡片刻隐藏且永久不再提示。

只提供**一条**建议（不是列表）——建议卡固定在消息流底部，
一次给三条会让用户无从选择，也让「取消」语义变得含糊。
"""
import logging
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# 建议工具的真实名与 ta3 伪装名
SUGGESTION_TOOL_NAMES = frozenset({"suggest_next_task", "SuggestNextTask"})
# 只有智能体与工作流模式支持建议卡（只读 / 计划模式、自定义模式一律不注入）
SUGGESTION_MODES = frozenset({"agent", "workflow"})


def suggestion_allowed(mode: str, session) -> bool:
    """本轮是否向主代理注入建议卡工具与提示词：设置开启 + 智能体 / 工作流模式 + 主会话（非子会话）。"""
    from app.core.config import settings

    if not getattr(settings, "suggestion_enabled", False):
        return False
    if mode not in SUGGESTION_MODES:
        return False
    return getattr(session, "side_parent_id", None) is None


async def unfinished_work_reason(db, session_id: int) -> str | None:
    """还有未完成的子代理 / 工作流 / 子会话时返回原因（建议卡要等它们全部完成才能给）；否则 None。"""
    try:
        from app.orchestration.subagent import get_subagent_manager
        pending = int(get_subagent_manager(session_id).pending_count())
        if pending > 0:
            return f"仍有 {pending} 个子代理在运行"
    except Exception:  # noqa: BLE001 — 判定失败按「没有」处理，不挡住正常收尾
        logger.debug("[suggestion] 子代理状态读取失败", exc_info=True)
    try:
        from app.orchestration.workflow.store import running_program_ids
        if await running_program_ids(session_id, ("running", "waiting_human", "paused")):
            return "仍有未完成的工作流"
    except Exception:  # noqa: BLE001
        logger.debug("[suggestion] 工作流状态读取失败", exc_info=True)
    try:
        from app.services import subsession_service
        for child in await subsession_service.list_children(db, session_id):
            if child.status != "archived" and await subsession_service._running_turn_id(db, child.id) is not None:
                return "仍有子会话在运行"
    except Exception:  # noqa: BLE001
        logger.debug("[suggestion] 子会话状态读取失败", exc_info=True)
    return None


class SuggestNextTaskTool(Tool):
    name = "suggest_next_task"
    risk_level = "low"
    description = (
        "任务收尾时，若确有值得单独跟进的下一步，向用户提一条后续任务建议，会以卡片形式展示在消息流底部。\n"
        "只在已有实际产出、且存在具体可执行的下一步（补测试、清理遗留、跟进刚发现的风险）时调用；"
        "纯问答 / 解释、没有明确后续、或用户还要继续当前任务时不要调用。\n"
        "必须等所有子会话、子代理、工作流都已运行完成后再调用，否则会被拒绝。\n"
        "不要写客套式「需要我做别的吗」，也不要在回答正文里重复罗列。"
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
                        "title": {
                            "type": "string",
                            "description": "建议标题（不超过 40 字，动宾结构），如「补齐回滚的并发测试」",
                        },
                        "summary": {
                            "type": "string",
                            "description": "一句话说明为什么要做这件事、做完有什么收益",
                        },
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "执行步骤（2~5 条，每条一句话）",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["readonly", "plan", "agent", "workflow"],
                            "description": "建议在子会话里用什么执行模式，默认 agent",
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "可选：给子会话的背景提示——本轮已完成什么、"
                                "以及它不要重复做什么"
                            ),
                        },
                    },
                    "required": ["title", "summary", "steps"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = str(args.get("title") or "").strip()
        summary = str(args.get("summary") or "").strip()
        steps = args.get("steps")
        if not title:
            return ToolResult(ok=False, output="", error="title 不能为空")
        if not summary:
            return ToolResult(ok=False, output="", error="summary 不能为空")
        if not isinstance(steps, list) or not steps:
            return ToolResult(ok=False, output="", error="steps 不能为空（至少一条）")
        if ctx.db is None:
            return ToolResult(ok=False, output="", error="缺少数据库会话，无法保存建议")
        reason = await unfinished_work_reason(ctx.db, ctx.session_id)
        if reason:
            return ToolResult(ok=False, output="", error=f"{reason}，请等它们全部完成后再给出下一步建议")

        from app.services import suggestion_service

        sid = await suggestion_service.create_suggestion(
            ctx.db,
            session_id=ctx.session_id,
            turn_id=ctx.task_id,  # ToolContext.task_id 传入的是 turn_id
            payload={
                "title": title,
                "summary": summary,
                "steps": steps,
                "mode": args.get("mode") or "agent",
                "rationale": args.get("rationale") or "",
            },
        )
        return ToolResult(
            ok=True,
            output=(
                f"已生成后续任务建议（#{sid}），展示在消息流底部。"
                "用户可以选择在新子会话里执行，或取消。"
                "不要在回答里重复罗列这份建议——用户已经能看到卡片。"
            ),
            data={"suggestion_id": sid},
        )

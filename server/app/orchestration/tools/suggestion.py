"""后续任务建议工具（plan-156-619 S10）。

会话收尾时调用一次，把「下一步可以做什么」交给用户决定：
接受 → 新开一个子会话执行；取消 → 卡片刻隐藏且永久不再提示。

只提供**一条**建议（不是列表）——建议卡固定在消息流底部，
一次给三条会让用户无从选择，也让「取消」语义变得含糊。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult


class SuggestNextTaskTool(Tool):
    name = "suggest_next_task"
    risk_level = "low"
    description = (
        "在任务收尾时向用户提一条后续任务建议，会以卡片形式展示在消息流底部。\n"
        "适用：本轮目标已达成、但你判断还有明确的下一步值得做（补测试、清理遗留、"
        "跟进刚发现的风险、把某个临时方案做扎实）。\n"
        "不适用：任务本身还没做完（先做完再说）；只是客套式「需要我做别的吗」。\n"
        "建议必须具体可执行：写清做什么、分几步。用户可选择在新子会话执行，或直接取消。"
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

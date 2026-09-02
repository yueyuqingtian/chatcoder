"""v2.2 (对齐 zcode 3.14): AskUserQuestion 工具——模型发起结构化提问。

复用审批同款 WS 通道（approval.request，detail.kind="question"），
前端渲染为选项卡（单选/多选/自定义输入），回答作为 tool result 返回模型。
用于需求澄清，显著降低方向性返工。
"""
import json
import logging
from typing import Any

from app.orchestration.approval import approval_manager
from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# 每次最多问题数（防止模型刷屏）
_MAX_QUESTIONS = 4


class AskUserQuestionTool(Tool):
    name = "ask_user_question"
    risk_level = "low"
    description = (
        "向用户发起结构化提问（用于需求澄清）。"
        "当任务的意图、范围或验收标准不明确、存在多种可行设计、或选择取决于用户偏好时，"
        "在动手实现之前使用此工具——避免猜测用户意图导致方向性返工。"
        "能从代码库/文档/会话历史中查到的事实不要问（先自行探索）；"
        "只问真正的决策点。"
        "问题必须简洁、选项互斥且覆盖主要可能，"
        "相关的问题合并到一次调用中（最多 4 个），不要反复打断用户；"
        "allow_custom 为 true 时用户可自由输入选项之外的答案。"
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
                        "questions": {
                            "type": "array",
                            "description": (
                                "问题列表（1-4 个）。每个问题包含 question 文本与 options 选项数组；"
                                "allow_custom 为 true 时用户可自由输入答案。"
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "question": {"type": "string"},
                                    "options": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "allow_custom": {"type": "boolean"},
                                },
                                "required": ["question"],
                            },
                        },
                    },
                    "required": ["questions"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        questions = args.get("questions") or []
        if not isinstance(questions, list) or not questions:
            return ToolResult(ok=False, output="", error="questions 不能为空")
        questions = questions[:_MAX_QUESTIONS]

        approval_id = approval_manager.new_id()
        detail = {
            "kind": "question",  # 前端据此渲染选项卡（区别于权限审批卡）
            "tool": self.name,
            "questions": questions,
            "session_id": ctx.session_id,
            "agent_name": ctx.agent_name,
            "summary": f"{ctx.agent_name} 需要向你确认几个问题",
        }
        approved = await approval_manager.request(
            approval_id=approval_id, detail=detail,
        )
        if not approved:
            return ToolResult(
                ok=False, output="",
                error="提问未获回应（用户取消或超时），请基于合理假设继续或放弃该方向",
                data={"approved": False},
            )
        # 回答由 ws.py 写入 detail["answer"]（resolve 时回填，引用共享）
        answer = detail.get("answer")
        if answer is None:
            return ToolResult(
                ok=False, output="",
                error="未收到有效回答，请基于合理假设继续",
            )
        return ToolResult(
            ok=True,
            output="用户回答:\n" + json.dumps(answer, ensure_ascii=False, indent=2),
            data={"answer": answer},
        )

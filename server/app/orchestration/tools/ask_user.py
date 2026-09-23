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


def _normalize_option(opt: Any) -> str:
    """模型可能把选项写成 {label, description} 对象（schema 要求字符串）——
    统一压成展示文本，避免前端把对象当 React 子元素渲染导致整页崩溃。"""
    if isinstance(opt, str):
        return opt
    if isinstance(opt, dict):
        def pick(keys: tuple[str, ...]) -> str:
            for k in keys:
                v = opt.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        label = pick(("label", "text", "title", "value", "name"))
        desc = pick(("description", "desc", "detail", "hint"))
        if label and desc:
            return f"{label} — {desc}"
        if label or desc:
            return label or desc
        try:
            return json.dumps(opt, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(opt)
    return str(opt)


def _normalize_questions(questions: list[Any]) -> list[Any]:
    """每个问题的 options 规范化为字符串数组（其余字段原样保留）。"""
    out: list[Any] = []
    for q in questions:
        if isinstance(q, dict):
            item = dict(q)
            opts = item.get("options")
            if isinstance(opts, list):
                item["options"] = [_normalize_option(o) for o in opts]
            out.append(item)
        else:
            out.append(q)
    return out


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
                                        "description": "选项文本数组（必须为字符串，不要用 {label, description} 对象）",
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
        # v36 紧急修复：规范化选项——模型常把 options 写成 [{label, description}] 对象数组，
        # 旧前端把对象当 React 子元素渲染会抛 React #31 并整页崩溃（用户实测）。
        questions = _normalize_questions(questions)

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

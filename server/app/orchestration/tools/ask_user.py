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
from app.services.attachment_service import resolve_upload

logger = logging.getLogger(__name__)

# 每次最多问题数（防止模型刷屏）。
# plan-598-2517：4 → 8。旧值 4 配合 run() 里的静默切片，会让模型一次抛 5 个问题时
# 第 5 个被无提示地丢掉——模型以为问了 5 个、用户只看到 4 个，后续还会基于“用户已答第 5 题”
# 继续推理。改为 8（前端分步渲染本身无压力），并在超限时把“已忽略第 N 题”回给模型。
_MAX_QUESTIONS = 8


def _normalize_option(opt: Any) -> str:
    """模型可能把选项写成 {label, description} 对象（schema 要求字符串）——
    统一压成展示文本，避免前端把对象当 React 子元素渲染导致整页崩溃。"""
    if isinstance(opt, str):
        return opt.strip()
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
        return label
    return ""


def _as_bool(value: Any, default: bool = False) -> bool:
    """模型常把布尔值写成字符串 / 数字（"true" / 1），统一归一。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "y", "on"):
            return True
        if low in ("false", "0", "no", "n", "off", "", "none", "null"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


# 多选的键名别名：ta3 官方 schema 用 multiple，模型也常照搬 Claude 的习惯写 multiSelect
_MULTI_KEYS = ("multiple", "multiSelect", "multi_select", "multi")

# 选项的键名别名：模型偶尔把选项写进 choices / answers 等键，卡片会因此整块没有可点选项
_OPTION_KEYS = ("options", "choices", "answer_options", "answers")


def _pick_option_list(item: dict) -> list[Any] | None:
    """取第一个「确实是数组」的选项键（保持键名多样性的同时不伪造空列表）。"""
    for key in _OPTION_KEYS:
        value = item.get(key)
        if isinstance(value, list) and (options := [v for opt in value if (v := _normalize_option(opt))]):
            return list(dict.fromkeys(options))
    return None


def _normalize_questions(questions: list[Any]) -> list[Any]:
    """每个问题的 options 规范化为字符串数组，并归一多选/自定义输入标记。

    - options 保持原样（缺省/非数组）时不动：模型可以只提开放式问题不给选项，
      前端会渲染自定义输入行让用户直接作答（用户实测：没选项的卡片此前整块不可作答）。
    - multiple：`multiple` / `multiSelect` 等别名统一成布尔 `multiple`；缺省视为单选，
      前端按单选/多选分别渲染（多选允许勾多个 + 补充自定义输入）。
    - allowCustom 归一成 allow_custom：非 ta3 供应商会照官方 schema 传驼峰键。
    """
    out: list[Any] = []
    for q in questions:
        if isinstance(q, dict):
            item = dict(q)
            opts = _pick_option_list(item)
            had_option_list = any(isinstance(item.get(key), list) for key in _OPTION_KEYS)
            for key in _OPTION_KEYS:
                if key in item:
                    item.pop(key)
            if had_option_list:
                item["options"] = opts or []
            for key in _MULTI_KEYS:
                if key in item:
                    item["multiple"] = _as_bool(item.pop(key), False)
                    break
            if "allowCustom" in item or "allow_custom" in item:
                item["allow_custom"] = not opts or _as_bool(item.get("allow_custom", item.get("allowCustom")), True)
                item.pop("allowCustom", None)
            out.append(item)
        else:
            out.append(q)
    return out


def validate_question_reply(questions: list[dict], answer: Any, references: Any) -> tuple[dict, list[dict]]:
    """plan-20261118-runtime-question-cache：每题独立校验，持久化前以本地文件元信息为准。"""
    if not isinstance(answer, dict) or (references is not None and not isinstance(references, dict)):
        raise ValueError("回答格式无效，请重新提交")
    references = references or {}
    keys = {str(i) for i in range(len(questions))}
    if (answer.keys() | references.keys()) - keys:
        raise ValueError("题号已变化，请重新提交当前提问")
    canonical = {}
    attachments = []
    for index, question in enumerate(questions):
        key = str(index)
        value = answer.get(key, "")
        multiple = question.get("multiple") is True
        values = value if multiple and isinstance(value, list) else [value]
        if not all(isinstance(v, str) for v in values):
            raise ValueError(f"第 {index + 1} 题回答格式无效")
        values = list(dict.fromkeys(v.strip() for v in values if v.strip()))
        files = references.get(key, [])
        if not isinstance(files, list) or any(not isinstance(ref, dict) for ref in files):
            raise ValueError(f"第 {index + 1} 题附件格式无效")
        options = question.get("options") or []
        if options and question.get("allow_custom") is False and (
            files or any(v not in options for v in values)
        ):
            raise ValueError(f"第 {index + 1} 题仅接受预设选项")
        if not values and not files:
            raise ValueError(f"请回答第 {index + 1} 题")
        canonical[key] = values if multiple else (values[0] if values else "")
        seen = set()
        for ref in files:
            attachment = resolve_upload(ref)
            if attachment["file_id"] not in seen:
                seen.add(attachment["file_id"])
                attachments.append({**attachment, "question_index": index})
    return canonical, attachments


class AskUserQuestionTool(Tool):
    name = "ask_user_question"
    risk_level = "low"
    description = (
        "向用户发起结构化提问（用于需求澄清）。"
        "当任务的意图、范围或验收标准不明确、存在多种可行设计、或选择取决于用户偏好时，"
        "在动手实现之前使用此工具——避免猜测用户意图导致方向性返工。"
        "能从代码库/文档/会话历史中查到的事实不要问（先自行探索）；"
        "只问真正的决策点。"
        "问题必须简洁、选项覆盖主要可能；互斥的选择用默认单选，"
        "选项之间互相独立、可以同时成立时把该题的 multiple 设为 true（用户可勾选多个，也能再补充自定义输入）。"
        "相关的问题合并到一次调用中（最多 8 个），不要反复打断用户；"
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
                            "maxItems": _MAX_QUESTIONS,
                            "description": (
                                f"问题列表（1-{_MAX_QUESTIONS} 个）。每个问题包含 question 文本与 options 选项数组；"
                                "allow_custom 为 true 时用户可自由输入答案。"
                                f"超过 {_MAX_QUESTIONS} 个时后续问题会被忽略（请不要依赖被忽略的问题）。"
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
                                    "multiple": {
                                        "type": "boolean",
                                        "description": (
                                            "是否多选（默认单选）。选项之间互相独立、可以同时成立时设为 true——"
                                            "用户可以勾选多个选项，也可以再补充自定义输入；"
                                            "互斥的选择保持默认单选即可"
                                        ),
                                    },
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
        # plan-598-2517：截断不能静默——被丢掉的问题模型以为问过了，会基于不存在的回答
        # 继续推理（用户实测：AI 说“清理/还原那项没作答”，而那条问题根本没展示给用户）。
        # 这里记录被忽略的区间，随 tool result 一同回给模型，让它知道哪些问题没问出口。
        _requested = len(questions)
        _dropped = _requested - _MAX_QUESTIONS if _requested > _MAX_QUESTIONS else 0
        questions = questions[:_MAX_QUESTIONS]
        # v36 紧急修复：规范化选项——模型常把 options 写成 [{label, description}] 对象数组，
        # 旧前端把对象当 React 子元素渲染会抛 React #31 并整页崩溃（用户实测）。
        questions = _normalize_questions(questions)
        invalid = [str(i + 1) for i, q in enumerate(questions)
                   if not isinstance(q, dict) or not isinstance(q.get("question"), str)
                   or not q["question"].strip()]
        if invalid:
            return ToolResult(
                ok=False, output="", error_code="ARG_INVALID",
                error=f"第 {'、'.join(invalid)} 题题干为空或格式无效。请为每题填写清晰的 question 字符串后重新提问。",
            )
        for q in questions:
            q["question"] = q["question"].strip()

        approval_id = approval_manager.new_id()
        detail = {
            "kind": "question",  # 前端据此渲染选项卡（区别于权限审批卡）
            "tool": self.name,
            "questions": questions,
            "session_id": ctx.session_id,
            "agent_name": ctx.agent_name,
            "summary": f"{ctx.agent_name} 需要向你确认几个问题",
            # plan-522-2209：提问与审批共用同一份身份标记，等待策略与日志口径保持单一来源
            "is_subagent": bool(getattr(ctx, "is_subagent", False)),
            "is_subsession": bool(getattr(ctx, "is_subsession", False)),
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
        attachments = detail.get("answer_attachments") or []
        attachment_lines = []
        for item in attachments:
            hint = "此文件类型无法直接解析" if item["type"] == "file" else "可用附件读取工具按路径读取"
            attachment_lines.append(f"- 第 {item['question_index'] + 1} 题：{item['filename']}，路径 `{item['path']}`。{hint}。")
        return ToolResult(
            ok=True,
            output="用户回答:\n" + json.dumps(answer, ensure_ascii=False, indent=2)
            + ("\n\n回答附件：\n" + "\n".join(attachment_lines) if attachment_lines else "")
            + (
                # plan-598-2517：明确告知模型这部分问题**从未展示给用户**，不要当作已问过
                f"\n\n注意：本次共提交 {_requested} 个问题，超过单次上限 {_MAX_QUESTIONS}，"
                f"第 {_MAX_QUESTIONS + 1}~{_requested} 个问题（共 {_dropped} 个）**未被展示给用户**，"
                "因此没有对应回答。若仍需这些信息，请另起一次提问。"
                if _dropped else ""
            ),
            data={"answer": answer, "dropped_questions": _dropped,
                  **({"attachments": attachments} if attachments else {})},
        )

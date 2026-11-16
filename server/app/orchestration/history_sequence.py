"""plan-20261103-fork：请求副本的配对校验，持久化历史不参与修补。"""
import logging
import json
from functools import wraps
from collections import deque
from contextvars import ContextVar

logger = logging.getLogger(__name__)
request_diagnostic_context = ContextVar("context_request", default={})
_recent_diagnostics = deque(maxlen=100)


class HistorySequenceError(ValueError):
    """本地结构错误；不应该换凭据或反复重发相同请求。"""


def validate_tool_sequence(messages):
    seen, pending = set(), set()
    for m in messages:
        row = m if isinstance(m, dict) else m.model_dump()
        role = row.get("role")
        if role == "tool":
            key = row.get("tool_call_id")
            if not key or key not in pending:
                raise HistorySequenceError("历史工具结果缺少唯一对应的调用，请从完整轮次重新创建分支")
            pending.remove(key)
            continue
        if pending:
            raise HistorySequenceError("历史工具调用尚未配齐结果，无法发送，请从完整轮次重新创建分支")
        calls = row.get("tool_calls") or []
        if calls and role != "assistant":
            raise HistorySequenceError("历史工具调用的消息角色异常")
        for call in calls:
            key = call.get("id")
            if not key or key in seen:
                raise HistorySequenceError("历史工具调用编号缺失或重复")
            seen.add(key)
            pending.add(key)
    if pending:
        raise HistorySequenceError("历史末尾存在未完成工具调用，请从完整轮次重新创建分支")


def prepare_history(messages):
    """正常历史直接通过；异常历史仅在深拷贝上进行一次既有修复。"""
    for m in messages:
        for call in m.tool_calls or []:
            _validate_function(call.get("name"), call.get("arguments", {}))
    try:
        validate_tool_sequence(messages)
        from app.orchestration.compaction import ensure_conversation_lead
        return ensure_conversation_lead(messages)
    except HistorySequenceError:
        from app.orchestration.compaction import repair_tool_call_ids, ensure_tool_pairing, normalize_tool_sequence
        copied = [m.model_copy(deep=True) for m in messages]
        copied = repair_tool_call_ids(copied)
        copied = ensure_tool_pairing(copied)
        copied = normalize_tool_sequence(copied)
        validate_tool_sequence(copied)
        logger.info("[history] 出站工具序列已修复 messages=%s", len(copied))
        from app.orchestration.compaction import ensure_conversation_lead
        return ensure_conversation_lead(copied)


def _validate_function(name, arguments):
    if not isinstance(name, str) or not name.strip():
        raise HistorySequenceError("请求工具名称缺失，已停止发送非法上下文")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError) as exc:
            raise HistorySequenceError("请求工具参数不是有效 JSON 对象") from exc
    if not isinstance(arguments, dict):
        raise HistorySequenceError("请求工具参数必须是对象")


def structure_diagnostic(messages, *, protocol):
    """plan-20261109-compact-structure：只导出结构，不含正文、参数值、思考或凭据。"""
    rows = [m if isinstance(m, dict) else m.model_dump() for m in messages]
    roles = [m.get("role") for m in rows]
    block_calls = block_results = 0
    checkpoints = []
    for i, row in enumerate(rows):
        content = row.get("content")
        blocks = content if isinstance(content, list) else []
        block_calls += sum(b.get("type") in ("tool_use", "tool-call") for b in blocks)
        block_results += sum(b.get("type") in ("tool_result", "tool-result") for b in blocks)
        texts = [content] if isinstance(content, str) else [b.get("text", "") for b in blocks]
        if (getattr(messages[i], "context_kind", None) == "checkpoint" or
                any("<conversation_summary>" in t or "<compacted-summary>" in t for t in texts if isinstance(t, str))):
            checkpoints.append(i)
    return {"stage": "wire_validation", "protocol": protocol, "messages": len(rows),
            "roles": roles, "first": roles[0] if roles else None, "last": roles[-1] if roles else None,
            "calls": sum(len(m.get("tool_calls") or []) for m in rows) + block_calls,
            "results": sum(m.get("role") == "tool" and not isinstance(m.get("content"), list)
                           for m in rows) + block_results,
            "reasoning_positions": [i for i, m in enumerate(rows) if "reasoning_content" in m],
            "checkpoint_positions": checkpoints}


def _log_wire(messages, protocol):
    diagnostic = {**request_diagnostic_context.get(), **structure_diagnostic(messages, protocol=protocol)}
    _recent_diagnostics.append(diagnostic)
    logger.info("[context-structure] %s", json.dumps(diagnostic))


def context_diagnostics(session_id=None):
    from copy import deepcopy
    return deepcopy([d for d in _recent_diagnostics if session_id is None or d.get("session_id") == session_id])


def record_upstream_error(error):
    """错误正文可能回显输入，仅保留状态和上游标识。"""
    import re
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) or getattr(error, "status_code", None)
    if status is None:
        match = re.search(r"(?:HTTP\s*|请求失败\s*)([45]\d\d)", str(error), re.I)
        status = int(match.group(1)) if match else None
    headers = getattr(response, "headers", {})
    code = None
    if response is not None:
        try:
            data = response.json()
            error_body = data.get("error") or {}
            candidate = error_body.get("code") if isinstance(error_body, dict) else data.get("code")
            if candidate is not None and re.fullmatch(r"[\w.-]{1,80}", str(candidate)):
                code = str(candidate)
        except (ValueError, AttributeError):
            pass
    diagnostic = {**request_diagnostic_context.get(), "stage": "upstream_rejection",
                  "status": status, "error_type": type(error).__name__,
                  "error_code": code,
                  "request_id": headers.get("request-id") or headers.get("x-request-id")}
    _recent_diagnostics.append(diagnostic)
    logger.warning("[context-structure] %s", json.dumps(diagnostic))


def _wire_guard(protocol):
    def decorate(validate):
        @wraps(validate)
        def checked(messages, *args, **kwargs):
            try:
                return validate(messages, *args, **kwargs)
            except HistorySequenceError:
                rows = messages if isinstance(messages, list) else []
                diagnostic = {**request_diagnostic_context.get(), **structure_diagnostic(rows, protocol=protocol), "failed": True}
                _recent_diagnostics.append(diagnostic)
                logger.error("[context-structure] %s", json.dumps(diagnostic))
                raise
        return checked
    return decorate

@_wire_guard("openai")

def validate_openai_wire(messages):
    validate_tool_sequence(messages)
    for row in messages:
        if row.get("role") not in ("system", "developer", "user", "assistant", "tool"):
            raise HistorySequenceError("请求包含不支持的消息角色")
        for call in row.get("tool_calls") or []:
            fn = call.get("function") or {}
            _validate_function(fn.get("name"), fn.get("arguments"))
    _log_wire(messages, "openai")
    return messages


@_wire_guard("anthropic")
def validate_anthropic_wire(messages):
    """检查适配后的原生工具块，不依赖 OpenAI 角色表示。"""
    projected = []
    for row in messages:
        if row.get("role") not in ("user", "assistant"):
            raise HistorySequenceError("Anthropic 消息只能使用用户或助手角色")
        blocks = row.get("content")
        if not isinstance(blocks, list):
            projected.append({"role": row.get("role")})
            continue
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        results = [b for b in blocks if b.get("type") == "tool_result"]
        for block in calls:
            _validate_function(block.get("name"), block.get("input"))
        if calls and (row.get("role") != "assistant" or results):
            raise HistorySequenceError("Anthropic 工具调用块角色或顺序异常")
        if results:
            if row.get("role") != "user" or any(b.get("type") != "tool_result" for b in blocks[:len(results)]):
                raise HistorySequenceError("Anthropic 工具结果必须在用户消息块最前面")
        for block in blocks:
            if block.get("type") == "thinking" and not block.get("signature"):
                raise HistorySequenceError("Anthropic 历史思考块缺少原始签名")
        if calls:
            projected.append({"role": row.get("role"), "tool_calls": [{"id": b.get("id")} for b in calls]})
        elif results:
            projected.extend({"role": "tool", "tool_call_id": b.get("tool_use_id")} for b in results)
            if any(b.get("type") != "tool_result" for b in blocks):
                projected.append({"role": row.get("role")})
        else:
            projected.append({"role": row.get("role")})
    validate_tool_sequence(projected)
    if messages and messages[0].get("role") != "user":
        raise HistorySequenceError("Anthropic 对话缺少用户开场")
    _log_wire(messages, "anthropic")
    return messages


def finish_anthropic_wire(messages, thinking=None):
    """思考模式不支持助手预填；相邻用户块合并且结果仍位于最前。"""
    from copy import deepcopy
    out = []
    for row in deepcopy(messages):
        blocks = row.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}] if blocks else []
        if not blocks:
            continue
        row["content"] = blocks
        if out and out[-1]["role"] == row["role"]:
            out[-1]["content"].extend(blocks)
        else:
            out.append(row)
    if thinking and thinking.get("type") in ("enabled", "adaptive") and out and out[-1]["role"] == "assistant":
        out.append({"role": "user", "content": [{"type": "text", "text": "请继续完成当前任务。"}]})
    return validate_anthropic_wire(out)


@_wire_guard("commandcode")
def validate_commandcode_wire(messages):
    projected = []
    for row in messages:
        blocks = row.get("content") or []
        calls = [b for b in blocks if b.get("type") == "tool-call"]
        results = [b for b in blocks if b.get("type") == "tool-result"]
        for block in calls:
            _validate_function(block.get("toolName"), block.get("input"))
        if results:
            if row.get("role") != "tool" or len(results) != len(blocks):
                raise HistorySequenceError("CommandCode 工具结果角色异常")
            projected.extend({"role": "tool", "tool_call_id": b.get("toolCallId")} for b in results)
        else:
            projected.append({"role": row.get("role"), "tool_calls": [
                {"id": b.get("toolCallId")} for b in calls]})
    validate_tool_sequence(projected)
    _log_wire(messages, "commandcode")
    return messages


@_wire_guard("kiro")
def validate_kiro_wire(payload):
    state = payload.get("conversationState") or {}
    current = (state.get("currentMessage") or {}).get("userInputMessage")
    history = state.get("history") or []
    if not current or (history and "assistantResponseMessage" not in history[-1]):
        raise HistorySequenceError("Kiro 当前输入或历史结尾结构异常")
    projected = []
    for row in [*history, {"userInputMessage": current}]:
        if "assistantResponseMessage" in row:
            uses = row["assistantResponseMessage"].get("toolUses") or []
            for block in uses:
                _validate_function(block.get("name"), block.get("input"))
            projected.append({"role": "assistant", "tool_calls": [{"id": b.get("toolUseId")} for b in uses]})
        elif "userInputMessage" in row:
            user = row["userInputMessage"]
            results = (user.get("userInputMessageContext") or {}).get("toolResults") or []
            projected.extend({"role": "tool", "tool_call_id": b.get("toolUseId")} for b in results)
            projected.append({"role": "user"})
        else:
            raise HistorySequenceError("Kiro 历史消息结构异常")
    validate_tool_sequence(projected)
    _log_wire(projected, "kiro")
    return payload


def trim_complete_tool_batches(all_rows, selected):
    """plan-20261103-fork：预算截取落在调用批次内部时，整批移出本次窗口。"""
    if not selected:
        return selected
    positions = {m.id: i for i, m in enumerate(all_rows)}
    cut = positions[selected[0].id]
    pending, spans = {}, []
    for i, m in enumerate(all_rows):
        key = (m.turn_id, m.thread_id, (m.content or {}).get("call_key"))
        if m.msg_type == "tool_call":
            pending.setdefault(key, []).append(i)
        elif m.msg_type == "tool_result" and pending.get(key):
            spans.append((pending[key].pop(0), i))
    spans.extend((start, len(all_rows) - 1) for starts in pending.values() for start in starts)
    # 跨批次区间按起点排序后单遍推进；相互重叠的并行调用一起移出。
    for start, end in sorted(spans):
        if start < cut <= end:
            cut = end + 1
    return all_rows[cut:]

"""plan-20261103-fork：请求副本的配对校验，持久化历史不参与修补。"""
import logging

logger = logging.getLogger(__name__)


class HistorySequenceError(ValueError):
    pass


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
    try:
        validate_tool_sequence(messages)
        return messages
    except HistorySequenceError:
        from app.orchestration.compaction import repair_tool_call_ids, ensure_tool_pairing, normalize_tool_sequence
        copied = [m.model_copy(deep=True) for m in messages]
        copied = repair_tool_call_ids(copied)
        copied = ensure_tool_pairing(copied)
        copied = normalize_tool_sequence(copied)
        validate_tool_sequence(copied)
        logger.info("[history] 出站工具序列已修复 messages=%s", len(copied))
        return copied


def validate_openai_wire(messages):
    validate_tool_sequence(messages)
    return messages


def validate_anthropic_wire(messages):
    """检查适配后的原生工具块，不依赖 OpenAI 角色表示。"""
    projected = []
    for row in messages:
        blocks = row.get("content")
        if not isinstance(blocks, list):
            projected.append({"role": row.get("role")})
            continue
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if calls:
            projected.append({"role": row.get("role"), "tool_calls": [{"id": b.get("id")} for b in calls]})
        elif results:
            projected.extend({"role": "tool", "tool_call_id": b.get("tool_use_id")} for b in results)
            if any(b.get("type") != "tool_result" for b in blocks):
                projected.append({"role": row.get("role")})
        else:
            projected.append({"role": row.get("role")})
    validate_tool_sequence(projected)
    return messages


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

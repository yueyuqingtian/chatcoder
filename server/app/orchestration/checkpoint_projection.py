"""plan-20261109-compact-structure：本轮续跑与跨轮重建共享检查点投影。"""
import logging
from collections import defaultdict, deque

from app.models.schemas import ChatMessage

logger = logging.getLogger(__name__)


def active_compactions(context: dict) -> list[dict]:
    seen = set()
    result = []
    # plan-20261115-checkpoint-degrade：shared_context 是 JSON 动态字段，历史数据里
    # compactions 可能不是数组（手改 / 迁移 / 旧格式），只认 list，避免一条脏数据打死整轮。
    items = context.get("compactions") if isinstance(context, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = item.get("compaction_id")
        if key and key not in seen and not item.get("merged") and not item.get("restored"):
            result.append(item)
            seen.add(key)
    return result


async def load_checkpoints(db, context: dict, *, missing: list[str] | None = None) -> list[ChatMessage]:
    """按活 compaction 取出检查点正文；单条缺失只跳过，不打死会话。

    plan-20261115-checkpoint-degrade：此前缺一条就抛 HistorySequenceError，而本函数位于
    「每轮构建上下文」「压缩后投影」「重试前修复」三条必经路径上——SUMMARY 行被软删
    （turns.py / mailbox.py 都会软删）或正文被清空后，该会话每一轮请求都会失败，
    用户无法自救。现在改为降级：缺失项记 warning 并收集到 missing（供调用方提示），
    其余有效检查点照常注入；真正的结构非法由 history_sequence 的序列校验负责。
    """
    from app.persistence.models.message import Message

    result: list[ChatMessage] = []
    for item in active_compactions(context):
        compaction_id = item.get("compaction_id")
        row_id = item.get("summary_message_id")
        row = await db.get(Message, row_id) if row_id else None
        text = (row.content or {}).get("text") if row is not None and not row.deleted else None
        if not isinstance(text, str) or not text.strip():
            logger.warning("[compact] 压缩检查点正文缺失，已跳过 compaction=%s row=%s",
                           compaction_id, row_id)
            if missing is not None:
                missing.append(str(compaction_id))
            continue
        result.append(ChatMessage(role="developer", content=text, context_kind="checkpoint",
                                  checkpoint_id=compaction_id, source_message_ids=[row.id]))
    return result


def project_checkpoints(messages: list[ChatMessage], checkpoints: list[ChatMessage],
                        shadowed_ids=()) -> list[ChatMessage]:
    """只移除已确认遮蔽的来源，未知来源/本轮任务/运行时输入均保留。"""
    from app.orchestration.compaction import finalize_compacted_messages

    shadowed = set(shadowed_ids)
    prefix, body = [], []
    # 调用及其结果共享一个删除决定，部分来源已压缩时不能丢掉保留调用的真实结果。
    removable_calls = set()
    results_by_id = defaultdict(list)
    for m in messages:
        if m.role == "tool":
            results_by_id[m.tool_call_id].append(m)
    for m in messages:
        if not m.tool_calls:
            continue
        ids = {tc.get("id") for tc in m.tool_calls}
        results = [r for key in ids for r in results_by_id[key]]
        batch = [m, *results]
        if (len(results) == len(ids) and all(r.source_message_ids and
                set(r.source_message_ids) <= shadowed for r in batch)):
            removable_calls.update(ids)
    for source in messages:
        m = source.model_copy(deep=True)
        if m.context_kind == "checkpoint":
            continue
        if m.tool_calls:
            if all(tc.get("id") in removable_calls for tc in m.tool_calls):
                continue
        elif m.role == "tool":
            if m.tool_call_id in removable_calls:
                continue
        elif m.context_kind != "current_task" and m.source_message_ids and set(m.source_message_ids) <= shadowed:
            continue
        (prefix if m.role in ("system", "developer") else body).append(m)
    unique, seen = [], set()
    for m in checkpoints:
        if m.checkpoint_id not in seen:
            unique.append(m.model_copy(deep=True))
            seen.add(m.checkpoint_id)
    return finalize_compacted_messages(prefix + unique + body)


def bind_runtime_sources(messages: list[ChatMessage], rows) -> list[ChatMessage]:
    """工具按稳定调用键绑定；文本按时间顺序精确匹配，未知内容不推断删除。"""
    calls, results, texts = defaultdict(deque), defaultdict(deque), defaultdict(deque)
    already_bound = {key for m in messages for key in m.source_message_ids}
    for row in rows:
        if row.id in already_bound:
            continue
        c = row.content or {}
        if row.msg_type == "tool_call":
            calls[c.get("call_key") or f"call_{row.id}"].append(row.id)
        elif row.msg_type == "tool_result":
            results[c.get("call_key") or f"call_{row.id}"].append(row.id)
        elif row.msg_type == "text":
            texts[("user" if row.sender_type == "user" else "assistant", c.get("text") or "")].append(row.id)
    copied = [m.model_copy(deep=True) for m in messages]
    for m in copied:
        if m.source_message_ids or m.context_kind in ("checkpoint", "current_task"):
            continue
        if m.tool_calls:
            ids = [calls[tc.get("id")].popleft() for tc in m.tool_calls if calls[tc.get("id")]]
            # 部分调用尚未落库时，整批必须保留。
            if len(ids) == len(m.tool_calls):
                m.source_message_ids = ids
        elif m.role == "tool" and results[m.tool_call_id]:
            m.source_message_ids = [results[m.tool_call_id].popleft()]
        elif m.role in ("user", "assistant") and texts[(m.role, m.content or "")]:
            m.source_message_ids = [texts[(m.role, m.content or "")].popleft()]
    return copied

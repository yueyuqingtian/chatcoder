"""落库式上下文压缩器（v30，参照 deepseek-harness compaction 能力缝隙）。

与 compaction.py（内存级规范化 + 旧 auto_compact）的关系：
- compaction.py 保留 ensure_tool_pairing / normalize_tool_sequence /
  build_api_copy / micro_compact 等"每次请求前的内存变换"；
- 本模块是新的**落库式压缩**：按 token 预算选定范围 → LLM 生成结构化
  checkpoint → 插入 SUMMARY 消息 + 更新 session.shared_context.compacted_ids，
  下轮重建上下文时被压缩消息不再注入、checkpoint 摘要注入。

对齐 deepseek-harness 的关键设计：
1. region 选择（select_compactable_range）：保留最近 retain_tokens，向前对齐
   tool_call/tool_result 配对边界，绝不拆散工具回合；
2. 阴影定价：压缩结果记录 shadowed_ids / shadowed_tokens / saved_tokens，
   前端据此渲染压缩卡片（压缩了哪些消息、省了多少 token）；
3. 结构化 checkpoint：COMPACTION_PROMPT 9 段 + <compacted-summary> 帧 +
   CHECKPOINT_PREAMBLE，接手模型把压缩内容视为既定背景；
4. 事件协议：compact.started（触发占用）→ compact.summary（阴影定价）→
   compact.completed，全部经 WS 广播供前端状态机消费。
"""
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.prompts import CHECKPOINT_PREAMBLE, COMPACTION_PROMPT, SUMMARY_CLOSE_TAG, SUMMARY_OPEN_TAG
from app.orchestration.token_counter import (
    estimate_message_tokens_from_model,
    get_agent_context_window,
    messages_token_total,
)

logger = logging.getLogger(__name__)

# 参与压缩的消息类型（thinking/plan 摘要价值低，排除；error/system 不压缩）
_COMPACT_KEEP_TYPES = {
    MsgType.TEXT.value, MsgType.TOOL_CALL.value, MsgType.TOOL_RESULT.value,
}
# 摘要时可忽略的消息类型（不进入重放/transcript）
_SUMMARY_SKIP_TYPES = {
    MsgType.THINKING.value, MsgType.ARTIFACT.value, MsgType.ERROR.value,
}


def _call_key(m) -> str:
    """从消息提取 tool_call/tool_result 的配对 key。"""
    content = m.content if isinstance(m.content, dict) else {}
    return str(content.get("call_key") or "")


def select_compactable_range(messages: list, retain_tokens: int) -> tuple[int, int] | None:
    """按 token 预算 + tool 配对边界选择压缩范围（参照 deepseek-harness region.ts）。

    从尾部向前累计 token 至 retain_tokens 得初始切点 k0，再向后移动切点
    找到第一个"配对平衡"位置 k：压缩区 [0, k-1] 内每个 tool_call 都有
    配对的 tool_result、保留区 [k:] 内没有孤立的 tool_result。

    Args:
        messages: 按时间正序的消息列表（DB Message）。
        retain_tokens: 最近尾部原样保留的 token 预算。

    Returns:
        (start, end) 被压缩的闭区间索引；无可压缩范围时返回 None。
    """
    if not messages:
        return None

    # 1. 从尾部向前累计 token，定初始切点
    keep_from = len(messages)
    acc = 0
    for i in range(len(messages) - 1, -1, -1):
        acc += estimate_message_tokens_from_model(messages[i])
        keep_from = i
        if acc >= retain_tokens:
            break
    if keep_from == 0:
        return None

    # 2. 向后找第一个配对平衡切点（压缩区收缩，保留区扩大，收益仍满足预算）
    k = keep_from
    while k <= len(messages):
        if _is_pairing_balanced(messages, k):
            break
        k += 1
    if k <= 1 or k > len(messages):
        return None
    return (0, k - 1)


def _is_pairing_balanced(messages: list, k: int) -> bool:
    """切点 k 是否配对平衡。

    1. 压缩区 [0,k-1] 内每个 tool_call 都必须在压缩区内配对闭合
       （result 落在保留区 = 配对被切断，不合法）；
    2. 保留区 [k:] 内每个 tool_result 的配对 call 也必须在保留区内且在其前
       （call 落在压缩区 = 保留区出现孤立 result，不合法）。
    """
    open_ids: set[str] = set()
    for m in messages[:k]:
        if m.msg_type == MsgType.TOOL_CALL.value:
            open_ids.add(_call_key(m))
        elif m.msg_type == MsgType.TOOL_RESULT.value:
            open_ids.discard(_call_key(m))
    if open_ids:
        return False
    call_keys: set[str] = set()
    for m in messages[k:]:
        if m.msg_type == MsgType.TOOL_CALL.value:
            call_keys.add(_call_key(m))
        elif m.msg_type == MsgType.TOOL_RESULT.value:
            if _call_key(m) not in call_keys:
                return False
    return True


# ---------------------------------------------------------------------------
# 摘要构建
# ---------------------------------------------------------------------------

def _fallback_summary(messages: list) -> str:
    """硬编码降级摘要（LLM 不可用/失败时），逻辑与 compaction.emergency_compact 等价。"""
    from collections import defaultdict

    parts: list[str] = []
    tool_names: list[str] = []
    files: set[str] = set()
    for m in messages:
        c = m.content if isinstance(m.content, dict) else {}
        if m.msg_type == MsgType.TOOL_CALL.value:
            tool_names.append(str(c.get("tool") or "?"))
            args = c.get("args")
            if isinstance(args, dict):
                path = args.get("path") or args.get("repo")
                if path:
                    files.add(str(path))
        elif m.msg_type == MsgType.TOOL_RESULT.value:
            out = str(c.get("output") or c.get("error") or "")
            if out.strip():
                parts.append(f"  result: {out[:100]}")
        elif m.msg_type == MsgType.TEXT.value:
            text = str(c.get("text") or "")
            if text.strip():
                speaker = "user" if m.sender_type == SenderType.USER.value else "assistant"
                parts.append(f"[{speaker}] {text[:200]}")
    lines = ["以下是之前对话的摘要："]
    if tool_names:
        counts = defaultdict(int)
        for n in tool_names:
            counts[n] += 1
        lines.append("已调用工具: " + ", ".join(f"{k}({v}次)" for k, v in counts.items()))
    if files:
        lines.append("涉及文件: " + ", ".join(sorted(files)[:20]))
    if parts:
        lines.append("关键内容:\n" + "\n".join(parts[:30]))
    return "\n".join(lines)


def _build_transcript(messages: list) -> str:
    """把待压缩消息转成摘要用文本（降级路径，参照旧 auto_compact）。"""
    from collections import defaultdict

    lines: list[str] = []
    tool_names: list[str] = []
    files: set[str] = set()
    for m in messages:
        c = m.content if isinstance(m.content, dict) else {}
        if m.msg_type == MsgType.TOOL_CALL.value:
            tool = str(c.get("tool") or "?")
            tool_names.append(tool)
            args = c.get("args")
            args_str = ""
            if isinstance(args, dict):
                import json
                try:
                    args_str = json.dumps(args, ensure_ascii=False)[:150]
                except (TypeError, ValueError):
                    args_str = str(args)[:150]
                path = args.get("path") or args.get("repo")
                if path:
                    files.add(str(path))
            lines.append(f"[tool_call] {tool}({args_str})")
        elif m.msg_type == MsgType.TOOL_RESULT.value:
            out = str(c.get("output") or c.get("error") or "")
            lines.append(f"[tool_result] {out[:200]}")
        elif m.msg_type == MsgType.TEXT.value:
            text = str(c.get("text") or "")
            if text.strip():
                speaker = "user" if m.sender_type == SenderType.USER.value else "assistant"
                lines.append(f"[{speaker}] {text[:300]}")
    if tool_names:
        counts = defaultdict(int)
        for n in tool_names:
            counts[n] += 1
        lines.insert(0, "已调用工具: " + ", ".join(f"{k}({v}次)" for k, v in counts.items()))
    if files:
        lines.insert(1, "涉及文件: " + ", ".join(sorted(files)[:20]))
    return "\n".join(lines)


async def _summarize_with_llm(db: AsyncSession, provider, messages: list, max_chars: int = 4000) -> str:
    """LLM 生成结构化 checkpoint 摘要。

    优先 KV 缓存复用路径：把待压缩消息结构化为 user/assistant/tool 重放序列
    （保留工具调用结构信息，比纯文本 transcript 摘要质量更高）；
    provider/LLM 失败时降级为硬编码摘要。
    """
    if not messages:
        return ""
    try:
        replay: list[ChatMessage] = []
        pending_text: list[str] = []
        for m in messages:
            c = m.content if isinstance(m.content, dict) else {}
            if m.msg_type == MsgType.TOOL_CALL.value:
                if pending_text:
                    replay.append(ChatMessage(role="assistant", content="\n".join(pending_text)))
                    pending_text = []
                tool = str(c.get("tool") or "unknown")
                args = c.get("args") or {}
                key = _call_key(m) or f"call_{m.id}"
                replay.append(ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[{"id": key, "type": "function",
                                 "function": {"name": tool, "arguments": args}}],
                ))
            elif m.msg_type == MsgType.TOOL_RESULT.value:
                out = str(c.get("output") or c.get("error") or "(无输出)")
                replay.append(ChatMessage(
                    role="tool", content=out[:2000],
                    tool_call_id=_call_key(m) or f"call_{m.id}",
                    name=str(c.get("tool") or "unknown"),
                ))
            elif m.msg_type == MsgType.TEXT.value:
                text = str(c.get("text") or "").strip()
                if text:
                    if m.sender_type == SenderType.USER.value:
                        if pending_text:
                            replay.append(ChatMessage(role="assistant", content="\n".join(pending_text)))
                            pending_text = []
                        replay.append(ChatMessage(role="user", content=text))
                    else:
                        pending_text.append(text[:800])
        if pending_text:
            replay.append(ChatMessage(role="assistant", content="\n".join(pending_text)))
        if not replay:
            return ""

        req = ChatRequest(
            messages=[
                ChatMessage(role="system", content=COMPACTION_PROMPT),
                *replay,
                ChatMessage(
                    role="user",
                    content="请按上述结构输出 checkpoint 摘要，不要调用任何工具。",
                ),
            ],
            model="",
            temperature=0.3,
        )
        resp = await provider.chat(req)
        text = (resp.content or "").strip()
        return text[:max_chars] if text else ""
    except Exception as e:
        logger.warning("[compressor] LLM 摘要失败，降级硬编码: %s", e)
        return ""


# ---------------------------------------------------------------------------
# 落库式压缩事务
# ---------------------------------------------------------------------------

async def compact_session(
    db: AsyncSession, *,
    session, provider, context_window: int,
    used_tokens: int | None = None,
    agent_id: int | None = None,
    agent_name: str = "main",
    turn_id: int | None = None,
    trigger: str = "pressure",
    retain_tokens: int | None = None,
    max_summary_chars: int = 4000,
) -> dict | None:
    """执行一次落库式压缩事务（参照 deepseek-harness compactSurfaceRegion）。

    流程：选范围 → 收益检查 → LLM 摘要（降级硬编码）→ 插入 SUMMARY 消息 →
    更新 shared_context.compacted_ids/compactions → 广播 compact.summary/
    compact.completed。压缩从下轮上下文重建开始生效（本轮内存消息不动）。

    Args:
        db: 数据库会话。
        session: 待压缩的主会话。
        provider: 摘要用 LLM provider（None 时直接降级硬编码）。
        context_window: 模型上下文窗口（用于计算保留预算与阈值）。
        used_tokens: 触发时的真实占用（broadcast 用）。
        agent_id / agent_name: 广播身份。
        turn_id: 压缩归属 turn（SUMMARY 消息挂在哪个 turn）。
        trigger: pressure（step 压力）/ context-overflow（溢出恢复）。
        retain_tokens: 尾部保留预算，缺省按 context_window × 0.16 计算
            （对齐 deepseek-harness 默认 retainRatio=0.16）。
        max_summary_chars: 摘要文本上限。

    Returns:
        压缩结果 dict（含 compaction_id / shadowed_ids / saved_tokens 等），
        无可压缩范围或收益不足时返回 None。
    """
    from app.orchestration.context_memory import _fetch_main_messages
    from app.orchestration.token_counter import get_main_summarize_threshold

    ctx = session.shared_context or {}
    if not isinstance(ctx, dict):
        ctx = {}
    summarized_ids = set(ctx.get("summarized_ids") or [])
    compacted_ids = set(ctx.get("compacted_ids") or [])

    all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
    # 候选 = 未摘要未压缩、类型可压缩的主线程消息
    candidates = [
        m for m in all_msgs
        if m.id not in summarized_ids and m.id not in compacted_ids
        and m.msg_type in _COMPACT_KEEP_TYPES
    ]
    if len(candidates) < 4:
        logger.debug("[compressor] session=%s 候选消息 %d 条过少，跳过压缩", session.id, len(candidates))
        return None

    if retain_tokens is None:
        retain_tokens = max(4000, int(context_window * 0.16))
    # 收益检查：可回收 token 必须大于压缩自身成本（LLM 摘要输出 + 重建开销）
    total_tokens = messages_token_total(candidates)
    min_reclaim = max(2000, int(context_window * 0.05))
    if total_tokens <= min_reclaim:
        logger.debug(
            "[compressor] session=%s 候选 %d tokens <= 最小回收 %d，跳过",
            session.id, total_tokens, min_reclaim,
        )
        return None

    span = select_compactable_range(candidates, retain_tokens)
    if span is None:
        logger.debug("[compressor] session=%s 无可压缩范围(retain=%d)", session.id, retain_tokens)
        return None
    start_idx, end_idx = span
    shadowed = candidates[start_idx:end_idx + 1]
    shadowed_ids = [m.id for m in shadowed]
    shadowed_tokens = messages_token_total(shadowed)

    # 摘要（LLM 优先，硬编码降级）
    if provider is None:
        summary_text = _fallback_summary(shadowed)
    else:
        summary_text = await _summarize_with_llm(db, provider, shadowed, max_chars=max_summary_chars)
        if not summary_text:
            summary_text = _fallback_summary(shadowed)

    summary_text = summary_text.strip()
    if not summary_text:
        logger.warning("[compressor] session=%s 摘要为空，跳过压缩", session.id)
        return None

    compaction_id = "cp-" + uuid.uuid4().hex[:12]
    saved_tokens = max(0, shadowed_tokens - len(summary_text.encode("utf-8")) // 4)

    # v30.1: 压缩块索引——会话内压缩序号（从 1 起）。SUMMARY 消息与
    # shared_context.compactions 都携带 index，AI 可据此按索引查看压缩前会话
    # （compaction_index / compaction_view 工具）。先 refresh 拿最新 compactions
    # 防并发覆盖，再落 SUMMARY 消息携带 index。
    await db.refresh(session)
    latest_ctx = session.shared_context or {}
    if not isinstance(latest_ctx, dict):
        latest_ctx = {}
    compaction_index = len(latest_ctx.get("compactions") or []) + 1

    # 落库：SUMMARY 消息（checkpoint 帧）
    from app.services.message_service import create_message

    frame_text = f"{CHECKPOINT_PREAMBLE}\n\n{SUMMARY_OPEN_TAG}\n{summary_text}\n{SUMMARY_CLOSE_TAG}"
    summary_msg = await create_message(
        db,
        session_id=session.id,
        sender_type=SenderType.SYSTEM.value,
        msg_type=MsgType.SUMMARY.value,
        content={
            "text": frame_text,
            "compaction_id": compaction_id,
            "index": compaction_index,
            "checkpoint": True,
            "trigger": trigger,
            "shadowed_ids": shadowed_ids,
            "shadowed_tokens": shadowed_tokens,
            "saved_tokens": saved_tokens,
            "summary_tokens": max(1, len(summary_text.encode("utf-8")) // 4),
        },
        turn_id=turn_id,
        broadcast=True,
    )

    # 更新 shared_context（基于上面已 refresh 的 latest_ctx，乐观合并）。
    # 拷贝新 dict 再赋值：JSON 列同引用赋值不触发 UPDATE（SQLAlchemy 按 identity 检测 dirty）
    latest_compacted = set(latest_ctx.get("compacted_ids") or [])
    latest_compacted.update(shadowed_ids)
    latest_compactions = list(latest_ctx.get("compactions") or [])
    latest_compactions.append({
        "compaction_id": compaction_id,
        "index": compaction_index,
        "summary_message_id": summary_msg.id,
        "shadowed_ids": shadowed_ids,
        "shadowed_tokens": shadowed_tokens,
        "saved_tokens": saved_tokens,
        "trigger": trigger,
        "created_at": str(summary_msg.created_at),
    })
    new_ctx = dict(latest_ctx)
    new_ctx["compacted_ids"] = sorted(latest_compacted)
    new_ctx["compactions"] = latest_compactions
    session.shared_context = new_ctx
    await db.flush()
    await db.commit()

    result = {
        "compaction_id": compaction_id,
        "index": compaction_index,
        "summary_message_id": summary_msg.id,
        "shadowed_ids": shadowed_ids,
        "shadowed_tokens": shadowed_tokens,
        "saved_tokens": saved_tokens,
        "summary": summary_text,
        "trigger": trigger,
        "retained_tokens": retain_tokens,
        "used_tokens": used_tokens,
        "context_window": context_window,
        "ratio": round((used_tokens or 0) / context_window * 100, 1) if context_window else None,
    }
    logger.info(
        "[compressor] session=%s 压缩完成: index=%d %d 条消息 %d tokens -> checkpoint(%d chars, %d tokens)，节省 %d tokens (trigger=%s)",
        session.id, compaction_index, len(shadowed_ids), shadowed_tokens, len(summary_text), result["summary_tokens"], saved_tokens, trigger,
    )
    return result


async def emergency_compact_session(
    db: AsyncSession, *, session, provider, context_window: int,
    used_tokens: int | None = None,
    agent_id: int | None = None,
    agent_name: str = "main",
    turn_id: int | None = None,
) -> dict | None:
    """溢出恢复压缩（trigger='context-overflow'）。

    与 pressure 压缩的区别：不按 16% 保留预算，只保留最近 6 个工具回合
    （对齐旧 emergency_compact 语义），强制做一次有效缩减。
    """
    from app.orchestration.context_memory import _fetch_main_messages

    ctx = session.shared_context or {}
    if not isinstance(ctx, dict):
        ctx = {}
    summarized_ids = set(ctx.get("summarized_ids") or [])
    compacted_ids = set(ctx.get("compacted_ids") or [])

    all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
    candidates = [
        m for m in all_msgs
        if m.id not in summarized_ids and m.id not in compacted_ids
        and m.msg_type in _COMPACT_KEEP_TYPES
    ]
    if len(candidates) < 4:
        return None

    # 溢出恢复：保留最近 6 个工具回合（从尾部数 6 个 TOOL_CALL 的位置）
    call_positions = [i for i, m in enumerate(candidates) if m.msg_type == MsgType.TOOL_CALL.value]
    if len(call_positions) <= 6:
        return None
    # 切点取第 (倒数第6个调用) 的索引；保留区从该调用开始
    keep_from = call_positions[-6]
    # 向前对齐配对平衡（保留区不应含孤立 result / 压缩区不应含未闭合 call）
    k = keep_from
    while k <= len(candidates) and not _is_pairing_balanced(candidates, k):
        k += 1
    if k <= 1 or k > len(candidates):
        return None
    span = (0, k - 1)
    shadowed = candidates[span[0]:span[1] + 1]

    return await _commit_compaction(
        db, session=session, provider=provider, shadowed=shadowed,
        context_window=context_window, used_tokens=used_tokens,
        agent_id=agent_id, agent_name=agent_name, turn_id=turn_id,
        trigger="context-overflow",
    )


async def _commit_compaction(
    db: AsyncSession, *, session, provider, shadowed: list,
    context_window: int, used_tokens: int | None,
    agent_id: int | None, agent_name: str, turn_id: int | None, trigger: str,
) -> dict | None:
    """共享提交逻辑：摘要 → SUMMARY 消息 → compacted_ids → 广播。"""
    from app.orchestration.token_counter import get_main_summarize_threshold

    shadowed_ids = [m.id for m in shadowed]
    shadowed_tokens = messages_token_total(shadowed)

    if provider is None:
        summary_text = _fallback_summary(shadowed)
    else:
        summary_text = await _summarize_with_llm(db, provider, shadowed)
        if not summary_text:
            summary_text = _fallback_summary(shadowed)
    summary_text = summary_text.strip()
    if not summary_text:
        logger.warning("[compressor] session=%s 摘要为空，跳过压缩", session.id)
        return None

    compaction_id = "cp-" + uuid.uuid4().hex[:12]
    saved_tokens = max(0, shadowed_tokens - len(summary_text.encode("utf-8")) // 4)

    # v30.1: 压缩块索引（先 refresh 拿最新 compactions，再落 SUMMARY 消息）
    await db.refresh(session)
    latest_ctx = session.shared_context or {}
    if not isinstance(latest_ctx, dict):
        latest_ctx = {}
    compaction_index = len(latest_ctx.get("compactions") or []) + 1

    from app.services.message_service import create_message

    frame_text = f"{CHECKPOINT_PREAMBLE}\n\n{SUMMARY_OPEN_TAG}\n{summary_text}\n{SUMMARY_CLOSE_TAG}"
    summary_msg = await create_message(
        db,
        session_id=session.id,
        sender_type=SenderType.SYSTEM.value,
        msg_type=MsgType.SUMMARY.value,
        content={
            "text": frame_text,
            "compaction_id": compaction_id,
            "index": compaction_index,
            "checkpoint": True,
            "trigger": trigger,
            "shadowed_ids": shadowed_ids,
            "shadowed_tokens": shadowed_tokens,
            "saved_tokens": saved_tokens,
            "summary_tokens": max(1, len(summary_text.encode("utf-8")) // 4),
        },
        turn_id=turn_id,
        broadcast=True,
    )

    latest_compacted = set(latest_ctx.get("compacted_ids") or [])
    latest_compacted.update(shadowed_ids)
    latest_compactions = list(latest_ctx.get("compactions") or [])
    latest_compactions.append({
        "compaction_id": compaction_id,
        "index": compaction_index,
        "summary_message_id": summary_msg.id,
        "shadowed_ids": shadowed_ids,
        "shadowed_tokens": shadowed_tokens,
        "saved_tokens": saved_tokens,
        "trigger": trigger,
        "created_at": str(summary_msg.created_at),
    })
    new_ctx = dict(latest_ctx)
    new_ctx["compacted_ids"] = sorted(latest_compacted)
    new_ctx["compactions"] = latest_compactions
    session.shared_context = new_ctx
    await db.flush()
    await db.commit()

    result = {
        "compaction_id": compaction_id,
        "index": compaction_index,
        "summary_message_id": summary_msg.id,
        "shadowed_ids": shadowed_ids,
        "shadowed_tokens": shadowed_tokens,
        "saved_tokens": saved_tokens,
        "summary": summary_text,
        "trigger": trigger,
        "used_tokens": used_tokens,
        "context_window": context_window,
        "ratio": round((used_tokens or 0) / context_window * 100, 1) if context_window else None,
    }
    logger.info(
        "[compressor] session=%s %s 压缩完成: index=%d %d 条消息 %d tokens -> %d tokens，节省 %d",
        session.id, trigger, compaction_index, len(shadowed_ids), shadowed_tokens, result["summary_tokens"], saved_tokens,
    )
    return result

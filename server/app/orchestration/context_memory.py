"""v3.0: 分层记忆系统 + 三层渐进式压缩。

解决 AI 上下文有限的问题，采用三层记忆架构：
1. 工作记忆(Working Memory): 最近 N 条消息直接进入 prompt
2. 摘要记忆(Summary Memory): 超出窗口的历史被压缩为摘要，存入 session.shared_context
3. 检索记忆(Retrieval): 提供 memory.search 工具，让 AI 按需检索更早的消息

v3.0 改进（参考 Claude Code / OpenCode 最佳实践）：
- Thread 历史正确序列化 tool_call/tool_result（修复关键 Bug）
- 窗口大小扩大，配合 micro_compact 自动压缩工具结果
- 摘要质量大幅提升（结构化 6 段格式，2048 token 输出）
- 新增 fs.grep 工具，Agent 可搜索代码内容

设计原则：
- 滑动窗口：主会话保留最近 N 条消息，配合 micro_compact 自动压缩
- 自动摘要：消息数超过阈值时，压缩为结构化摘要
- 按需检索：AI 可通过 memory.search / fs.grep 检索历史和代码
- Token 驱动：通过 token_counter 估算，在模型上下文 80% 以内动态管理
"""
import json
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.models.schemas import ChatMessage, ChatRequest
from app.models.registry import get_model_registry
from app.orchestration.token_counter import (
    estimate_message_tokens_from_model,
    get_agent_context_window,
    get_main_summarize_batch_tokens,
    get_main_summarize_threshold,
    get_main_window_budget,
    messages_token_total,
    MIN_MESSAGES_KEEP,
    select_messages_by_token_budget,
)
from app.services import session_service

if TYPE_CHECKING:
    from app.persistence.models.message import Message, Session

logger = logging.getLogger(__name__)

# ── v3.3: Per-Agent 动态窗口管理 ──
# 所有窗口/阈值基于每个 Agent 的 Model.context_window 按比例计算，不再固定。
# 比例定义在 token_counter.py 中：
#   MAIN_WINDOW_RATIO = 0.08       主群聊窗口 = ctx_window × 8%
#   MAIN_SUMMARIZE_RATIO = 0.15    摘要触发 = ctx_window × 15%
#   MAIN_SUMMARIZE_BATCH_RATIO = 0.06  每次摘要量 = ctx_window × 6%
#   THREAD_WINDOW_RATIO = 0.15     Thread 窗口 = ctx_window × 15%
#   AGENT_LOOP_COMPACT_RATIO = 0.72 Agent Loop 压缩 = ctx_window × 72%
#
# 示例（不同模型对比）：
# ┌──────────┬──────────┬──────────┬──────────┬──────────┐
# │ 模型窗口  │ 主群聊窗口│ 摘要触发  │ Thread   │ Loop压缩  │
# │ 200K     │ 16K      │ 30K      │ 30K      │ 144K     │
# │ 500K     │ 40K      │ 75K      │ 75K      │ 360K     │
# │ 1M       │ 80K      │ 150K     │ 150K     │ 720K     │
# └──────────┴──────────┴──────────┴──────────┴──────────┘
#
# 群聊摘要触发使用 Leader 的 context_window（Leader 是群聊的主要消费方）
# Thread 窗口使用被分配 Agent 的 context_window（各自模型不同）

# 摘要保留的最大条数（用于检索）
SUMMARY_MAX_CHARS = 4000
# v3.1/v6.0: 超级摘要触发条数（纳入 config，可在 .env 调整）
from app.core.config import settings as _settings
SUPER_SUMMARY_TRIGGER = _settings.super_summary_trigger
SUPER_SUMMARY_KEEP_LATEST = _settings.super_summary_keep_latest

# 默认上下文窗口（用于无法解析 agent 的场景，如 Leader 缺失）
_DEFAULT_CTX_WINDOW_FALLBACK = 500000


async def _fetch_main_messages(
    db: AsyncSession, session_id: int, limit: int = 200
) -> "list[Message]":
    """取主会话全部消息（按时间正序）。"""
    from app.persistence.models.message import Message

    res = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .where(Message.thread_id.is_(None))
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list(reversed(res.scalars().all()))


async def _summarize_messages(messages: "list[Message]") -> str:
    """调用 LLM 把一批消息压缩为结构化摘要。失败时降级为简单拼接。

    v3.0: 摘要质量大幅提升 —— 结构化 6 段格式，max_tokens=2048（原 500）。
    """
    if not messages:
        return ""

    provider = get_model_registry().get_default_provider()
    if provider is None:
        # 降级：每条取前 80 字
        lines = []
        for m in messages:
            speaker = m.sender_type
            if m.sender_type == "agent":
                speaker = m.content.get("agent_name") or f"agent#{m.sender_id}"
            text = m.content.get("text") or m.content.get("note") or ""
            # 包含工具调用摘要
            if m.msg_type == MsgType.TOOL_CALL:
                tool = m.content.get("tool", "")
                text = f"[调用了工具 {tool}]"
            elif m.msg_type == MsgType.TOOL_RESULT:
                tool = m.content.get("tool", "")
                ok = "成功" if m.content.get("ok") else "失败"
                text = f"[工具结果 {tool} {ok}]"
            text = text[:80]
            if text:
                lines.append(f"- [{speaker}] {text}")
        return "历史摘要(降级):\n" + "\n".join(lines)[:SUMMARY_MAX_CHARS]

    # 构造待摘要文本（包含工具调用和结果摘要）
    lines = []
    for m in messages:
        speaker = m.sender_type
        if m.sender_type == "agent":
            speaker = m.content.get("agent_name") or f"agent#{m.sender_id}"
        if m.msg_type == MsgType.TOOL_CALL:
            tool = m.content.get("tool", "")
            args = m.content.get("args", {})
            args_str = json.dumps(args, ensure_ascii=False)[:100] if args else ""
            text = f"调用了工具 {tool}({args_str})"
        elif m.msg_type == MsgType.TOOL_RESULT:
            tool = m.content.get("tool", "")
            ok = "成功" if m.content.get("ok") else "失败"
            output = (m.content.get("output") or m.content.get("error") or "")[:200]
            text = f"工具 {tool} {ok}: {output}"
        else:
            text = m.content.get("text") or m.content.get("note") or "(非文本)"
        lines.append(f"[{speaker}] {text}")
    transcript = "\n".join(lines)

    try:
        request = ChatRequest(
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        "You are a conversation summarization assistant. Compress the group chat transcript below into a structured summary covering:\n"
                        "1. Key decisions and consensus\n"
                        "2. Assigned tasks and owners\n"
                        "3. Completed work (specific files, commands, operations)\n"
                        "4. Open issues and blockers\n"
                        "5. Key file paths involved\n\n"
                        "Keep it under 800 words, in bullet points. "
                        "Never omit file paths and technical details."
                    ),
                ),
                ChatMessage(role="user", content=transcript),
            ],
            model="",
        )
        resp = await provider.chat(request)
        return (resp.content or "").strip()[:SUMMARY_MAX_CHARS]
    except Exception as e:
        logger.warning("摘要生成失败，降级: %s", e)
        lines = []
        for m in messages:
            text = (m.content.get("text") or "")[:80]
            if text:
                lines.append(f"- {text}")
        return "历史摘要(降级):\n" + "\n".join(lines)[:SUMMARY_MAX_CHARS]


async def _compress_super_summary(summaries: list[dict]) -> list[dict]:
    """v3.1: 当摘要超过上限时，将最旧的若干条压缩为一条超级摘要。

    避免直接丢弃最旧摘要导致长期记忆丢失。
    保留最新 SUPER_SUMMARY_KEEP_LATEST 条不压缩。
    """
    if len(summaries) <= SUPER_SUMMARY_TRIGGER:
        return summaries

    to_compress = summaries[:-SUPER_SUMMARY_KEEP_LATEST]
    latest = summaries[-SUPER_SUMMARY_KEEP_LATEST:]

    texts = [s["text"] for s in to_compress]
    merged_text = "\n\n---\n\n".join(texts)

    provider = get_model_registry().get_default_provider()
    if provider:
        try:
            request = ChatRequest(
                messages=[
                    ChatMessage(
                        role="system",
                        content=(
                            "You are a session summary compression assistant. Merge and compress the multiple history summaries below into one refined summary. "
                            "Must preserve: key technical decisions, file paths, completed work, open issues, and code conventions. "
                            "Keep it under 600 words, in bullet points."
                        ),
                    ),
                    ChatMessage(role="user", content=merged_text[:6000]),
                ],
                model="",
            )
            resp = await provider.chat(request)
            super_text = (resp.content or "").strip()[:2000]
        except Exception as e:
            logger.warning("超级摘要生成失败，降级为截断拼接: %s", e)
            super_text = merged_text[:1500]
    else:
        super_text = merged_text[:1500]

    all_ids: list[int] = []
    for s in to_compress:
        r = s.get("range", [])
        if isinstance(r, list) and len(r) >= 2:
            all_ids.extend(r)
        elif isinstance(r, int):
            all_ids.append(r)

    compressed = {
        "text": super_text,
        "range": [min(all_ids) if all_ids else 0, max(all_ids) if all_ids else 0],
        "count": sum(s.get("count", 0) for s in to_compress),
        "is_super": True,
    }
    return [compressed] + latest


async def _resolve_leader_context_window(db: AsyncSession, session: "Session") -> int:
    """解析 Leader Agent 的模型上下文窗口大小。

    v3：团队概念已移除，不再有 Leader 角色。
    回退到默认上下文窗口（_DEFAULT_CTX_WINDOW_FALLBACK）。
    """
    return _DEFAULT_CTX_WINDOW_FALLBACK


async def maybe_summarize_main_session(
    db: AsyncSession, session: "Session",
    context_window: int | None = None,
) -> None:
    """检查主会话是否需要摘要，需要则生成并更新 shared_context。

    v3.3: 摘要触发阈值基于 Leader 模型的 context_window 动态计算。
    - context_window 由调用方传入，或自动从 Leader 模型解析
    - 触发阈值 = context_window × MAIN_SUMMARIZE_RATIO (15%)
    - 每次摘要量 = context_window × MAIN_SUMMARIZE_BATCH_RATIO (6%)
    """
    # v3.3: 解析 Leader 的 context_window
    if context_window is None:
        context_window = await _resolve_leader_context_window(db, session)

    summarize_threshold = get_main_summarize_threshold(context_window)
    batch_tokens = get_main_summarize_batch_tokens(context_window)

    messages = await _fetch_main_messages(db, session.id)

    ctx = session.shared_context or {}
    if not isinstance(ctx, dict):
        ctx = {}

    summarized_ids: set[int] = set(ctx.get("summarized_ids") or [])
    # 只看未摘要的消息
    candidates = [m for m in messages if m.id not in summarized_ids]

    # v3.3: 按 token 触发，而非按条数
    candidates_tokens = messages_token_total(candidates)
    if candidates_tokens <= summarize_threshold:
        return

    # 按 token 批量选取（从最早的开始，直到达到 batch_tokens 目标）
    to_summarize: list = []
    accumulated = 0
    for m in candidates:
        msg_tokens = estimate_message_tokens_from_model(m)
        to_summarize.append(m)
        accumulated += msg_tokens
        if accumulated >= batch_tokens:
            break

    if len(to_summarize) < 3:
        return

    summary_text = await _summarize_messages(to_summarize)
    if not summary_text:
        return

    # v3.1: 乐观锁 —— 写入前重读 session 最新状态，避免并发覆盖
    await db.refresh(session)
    latest_ctx = session.shared_context or {}
    if not isinstance(latest_ctx, dict):
        latest_ctx = {}

    latest_summarized_ids = set(latest_ctx.get("summarized_ids") or [])
    our_ids = set(m.id for m in to_summarize)
    # 如果这批消息已被其他进程摘要 → 跳过
    if our_ids.issubset(latest_summarized_ids):
        logger.debug("消息已被其他进程摘要,跳过 session=%s", session.id)
        return

    # 合并：在最新数据基础上追加我们的摘要
    summaries: list[dict] = list(latest_ctx.get("summaries") or [])
    summaries.append({
        "text": summary_text,
        "range": [to_summarize[0].id, to_summarize[-1].id],
        "count": len(to_summarize),
        "tokens": accumulated,
    })

    # v3.1: 超级摘要压缩（替代直接截断丢弃）
    if len(summaries) > SUPER_SUMMARY_TRIGGER:
        summaries = await _compress_super_summary(summaries)

    latest_summarized_ids.update(our_ids)

    latest_ctx["summaries"] = summaries
    latest_ctx["summarized_ids"] = list(latest_summarized_ids)
    # 拼接总摘要供 _layer1 使用
    latest_ctx["summary"] = "\n\n".join(s["text"] for s in summaries)
    session.shared_context = latest_ctx
    await db.flush()
    logger.info(
        "会话 %s 生成摘要: %d 条消息 %d tokens -> %d 字符 (摘要总数=%d, 窗口=%dK)",
        session.id, len(to_summarize), accumulated, len(summary_text),
        len(summaries), context_window // 1000,
    )


async def build_main_chat_context(
    db: AsyncSession, session: "Session", current_content: str,
) -> list[ChatMessage]:
    """为群聊意图识别/回复构建上下文。

    v3.3: 窗口大小基于 Leader 模型的 context_window 动态计算。
    返回：system(含摘要) + 最近窗口内历史 + 当前用户消息。
    """
    # v3.3: 解析 Leader context_window
    context_window = await _resolve_leader_context_window(db, session)
    window_budget = get_main_window_budget(context_window)

    # 先尝试更新摘要
    await maybe_summarize_main_session(db, session, context_window=context_window)

    ctx = session.shared_context or {}
    if not isinstance(ctx, dict):
        ctx = {}
    summary_text = ctx.get("summary") or ""

    # v3.3: 取全部未摘要消息，用 token 预算贪心选取
    all_msgs = await _fetch_main_messages(db, session.id, limit=200)
    summarized_ids = set(ctx.get("summarized_ids") or [])
    unsummarized = [m for m in all_msgs if m.id not in summarized_ids]

    # Token-budget 选取：从最新向前贪心，直到预算耗尽
    recent, _ = select_messages_by_token_budget(
        unsummarized, window_budget, min_keep=MIN_MESSAGES_KEEP,
    )

    sys_parts = ["You are the Leader in the group chat, responsible for understanding user intent and coordinating the team."]
    if summary_text:
        sys_parts.append("\n## History Summary (earlier conversation compressed)")
        sys_parts.append(summary_text)
    sys_parts.append("\n## Recent Group Chat Activity")
    if recent:
        for m in recent[:-1]:  # 最后一条是当前用户消息，单独放 user
            speaker = m.sender_type
            if m.sender_type == "agent":
                speaker = m.content.get("agent_name") or f"agent#{m.sender_id}"
            elif m.sender_type == "user":
                speaker = "user"
            text = m.content.get("text") or m.content.get("note") or ""
            if text:
                sys_parts.append(f"[{speaker}] {text}")
    else:
        sys_parts.append("(会话刚开始)")

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content="\n".join(sys_parts)),
        ChatMessage(role="user", content=current_content),
    ]
    return messages


async def build_leader_context_lines(
    db: AsyncSession, session: "Session",
) -> tuple[str, list[str]]:
    """v3.3: 为 Leader 编排构建群聊上下文（摘要 + token 预算窗口）。

    返回 (摘要文本, 上下文行列表)，供 chat_handler 拼入 Leader prompt。
    窗口大小基于 Leader 模型的 context_window 动态计算。
    """
    # v3.3: 解析 Leader context_window
    context_window = await _resolve_leader_context_window(db, session)
    window_budget = get_main_window_budget(context_window)

    # 先尝试更新摘要
    try:
        await maybe_summarize_main_session(db, session, context_window=context_window)
    except Exception:
        logger.debug("主会话摘要更新失败(非阻塞)", exc_info=True)

    ctx = session.shared_context or {}
    if not isinstance(ctx, dict):
        ctx = {}
    summary_text = ctx.get("summary") or ""

    # v3.3: token 预算贪心选取
    all_msgs = await _fetch_main_messages(db, session.id, limit=200)
    summarized_ids = set(ctx.get("summarized_ids") or [])
    unsummarized = [m for m in all_msgs if m.id not in summarized_ids]
    recent, _ = select_messages_by_token_budget(
        unsummarized, window_budget, min_keep=MIN_MESSAGES_KEEP,
    )

    context_lines: list[str] = []
    for m in recent:
        role = "用户" if m.sender_type == SenderType.USER else "agent"
        if isinstance(m.content, dict):
            if m.msg_type == MsgType.TOOL_CALL:
                tool = m.content.get("tool", "")
                text = f"[调用工具 {tool}]"
            elif m.msg_type == MsgType.TOOL_RESULT:
                tool = m.content.get("tool", "")
                ok = "成功" if m.content.get("ok") else "失败"
                output = (m.content.get("output") or "")[:150]
                text = f"[工具结果 {tool} {ok}: {output}]"
            else:
                text = (m.content.get("text") or m.content.get("note") or "")
        else:
            text = str(m.content)
        text = (text or "").strip()[:2000]
        if text:
            speaker_name = ""
            if m.sender_type == SenderType.AGENT:
                speaker_name = m.content.get("agent_name", "") if isinstance(m.content, dict) else ""
            context_lines.append(f"[{role}{': ' + speaker_name if speaker_name else ''}] {text}")

    return summary_text, context_lines


async def build_thread_context_with_window(
    db: AsyncSession, session_id: int, thread_id: int,
    context_window: int | None = None,
) -> list[ChatMessage]:
    """子会话(thread)滑动窗口：按 token 预算保留最近消息。

    v3.3: 窗口大小基于 Agent 模型的 context_window 动态计算。
    - context_window 由调用方传入（来自 agent_runtime 的 per-agent 解析）
    - 未传入时使用默认值 500K
    - 使用 token 预算贪心选取，短消息多保留，长工具输出自然挤出
    """
    from app.orchestration.token_counter import get_thread_window_budget

    if context_window is None:
        context_window = _DEFAULT_CTX_WINDOW_FALLBACK
    window_budget = get_thread_window_budget(context_window)

    # 取较多的消息，再用 token 预算筛选
    msgs = await session_service.list_thread_messages(
        db, session_id=session_id, thread_id=thread_id, limit=200,
    )

    # v3.3: token 预算贪心选取
    selected, _ = select_messages_by_token_budget(
        msgs, window_budget, min_keep=MIN_MESSAGES_KEEP,
    )

    out: list[ChatMessage] = []
    for m in selected:
        if m.msg_type == MsgType.TOOL_CALL:
            # 序列化为 assistant 的 tool_call（让 LLM 能看到之前调用了什么工具）
            tool = m.content.get("tool", "")
            args = m.content.get("args", {})
            call_key = m.content.get("call_key", "")
            out.append(ChatMessage(
                role="assistant",
                content=f"调用了工具 {tool}",
                tool_calls=[{
                    "id": call_key,
                    "name": tool,
                    "arguments": args,
                }],
            ))
        elif m.msg_type == MsgType.TOOL_RESULT:
            # 序列化为 tool 的返回结果（让 LLM 能看到之前的工具输出）
            tool = m.content.get("tool", "")
            ok = m.content.get("ok", True)
            output = m.content.get("output", "")
            error = m.content.get("error", "")
            call_key = m.content.get("call_key", "")
            content = output if ok else f"Error: {error}"
            # 截断过长的工具结果（配合 micro_compact 在 agent loop 中进一步压缩）
            if len(content) > 2000:
                content = content[:2000] + "\n...(已截断)"
            out.append(ChatMessage(
                role="tool",
                content=content,
                name=tool,
                tool_call_id=call_key,
            ))
        else:
            # 文本消息（TEXT / TASK_CARD 等）
            text = m.content.get("text") or m.content.get("note") or ""
            if not text:
                continue
            role = "assistant" if m.sender_type == "agent" else "user"
            out.append(ChatMessage(role=role, content=text))
    return out


async def search_session_memory(
    db: AsyncSession, session_id: int, keyword: str, limit: int = 10,
) -> "list[Message]":
    """按关键词检索会话历史消息(主群 + 所有 thread)。

    供 memory.search 工具调用，让 AI 按需检索更早的历史。
    """
    from app.persistence.models.message import Message

    keyword = keyword.strip()
    if not keyword:
        return []

    # 用 LIKE 做简单全文检索（SQLite/PG 通用）
    pattern = f"%{keyword}%"
    res = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .where(Message.content["text"].as_string().like(pattern))
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list(reversed(res.scalars().all()))

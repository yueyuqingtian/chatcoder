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

from app.core.config import resolve_workspace_root
from app.core.enums import MsgType, SenderType
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.prompts import (
    SUMMARY_CLOSE_TAG,
    SUMMARY_OPEN_TAG,
    build_compaction_prompt,
    get_checkpoint_preamble,
)
from app.orchestration.token_counter import (
    estimate_message_tokens_from_model,
    get_agent_context_window,
    get_compact_target_max_tokens,
    get_compact_target_tokens,
    messages_token_total,
)

logger = logging.getLogger(__name__)

# 参与压缩的消息类型（plan-19-82 步骤5：纳入 THINKING——此前既不参与压缩候选，
# 重建历史时又被保留，导致 thinking 占用被永久保留、压缩后占用居高不下）。
_COMPACT_KEEP_TYPES = {
    MsgType.TEXT.value, MsgType.TOOL_CALL.value, MsgType.TOOL_RESULT.value,
    MsgType.THINKING.value,
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

def _fallback_summary(messages: list, language: str = "auto") -> str:
    """硬编码降级摘要（LLM 不可用/失败时）。

    plan-19-82: 固定文案按语言选择（中文会话用中文文案，避免英文摘要污染回复语言）。
    """
    from collections import defaultdict

    zh = language == "zh"
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
    lines = ["以下是之前对话的摘要：" if zh else "Summary of earlier conversation:"]
    if tool_names:
        counts = defaultdict(int)
        for n in tool_names:
            counts[n] += 1
        items = ", ".join(f"{k}({v}次)" if zh else f"{k} x{v}" for k, v in counts.items())
        lines.append(("已调用工具: " if zh else "Tools called: ") + items)
    if files:
        lines.append(("涉及文件: " if zh else "Files involved: ") + ", ".join(sorted(files)[:20]))
    if parts:
        lines.append(("关键内容:\n" if zh else "Key content:\n") + "\n".join(parts[:30]))
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


async def _summarize_with_llm(db: AsyncSession, provider, messages: list, max_chars: int = 4000,
                              workspace_dir: str | None = None, language: str = "auto") -> str:
    """LLM 生成结构化 checkpoint 摘要。

    优先 KV 缓存复用路径：把待压缩消息结构化为 user/assistant/tool 重放序列
    （保留工具调用结构信息，比纯文本 transcript 摘要质量更高）；
    provider/LLM 失败时降级为硬编码摘要。

    plan-19-82：摘要语言由 language 参数**无条件**指定（旧版是让模型自行判断），
    避免中文会话产出英文 checkpoint 进而把回复语言带向英文。
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
                ChatMessage(role="system", content=build_compaction_prompt(language)),
                *replay,
                ChatMessage(
                    role="user",
                    content=("请按上述结构输出 checkpoint 摘要，不要调用任何工具。"
                             if language == "zh" else
                             "Output the checkpoint summary following the structure above. "
                             "Do not call any tool."),
                ),
            ],
            model="",
            temperature=0.3,
            # plan-270-1358: ta3 x-ws-id 需要工作目录指纹（其它 Provider 忽略）
            workspace_dir=workspace_dir,
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
    language: str = "auto",
    fixed_overhead_tokens: int | None = None,
) -> dict | None:
    """执行一次落库式压缩事务——**目标闭环**（plan-19-82 步骤5）。

    流程：算固定开销 → 迭代「选范围 → 摘要 → 收缩候选」直到
    「压缩后估算总占用 ≤ 目标区间上界」或「无可压缩范围」或「达迭代上限」→
    多轮结果滚动合并为一条活跃 checkpoint → 落库 + 更新 shared_context。

    目标区间（可配置，默认 10%-15%）：
    - `settings.compact_target_ratio`（默认 0.12）= 压缩后目标占用；
    - `settings.compact_target_max_ratio`（默认 0.15）= 达标线（超过则继续压）；
    - `settings.compact_max_rounds`（默认 5）= 单次压缩最大迭代轮数。

    旧实现只切一刀（retain=窗口 16%），压缩后仍可能占 40%（512K 窗口），
    与用户「应回落到 10%-15%」的诉求不符。

    Args:
        language: 本轮回复语言（zh/en/auto），摘要与 checkpoint 文案按此生成。

    Returns:
        压缩结果 dict（含 compaction_id / shadowed_ids / saved_tokens /
        post_compact_ratio / rounds / overhead_over_target 等）；
        无可压缩范围或收益不足时返回 None。
    """
    from app.orchestration.context_memory import _fetch_main_messages, _is_image_message

    ctx = session.shared_context or {}
    if not isinstance(ctx, dict):
        ctx = {}
    summarized_ids = set(ctx.get("summarized_ids") or [])
    compacted_ids = set(ctx.get("compacted_ids") or [])

    all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
    # 候选 = 未摘要未压缩、类型可压缩的主线程消息
    # plan-166-767: 跳过含图片附件的消息——图片一旦被遮蔽（compacted_ids）无法恢复，
    # 切回多模态模型后历史图片块将丢失。
    candidates = [
        m for m in all_msgs
        if m.id not in summarized_ids and m.id not in compacted_ids
        and m.msg_type in _COMPACT_KEEP_TYPES
        and not _is_image_message(m)
    ]
    if len(candidates) < 4:
        logger.debug("[compressor] session=%s 候选消息 %d 条过少，跳过压缩", session.id, len(candidates))
        return None

    # 收益检查：可回收 token 必须大于压缩自身成本（LLM 摘要输出 + 重建开销）
    total_tokens = messages_token_total(candidates)
    min_reclaim = max(2000, int(context_window * 0.05))
    if total_tokens <= min_reclaim:
        logger.debug(
            "[compressor] session=%s 候选 %d tokens <= 最小回收 %d，跳过",
            session.id, total_tokens, min_reclaim,
        )
        return None

    # ── 目标闭环预算核算 ──
    # plan-19-82：固定开销（system/developer/工具规则等，压缩不会动它们）先扣除，
    # 否则会出现「压不动却反复压缩」的空转。
    target_tokens = get_compact_target_tokens(context_window)          # 12%
    target_max_tokens = get_compact_target_max_tokens(context_window)  # 15%（达标线）
    # 固定开销优先取调用方（agent_loop）实测值，缺省回退会话记录/0
    if fixed_overhead_tokens is None:
        fixed_overhead = _estimate_fixed_overhead(session)
    else:
        fixed_overhead = max(0, int(fixed_overhead_tokens))
    overhead_over_target = fixed_overhead >= target_max_tokens
    if overhead_over_target:
        logger.warning(
            "[compressor] session=%s 固定开销 %d tokens 已 >= 目标上界 %d（窗口 %d），"
            "退化为尽力压缩（提示用户精简系统规则/工具）",
            session.id, fixed_overhead, target_max_tokens, context_window,
        )
    # 可压缩预算 = 目标上界 − 固定开销；低于此 token 量的候选可保留（不必再压）
    compressible_budget = max(0, target_max_tokens - fixed_overhead)

    # 尾部保留预算：缺省取目标预算（让压缩后总占用逼近目标 12%），
    # 兼容旧调用方传入的 retain_tokens。
    if retain_tokens is None:
        retain_tokens = max(4000, min(target_tokens, compressible_budget or target_tokens))

    # ── 迭代压缩：每轮压掉一段较早候选，直到总占用落入目标上界 ──
    from app.core.config import settings as _settings

    max_rounds = max(1, int(getattr(_settings, "compact_max_rounds", 5) or 5))
    remaining = list(candidates)
    shadowed_all: list = []
    summaries: list[str] = []
    rounds = 0
    while rounds < max_rounds:
        # 剩余候选总量已 <= 可压缩预算 → 达标，停止
        if compressible_budget > 0 and messages_token_total(remaining) <= compressible_budget:
            break
        span = select_compactable_range(remaining, retain_tokens)
        if span is None:
            break
        start_idx, end_idx = span
        chunk = remaining[start_idx:end_idx + 1]
        if not chunk:
            break
        summary_text = ""
        if provider is None:
            summary_text = _fallback_summary(chunk, language=language)
        else:
            summary_text = await _summarize_with_llm(
                db, provider, chunk, max_chars=max_summary_chars,
                workspace_dir=resolve_workspace_root(getattr(session, "workspace_root", None)),
                language=language,
            )
            if not summary_text:
                summary_text = _fallback_summary(chunk, language=language)
        summary_text = summary_text.strip()
        if not summary_text:
            logger.warning("[compressor] session=%s 第 %d 轮摘要为空，停止迭代", session.id, rounds + 1)
            break
        shadowed_all.extend(chunk)
        summaries.append(summary_text)
        # 收缩候选：移除本轮已压缩的 span（保留区保留）
        remaining = remaining[:start_idx] + remaining[end_idx + 1:]
        rounds += 1
        if not remaining:
            break

    if not shadowed_all:
        logger.debug("[compressor] session=%s 无可压缩范围(retain=%d)", session.id, retain_tokens)
        return None

    # ── 滚动合并：多轮摘要合并为一条活跃 checkpoint（plan-19-82 步骤5） ──
    if len(summaries) == 1:
        summary_text = summaries[0]
    else:
        merged_head = ("以下是分多轮压缩的历史摘要，已合并为单一检查点：\n\n" if language == "zh"
                       else "Consolidated checkpoint from multiple compaction rounds:\n\n")
        summary_text = merged_head + "\n\n".join(
            f"<!-- round {i + 1} -->\n{s}" for i, s in enumerate(summaries)
        )

    shadowed_ids = [m.id for m in shadowed_all]
    shadowed_tokens = messages_token_total(shadowed_all)
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

    frame_text = (f"{get_checkpoint_preamble(language)}\n\n"
                  f"{SUMMARY_OPEN_TAG}\n{summary_text}\n{SUMMARY_CLOSE_TAG}")
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
            "rounds": rounds,
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
    # ── checkpoint 滚动合并（plan-19-82 步骤5）──
    # 活跃（未 restored / 未 merged）块超过阈值时，把较旧的标记 merged=true：
    # 其原文仍可经 compaction_view 按需回看，但不再重复注入 checkpoint，
    # 避免多次压缩后历史 checkpoint 之和本身就很可观（固定开销膨胀）。
    _merge_limit = 2
    _active = [c for c in latest_compactions
               if not c.get("restored") and not c.get("merged")]
    if len(_active) >= _merge_limit:
        for _c in _active[:len(_active) - (_merge_limit - 1)]:
            _c["merged"] = True
            _c["merged_into"] = "latest"
    latest_compactions.append({
        "compaction_id": compaction_id,
        "index": compaction_index,
        "summary_message_id": summary_msg.id,
        "shadowed_ids": shadowed_ids,
        "shadowed_tokens": shadowed_tokens,
        "saved_tokens": saved_tokens,
        "rounds": rounds,
        "trigger": trigger,
        "created_at": str(summary_msg.created_at),
    })
    new_ctx = dict(latest_ctx)
    new_ctx["compacted_ids"] = sorted(latest_compacted)
    new_ctx["compactions"] = latest_compactions
    # shared_context 写入经 WriteEngine 单写线程（无锁单写者；async db 不再持有写事务）
    from app.persistence.database import run_write_locked

    def _persist_ctx(s):
        from app.persistence.models.message import Session as _Sess
        row = s.get(_Sess, session.id)
        if row is not None:
            row.shared_context = new_ctx
            s.commit()

    await run_write_locked(_persist_ctx, label="compact.shared_ctx")

    # 压缩后总占用估算：未压缩候选 + 固定开销 + 新 checkpoint 摘要
    remaining_tokens = messages_token_total(remaining)
    summary_tokens = max(1, len(summary_text.encode("utf-8")) // 4)
    post_tokens = remaining_tokens + fixed_overhead + summary_tokens
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
        # plan-19-82: 目标闭环验收字段
        "rounds": rounds,
        "fixed_overhead_tokens": fixed_overhead,
        "target_tokens": target_tokens,
        "target_max_tokens": target_max_tokens,
        "post_compact_tokens": post_tokens,
        "post_compact_ratio": round(post_tokens / context_window * 100, 1) if context_window else None,
        "target_reached": post_tokens <= target_max_tokens,
        "overhead_over_target": overhead_over_target,
        # plan-238-1188: 补入 summary_tokens——下方日志引用了该键，此前缺失会在
        # 写库全部完成后抛 KeyError，被 agent_loop 当成“落库式压缩失败”而叠加执行
        # 内存式压缩（上下文被双重压缩、占用直接跌到 4%）。
        "summary_tokens": summary_tokens,
    }
    logger.info(
        "[compressor] session=%s 压缩完成: index=%d rounds=%d %d 条消息 %d tokens -> checkpoint(%d tokens)，"
        "固定开销 %d，压缩后约 %d tokens (%.1f%%)，目标 %d~%d，达标=%s (trigger=%s)",
        session.id, compaction_index, rounds, len(shadowed_ids), shadowed_tokens, summary_tokens,
        fixed_overhead, post_tokens, result["post_compact_ratio"] or 0,
        target_tokens, target_max_tokens, result["target_reached"], trigger,
    )
    return result


def _estimate_fixed_overhead(session) -> int:
    """估算本会话不可压缩的固定开销（plan-19-82 步骤5）。

    由「当前上下文构建口径」近似：系统提示 + developer 分层上下文 + 工具 schema。
    这里用可测的部分保守估算——系统提示/规则/记忆等 developer 段长度无法在压缩器内
    直接拿到消息列表，故以会话级 shared_context 记录的历史值 + 最近一次总结为准；
    拿不到时退化为 0（此时按纯候选量判定，行为接近旧实现，不会更差）。
    """
    try:
        ctx = getattr(session, "shared_context", None) or {}
        if isinstance(ctx, dict):
            v = ctx.get("fixed_overhead_tokens")
            if isinstance(v, int) and v >= 0:
                return v
    except Exception:  # noqa: BLE001
        pass
    return 0


async def emergency_compact_session(
    db: AsyncSession, *, session, provider, context_window: int,
    used_tokens: int | None = None,
    agent_id: int | None = None,
    agent_name: str = "main",
    turn_id: int | None = None,
    language: str = "auto",
) -> dict | None:
    """溢出恢复压缩（trigger='context-overflow'）。

    与 pressure 压缩的区别：不按目标比例保留预算，只保留最近 6 个工具回合
    （对齐旧 emergency_compact 语义），强制做一次有效缩减。
    plan-19-82：新增 language 参数，摘要与 checkpoint 文案跟随本轮语言。
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
        trigger="context-overflow", language=language,
    )


async def _commit_compaction(
    db: AsyncSession, *, session, provider, shadowed: list,
    context_window: int, used_tokens: int | None,
    agent_id: int | None, agent_name: str, turn_id: int | None, trigger: str,
    language: str = "auto",
) -> dict | None:
    """共享提交逻辑：摘要 → SUMMARY 消息 → compacted_ids → 广播。

    plan-19-82：language 参数控制摘要语言与 checkpoint 前言语言。
    """
    shadowed_ids = [m.id for m in shadowed]
    shadowed_tokens = messages_token_total(shadowed)

    if provider is None:
        summary_text = _fallback_summary(shadowed, language=language)
    else:
        summary_text = await _summarize_with_llm(
            db, provider, shadowed,
            workspace_dir=resolve_workspace_root(getattr(session, "workspace_root", None)),
            language=language)
        if not summary_text:
            summary_text = _fallback_summary(shadowed, language=language)
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

    frame_text = (f"{get_checkpoint_preamble(language)}\n\n"
                  f"{SUMMARY_OPEN_TAG}\n{summary_text}\n{SUMMARY_CLOSE_TAG}")
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
    # plan-19-82 步骤5：溢出恢复路径同样做 checkpoint 滚动合并（避免累积膨胀）
    _merge_limit = 2
    _active = [c for c in latest_compactions
               if not c.get("restored") and not c.get("merged")]
    if len(_active) >= _merge_limit:
        for _c in _active[:len(_active) - (_merge_limit - 1)]:
            _c["merged"] = True
            _c["merged_into"] = "latest"
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
    # shared_context 写入经 WriteEngine 单写线程（无锁单写者；async db 不再持有写事务）
    from app.persistence.database import run_write_locked

    def _persist_ctx(s):
        from app.persistence.models.message import Session as _Sess
        row = s.get(_Sess, session.id)
        if row is not None:
            row.shared_context = new_ctx
            s.commit()

    await run_write_locked(_persist_ctx, label="compact.shared_ctx")

    summary_tokens = max(1, len(summary_text.encode("utf-8")) // 4)
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
        # plan-19-82: 与 compact_session 口径一致，供 agent_loop 计算 post_compact_ratio
        "post_compact_tokens": shadowed_tokens - saved_tokens,
        "post_compact_ratio": (
            round((shadowed_tokens - saved_tokens) / context_window * 100, 1)
            if context_window else None
        ),
        # plan-238-1188: 同 compact_session——日志引用 summary_tokens，缺失会抛 KeyError。
        "summary_tokens": summary_tokens,
    }
    logger.info(
        "[compressor] session=%s %s 压缩完成: index=%d %d 条消息 %d tokens -> %d tokens，节省 %d",
        session.id, trigger, compaction_index, len(shadowed_ids), shadowed_tokens, summary_tokens, saved_tokens,
    )
    return result

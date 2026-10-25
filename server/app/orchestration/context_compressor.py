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
from app.models.base import resolve_reasoning
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
# LLM 摘要连续失败达到该次数后，同一次压缩内不再调用 LLM（参照 zcode 的压缩熔断）
_LLM_FAIL_LIMIT = 3

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


def _collect_attachment_lines(messages: list) -> list[str]:
    """从被压缩消息里提取附件清单（文件名 + 绝对路径）。

    plan-527-2218：压缩后正文只剩摘要，附件路径若不写进 checkpoint，模型既不知道
    有这些文件、也没有路径可重读——这正是「压缩后彻底忘了早期发过的文档」的直接原因。
    """
    try:
        from app.orchestration.context_manager import _attachment_abs_path
    except Exception:  # noqa: BLE001 — 路径工具不可用时退化为原始相对路径
        def _attachment_abs_path(rel: str) -> str:  # type: ignore[misc]
            return rel

    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for m in messages:
        if m.msg_type != MsgType.TEXT.value:
            continue
        content = m.content if isinstance(m.content, dict) else {}
        atts = content.get("attachments") or []
        if not isinstance(atts, list):
            continue
        for a in atts:
            if not isinstance(a, dict):
                continue
            rel = str(a.get("path") or "")
            if not rel:
                continue
            abs_path = str(_attachment_abs_path(rel))
            name = str(a.get("filename") or rel)
            key = (name, abs_path)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- {name}: `{abs_path}` (message #{m.id})")
    return lines


def _build_checkpoint_frame(summary_text: str, language: str, shadowed: list) -> str:
    """拼接 checkpoint 帧：前言 + 摘要 + 附件清单。

    附件清单无条件写入，不依赖 LLM 是否在摘要里提到——压缩后正文消失，
    路径是模型重新读取早期文档的唯一线索。
    """
    frame = (f"{get_checkpoint_preamble(language)}\n\n"
             f"{SUMMARY_OPEN_TAG}\n{summary_text}\n{SUMMARY_CLOSE_TAG}")
    att_lines = _collect_attachment_lines(shadowed)
    if att_lines:
        title = ("## 已压缩区间的附件清单（需要原文时用 read_attachment 按路径重读）"
                 if language == "zh" else
                 "## Attachments in the compacted span (re-read via read_attachment)")
        frame += "\n\n" + title + "\n" + "\n".join(att_lines)
    return frame


def compute_compacted_floor(
    all_msgs: list, compacted_ids: set[int], prev_exceptions: list[int] | None = None,
) -> tuple[int, list[int]]:
    """计算压缩水位与例外集合（plan-527-2218）。

    - 水位 ``floor`` = 已被遮蔽的最大消息 id。``id > floor`` 的消息一条都没被压缩，
      可以从 SQL 下界直接查起，已压缩的整段历史不必读进内存。
    - 例外 ``exceptions`` = 水位以下但**没有**被遮蔽的消息 id。主要是含图片附件的消息
      （按 plan-166-767 不进压缩候选）：它们必须随下界查询一起带回，否则会从上下文
      静默消失。数量取决于会话里的图片消息，通常个位数。

    查询条件因此是 ``id > floor OR id IN (exceptions)``：大段已压缩历史整段跳过，
    少量例外单独取回，兼顾正确性与成本。

    早先实现用「未被遮蔽的最小 id - 1」，一旦最老消息恰好是图片消息，水位就永远停在
    它前面、优化彻底失效（会话 464 正是如此）。改为最大遮蔽 id 后水位能正常推进。
    """
    if not compacted_ids:
        return 0, []
    floor = max(compacted_ids)
    prev = {i for i in (prev_exceptions or []) if isinstance(i, int) and i <= floor}
    cur = {m.id for m in all_msgs if m.id <= floor and m.id not in compacted_ids}
    return floor, sorted(prev | cur)


def _reasoning_cutoff(messages: list) -> int:
    """返回「旧 reasoning 分界线」索引：< cutoff 的 thinking 出站时会被剥离。

    从尾部向前数第 _REASONING_KEEP_TOOL_ROUNDS 个 tool_call 的位置；
    工具回合不足时返回 0（即全部 thinking 都在保留窗口内，不剥离）。
    """
    from app.orchestration.compaction import _REASONING_KEEP_TOOL_ROUNDS

    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].msg_type == MsgType.TOOL_CALL.value:
            seen += 1
            if seen >= _REASONING_KEEP_TOOL_ROUNDS:
                return i
    return 0


def estimate_effective_tokens(messages: list) -> int:
    """估算「剥离旧 reasoning 后」实际发往模型的 token 数（plan-282-0）。

    压缩器的核算必须与真实出站内容一致，否则会出现「自报达标、真实超限」：
    build_api_copy 每次请求都会剥离较旧工具回合的 reasoning_content，
    但压缩器此前按「全量 thinking」核算候选与保留区，两边口径不同。

    此处镜像同一策略（仅保留最近 _REASONING_KEEP_TOOL_ROUNDS 个工具回合的
    thinking），使 post_compact_ratio 与保留区预算都反映真实占用。

    注意：保留窗口内的 thinking 仍全额计入（原样回传），只有更早的被排除。

    plan-527-2218：改为统一委托 `token_counter.estimate_outbound_tokens_db`。
    此前这里只剥离 thinking、不折算工具结果截断，与真实出站仍有系统性偏差；
    现在压缩器、前置压缩判断、重建预算共用同一口径。
    """
    from app.orchestration.token_counter import estimate_outbound_tokens_db

    return estimate_outbound_tokens_db(messages)


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
    # plan-282-0: 用「有效口径」累计——旧工具回合的 thinking 出站时会被剥离
    # （见 compaction.strip_stale_reasoning），若仍按全量 thinking 计预算，保留区
    # 会被这些「不占真实体积」的 thinking 白吃满，把真正的对话内容挤进压缩区，
    # 出现「百分比好看、AI 却丢了上下文」的伪优化。
    cutoff = _reasoning_cutoff(messages)
    keep_from = len(messages)
    acc = 0
    for i in range(len(messages) - 1, -1, -1):
        if i < cutoff and messages[i].msg_type == MsgType.THINKING.value:
            acc += 4  # 剥离后仅剩结构开销
        else:
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


# plan-548-2277：压缩的同一次调用顺带产出一份「会话记忆」。
# 分工：摘要每轮都进上下文，必须短（≤3000 字符、8 段）；记忆只在需要时被检索，可以详细。
_MAX_SESSION_MEMORIES = 12
_MAX_MEMORY_TEXT = 1200
_MIN_MEMORY_TEXT = 20
_MEMORY_KINDS = ("fact", "convention", "pitfall", "decision")


def _parse_session_memories(block: str) -> list[dict]:
    """解析 <session-memory> 里的 JSON 数组；任何非法情况都返回空列表。

    记忆是压缩的附加收益，绝不能因为它让压缩失败——所以这里全程吞异常。
    """
    import json
    import re

    block = (block or "").strip()
    if not block:
        return []
    data = None
    try:
        data = json.loads(block)
    except Exception:
        match = re.search(r"\[.*\]", block, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if len(text) < _MIN_MEMORY_TEXT:
            continue
        kind = str(item.get("kind") or "fact")
        if kind not in _MEMORY_KINDS:
            kind = "fact"
        kws = item.get("keywords")
        if isinstance(kws, str):
            kws = [k.strip() for k in re.split(r"[,，、;；\s]+", kws) if k.strip()]
        elif isinstance(kws, list):
            kws = [str(k).strip() for k in kws if str(k).strip()]
        else:
            kws = []
        out.append({"text": text[:_MAX_MEMORY_TEXT], "kind": kind, "keywords": kws[:8]})
        if len(out) >= _MAX_SESSION_MEMORIES:
            break
    return out


def _split_summary_and_memories(text: str, max_chars: int) -> tuple[str, list[dict]]:
    """把模型输出拆成 (checkpoint 摘要, 会话记忆列表)。

    模型只给摘要（没有 <session-memory> 段）是兼容路径，不是错误：
    旧提示词、以及模型偶尔忘记第二段，都按「无记忆」处理，压缩照常成功。
    """
    from app.orchestration.prompts.summary import (
        SESSION_MEMORY_CLOSE_TAG,
        SESSION_MEMORY_OPEN_TAG,
    )

    raw = str(text or "")
    start = raw.find(SESSION_MEMORY_OPEN_TAG)
    if start < 0:
        return raw.strip()[:max_chars], []
    end = raw.find(SESSION_MEMORY_CLOSE_TAG, start + len(SESSION_MEMORY_OPEN_TAG))
    block = raw[start + len(SESSION_MEMORY_OPEN_TAG): end if end >= 0 else len(raw)]
    body = raw[:start]
    if end >= 0:
        body += raw[end + len(SESSION_MEMORY_CLOSE_TAG):]
    return body.strip()[:max_chars], _parse_session_memories(block)


async def _summarize_with_llm(db: AsyncSession, provider, messages: list, max_chars: int = 4000,
                              workspace_dir: str | None = None,
                              language: str = "auto") -> tuple[str, list[dict]]:
    """LLM 生成结构化 checkpoint 摘要。

    优先 KV 缓存复用路径：把待压缩消息结构化为 user/assistant/tool 重放序列
    （保留工具调用结构信息，比纯文本 transcript 摘要质量更高）；
    provider/LLM 失败时降级为硬编码摘要。

    plan-19-82：摘要语言由 language 参数**无条件**指定（旧版是让模型自行判断），
    避免中文会话产出英文 checkpoint 进而把回复语言带向英文。

    plan-548-2277：返回值改为 (摘要, 会话记忆列表)。拆分失败时记忆为空列表，
    摘要仍回退为整段响应——压缩永远不因记忆解析失败而失败。
    """
    if not messages:
        return "", []
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
            # plan-560-2311 A1：签名是 (摘要, 记忆) 二元组，返回单字符串会让调用方
            # 的 `summary_text, _memories = await ...` 解包直接抛 ValueError。
            return "", []

        # plan-53-258 R4: 辅助摘要请求同样携带用户配置的思考深度（此前完全不带）
        _effort, _thinking = resolve_reasoning(provider)
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
            reasoning_effort=_effort,
            thinking=_thinking,
            # plan-270-1358: ta3 x-ws-id 需要工作目录指纹（其它 Provider 忽略）
            workspace_dir=workspace_dir,
        )
        resp = await provider.chat(req)
        text = (resp.content or "").strip()
        if not text:
            return "", []
        return _split_summary_and_memories(text, max_chars)
    except Exception as e:
        logger.warning("[compressor] LLM 摘要失败，降级硬编码: %s", e)
        return "", []


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
        fixed_overhead_tokens: 调用方实测的不可压缩固定开销（system/developer/工具规则）。
            未传时按 0 处理——固定开销由调用方传入，不再从 shared_context 推断
            （plan-562-2318 A29）。

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
    # plan-527-2218: 压缩水位以下的区间已被遮蔽，SQL 层直接跳过（未被遮蔽的少量例外
    # 单独带回）；limit=None 不设条数上限，保证最早那批未压缩消息一定进入压缩候选。
    compacted_floor = int(ctx.get("compacted_floor") or 0)
    compacted_exceptions = [
        i for i in (ctx.get("compacted_exceptions") or []) if isinstance(i, int)
    ]

    all_msgs = await _fetch_main_messages(
        db, session.id, limit=None,
        min_id=compacted_floor, include_ids=compacted_exceptions)
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
    # plan-562-2318 A29：固定开销只认调用方（agent_loop）实测值，未传按 0 处理。
    # 旧实现缺省时回退读 shared_context["fixed_overhead_tokens"]，但全仓库没有任何
    # 写入点，该分支恒 0 → post_compact_tokens / post_compact_ratio 系统性偏低
    # （自报「已达标」，实际占用仍高）。既然拿不到真实值，就显式按 0 保守处理。
    fixed_overhead = max(0, int(fixed_overhead_tokens or 0))
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
    _llm_fail = 0  # 本次压缩内 LLM 连续失败次数（熔断用）
    # plan-548-2277：各轮摘要顺带产出的会话记忆，压缩成功后统一归档
    session_memories: list[dict] = []
    while rounds < max_rounds:
        # 剩余候选总量已 <= 可压缩预算 → 达标，停止
        # plan-282-0: 用「有效口径」（剥离旧 reasoning 后）判定，与真实出站一致。
        if compressible_budget > 0 and estimate_effective_tokens(remaining) <= compressible_budget:
            break
        span = select_compactable_range(remaining, retain_tokens)
        if span is None:
            break
        start_idx, end_idx = span
        chunk = remaining[start_idx:end_idx + 1]
        if not chunk:
            break
        summary_text = ""
        # plan-562-2318 A27：本 chunk 的记忆先暂存，等摘要确实被采纳（该 chunk 进入
        # shadowed_all）之后才并入 session_memories，见下方提交处。
        _chunk_memories: list[dict] = []
        _chunk_chars = sum(len(str(getattr(m, "content", "") or "")) for m in chunk)
        # 熔断：本次压缩内 LLM 连续失败达上限后，剩余轮次直接用规则摘要，不再反复调用
        if provider is None or _llm_fail >= _LLM_FAIL_LIMIT:
            summary_text = _fallback_summary(chunk, language=language)
        else:
            summary_text, _chunk_memories = await _summarize_with_llm(
                db, provider, chunk, max_chars=max_summary_chars,
                workspace_dir=resolve_workspace_root(getattr(session, "workspace_root", None)),
                language=language,
            )
            if summary_text:
                _llm_fail = 0
            else:
                _llm_fail += 1
                logger.warning("[compressor] session=%s LLM 摘要失败（连续 %d 次），本轮降级规则摘要",
                               session.id, _llm_fail)
                summary_text = _fallback_summary(chunk, language=language)
        summary_text = summary_text.strip()
        # 质量门：摘要不比原文短就没有压缩价值（反而增加占用）；先换规则摘要，仍不更短则放弃本轮
        if summary_text and _chunk_chars and len(summary_text) >= _chunk_chars:
            summary_text = _fallback_summary(chunk, language=language).strip()
            if summary_text and len(summary_text) >= _chunk_chars:
                logger.warning("[compressor] session=%s 摘要不短于原文，放弃本轮压缩", session.id)
                summary_text = ""
        if not summary_text:
            logger.warning("[compressor] session=%s 第 %d 轮摘要为空，停止迭代", session.id, rounds + 1)
            break
        shadowed_all.extend(chunk)
        summaries.append(summary_text)
        # plan-562-2318 A27：记忆必须与摘要采纳同进同退——摘要被质量门放弃时该 chunk
        # 不会被遮蔽、原文仍留在上下文里，若此时已把它的记忆并入 session_memories，
        # 压缩成功后记忆与上下文重复，下一轮压缩还会把同一批消息再归档一次。
        if _chunk_memories:
            session_memories.extend(_chunk_memories)
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

    # plan-548-2277：压缩同一次调用产出的会话记忆先落库（origin=compaction）。
    # 失败只记日志——记忆是附加收益，压缩本身已经成功，不能因它回滚或报错。
    session_memory_count = 0
    if session_memories:
        try:
            from app.services import memory_service

            session_memory_count = await memory_service.save_memories(
                db, session_id=session.id, turn_id=turn_id,
                memories=[{**m, "scope": "session"} for m in session_memories],
                origin="compaction",
            )
        except Exception:
            logger.warning("[compressor] 会话记忆落库失败(非阻塞)", exc_info=True)

    # 落库：SUMMARY 消息（checkpoint 帧）
    from app.services.message_service import create_message

    # plan-527-2218: 帧内含附件清单，保证压缩后仍能按路径重读早期文档
    frame_text = _build_checkpoint_frame(summary_text, language, shadowed_all)
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
            # plan-548-2277：本次归档的会话记忆条数（前端压缩卡展示入口，详情可检索）
            "session_memory_count": session_memory_count,
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
    # plan-527-2218: 更新压缩水位与例外——下次重建与压缩只查 `id > floor` 的消息，
    # 水位以下未被遮蔽的少量例外（图片消息）单独带回，不再全量读再内存过滤。
    _floor, _exceptions = compute_compacted_floor(
        all_msgs, latest_compacted, latest_ctx.get("compacted_exceptions"))
    new_ctx["compacted_floor"] = _floor
    new_ctx["compacted_exceptions"] = _exceptions
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
    # plan-282-0: 用「有效口径」——旧 reasoning 出站时会被剥离，不计入真实占用，
    # 否则自报占用虚高、与实际不符（反向也会因漏算而虚低）。
    remaining_tokens = estimate_effective_tokens(remaining)
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


async def emergency_compact_session(
    db: AsyncSession, *, session, provider, context_window: int,
    used_tokens: int | None = None,
    agent_id: int | None = None,
    agent_name: str = "main",
    turn_id: int | None = None,
    language: str = "auto",
    fixed_overhead_tokens: int | None = None,
) -> dict | None:
    """溢出恢复压缩（trigger='context-overflow'）。

    与 pressure 压缩的区别：不按目标比例保留预算，只保留最近 6 个工具回合
    （对齐旧 emergency_compact 语义），强制做一次有效缩减。
    plan-19-82：新增 language 参数，摘要与 checkpoint 文案跟随本轮语言。
    plan-560-2311 A4：fixed_overhead_tokens 与 compact_session 同源（调用方实测的
    固定开销），保证两条路径的 post_compact_ratio 口径一致。
    """
    from app.orchestration.context_memory import _fetch_main_messages, _is_image_message

    ctx = session.shared_context or {}
    if not isinstance(ctx, dict):
        ctx = {}
    summarized_ids = set(ctx.get("summarized_ids") or [])
    compacted_ids = set(ctx.get("compacted_ids") or [])

    # plan-527-2218: 与 pressure 路径同口径——水位以下跳过（例外单独带回）、不设条数上限，
    # 保证最早那批未压缩消息也能被溢出压缩覆盖。
    compacted_floor = int(ctx.get("compacted_floor") or 0)
    compacted_exceptions = [
        i for i in (ctx.get("compacted_exceptions") or []) if isinstance(i, int)
    ]
    all_msgs = await _fetch_main_messages(
        db, session.id, limit=None,
        min_id=compacted_floor, include_ids=compacted_exceptions)
    # plan-166-767: 跳过含图片附件的消息——图片一旦被遮蔽（compacted_ids）无法恢复，
    # 切回多模态模型后历史图片块将丢失。与 compact_session 的候选过滤同口径（plan-560-2311 A3）。
    candidates = [
        m for m in all_msgs
        if m.id not in summarized_ids and m.id not in compacted_ids
        and m.msg_type in _COMPACT_KEEP_TYPES
        and not _is_image_message(m)
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
        # plan-527-2218: 传本次取数全集，提交时据此计算压缩水位
        all_msgs=all_msgs,
        # plan-560-2311 A4：保留区（未被遮蔽的尾部候选）用于算压缩后真实占用
        retained_messages=candidates[k:],
        fixed_overhead_tokens=fixed_overhead_tokens,
    )


async def _commit_compaction(
    db: AsyncSession, *, session, provider, shadowed: list,
    context_window: int, used_tokens: int | None,
    agent_id: int | None, agent_name: str, turn_id: int | None, trigger: str,
    language: str = "auto",
    all_msgs: list | None = None,
    retained_messages: list | None = None,
    fixed_overhead_tokens: int | None = None,
) -> dict | None:
    """共享提交逻辑：摘要 → SUMMARY 消息 → compacted_ids → 广播。

    plan-19-82：language 参数控制摘要语言与 checkpoint 前言语言。
    plan-527-2218：all_msgs 为本次压缩的取数全集，用于计算压缩水位；缺省时不更新
    水位（宁可不优化，也不能把未被遮蔽的消息挡在下界之外）。
    plan-560-2311 A4：retained_messages / fixed_overhead_tokens 用于和 compact_session
    同口径估算压缩后真实占用（保留区 + 固定开销 + 摘要）；缺省时保留区按 0。
    plan-562-2318 A29：固定开销同样只认调用方入参，未传按 0 处理，不再从
    shared_context 推断（该键无任何写入点，读取只会静默恒 0）。
    """
    shadowed_ids = [m.id for m in shadowed]
    shadowed_tokens = messages_token_total(shadowed)

    session_memory_count = 0
    # plan-562-2318 A28：记忆先暂存在局部变量，等「摘要非空、压缩确实继续」之后再落库。
    emergency_memories: list[dict] = []
    if provider is None:
        summary_text = _fallback_summary(shadowed, language=language)
    else:
        summary_text, emergency_memories = await _summarize_with_llm(
            db, provider, shadowed,
            workspace_dir=resolve_workspace_root(getattr(session, "workspace_root", None)),
            language=language)
        if not summary_text:
            summary_text = _fallback_summary(shadowed, language=language)
    summary_text = summary_text.strip()
    if not summary_text:
        logger.warning("[compressor] session=%s 摘要为空，跳过压缩", session.id)
        return None

    # plan-549-2281 复盘修复：紧急压缩（overflow 路径）产出的会话记忆原先赋值后
    # 被静默丢弃——与主压缩路径（pressure）不一致，细节归档在这里断档。
    # plan-562-2318 A28：落库必须晚于「摘要为空则放弃」判断——否则压缩失败返回 None
    # 时记忆已经写进库，重试会重复写一遍。与 compact_session 的「先判成功再归档」同口径。
    # 与主路径同口径落库（origin=compaction，失败只记日志不回滚）。
    if emergency_memories:
        try:
            from app.services import memory_service

            session_memory_count = await memory_service.save_memories(
                db, session_id=session.id, turn_id=turn_id,
                memories=[{**m, "scope": "session"} for m in emergency_memories],
                origin="compaction",
            )
        except Exception:
            logger.warning("[compressor] 紧急压缩会话记忆落库失败(非阻塞)", exc_info=True)

    compaction_id = "cp-" + uuid.uuid4().hex[:12]
    saved_tokens = max(0, shadowed_tokens - len(summary_text.encode("utf-8")) // 4)

    # v30.1: 压缩块索引（先 refresh 拿最新 compactions，再落 SUMMARY 消息）
    await db.refresh(session)
    latest_ctx = session.shared_context or {}
    if not isinstance(latest_ctx, dict):
        latest_ctx = {}
    compaction_index = len(latest_ctx.get("compactions") or []) + 1

    from app.services.message_service import create_message

    # plan-527-2218: 与 pressure 路径共用同一帧构造（含附件清单）
    frame_text = _build_checkpoint_frame(summary_text, language, shadowed)
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
            # plan-548-2277：本次归档的会话记忆条数（前端压缩卡展示入口，详情可检索）
            "session_memory_count": session_memory_count,
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
    # plan-527-2218: 与 pressure 路径同口径更新压缩水位与例外；调用方没给全集时保持原水位
    if all_msgs is not None:
        _floor, _exceptions = compute_compacted_floor(
            all_msgs, latest_compacted, latest_ctx.get("compacted_exceptions"))
        new_ctx["compacted_floor"] = _floor
        new_ctx["compacted_exceptions"] = _exceptions
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
    # plan-560-2311 A4：压缩后占用与 compact_session（见其 `post_tokens` 口径）一致——
    # 「保留区 + 固定开销 + 新 checkpoint 摘要」。旧写法 shadowed_tokens - saved_tokens
    # 实际只等于摘要本身的 token，漏算保留区与固定开销，post_compact_ratio 因此
    # 严重偏低（自报已达标、真实仍高位），与 pressure 路径口径矛盾。
    remaining_tokens = estimate_effective_tokens(retained_messages or [])
    # plan-562-2318 A29：与 compact_session 同口径——只认调用方实测值，未传按 0。
    fixed_overhead = max(0, int(fixed_overhead_tokens or 0))
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
        "used_tokens": used_tokens,
        "context_window": context_window,
        "ratio": round((used_tokens or 0) / context_window * 100, 1) if context_window else None,
        # plan-19-82: 与 compact_session 口径一致，供 agent_loop 计算 post_compact_ratio
        "fixed_overhead_tokens": fixed_overhead,
        "post_compact_tokens": post_tokens,
        "post_compact_ratio": (
            round(post_tokens / context_window * 100, 1) if context_window else None
        ),
        # plan-238-1188: 同 compact_session——日志引用 summary_tokens，缺失会抛 KeyError。
        "summary_tokens": summary_tokens,
    }
    logger.info(
        "[compressor] session=%s %s 压缩完成: index=%d %d 条消息 %d tokens -> %d tokens，节省 %d，"
        "固定开销 %d，压缩后约 %d tokens (%.1f%%)",
        session.id, trigger, compaction_index, len(shadowed_ids), shadowed_tokens, summary_tokens, saved_tokens,
        fixed_overhead, post_tokens, result["post_compact_ratio"] or 0,
    )
    return result

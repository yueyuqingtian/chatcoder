"""上下文管理 —— 参照 Codex 的 normalize + emergency compact 设计。

v6.0 彻底重写，删除所有本地启发式压缩（micro_compact / auto_compact / snip /
context_collapse），这些是导致模型退化的根因。

保留核心操作（与 codex/core/src/context_manager/normalize.rs 对齐）：
1. ensure_tool_pairing —— 对应 codex 的 ensure_call_outputs_present + remove_orphan_outputs
2. emergency_compact —— 仅在 API 报 context overflow 错误时调用
3. DiminishingReturnsDetector —— 边际效应递减检测（v1.0 重新引入）
"""
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from app.models.schemas import ChatMessage

if TYPE_CHECKING:
    from app.models.schemas import ChatResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 边际效应递减检测器（v1.0: 重新引入，替代已删除的旧版实现）
# ---------------------------------------------------------------------------

class DiminishingReturnsDetector:
    """检测 agent loop 是否进入空转状态（v1.1: 前移预警）。

    判定空转的条件（满足任一即触发）：
    1. 连续 N 步无工具调用且内容高度相似（重复输出）
    2. 连续 N 步无工具调用且内容为空或极短
    3. 连续 N 步工具调用完全相同（由 agent_runtime 的 _called_tool_keys 处理）

    v1.1 改进：
    - max_idle_steps 从 5 降到 3
    - 新增"步效信号"：连续 2 步无新工具调用且无新 artifact 时提前预警
    - should_warn() 返回 True 时注入提示而非直接停止
    """

    def __init__(self, max_idle_steps: int = 3, similarity_threshold: float = 0.85, warn_steps: int = 2):
        self._max_idle_steps = max_idle_steps
        self._warn_steps = warn_steps
        self._similarity_threshold = similarity_threshold
        self._consecutive_no_tool: int = 0
        self._last_content: str = ""
        self._last_artifact_count: int = 0
        self._should_stop: bool = False
        self._should_warn: bool = False

    def observe(self, step: int, response: "ChatResponse", artifact_count: int = 0) -> None:
        """观察一步的模型响应，更新内部状态。

        Args:
            step: 当前步数
            response: 模型响应
            artifact_count: 累计 artifact 数（用于步效信号）
        """
        has_tools = bool(response.tool_calls)
        content = (response.content or "").strip()
        has_new_artifact = artifact_count > self._last_artifact_count
        self._last_artifact_count = artifact_count

        if has_tools:
            self._consecutive_no_tool = 0
            self._should_warn = False
            self._last_content = content
            return

        self._consecutive_no_tool += 1

        # 步效信号：连续 warn_steps 步无工具且无新 artifact -> 预警
        if self._consecutive_no_tool >= self._warn_steps and not has_new_artifact:
            self._should_warn = True
            logger.info("[DRDetector] step=%s 连续 %d 步无新工具调用且无新 artifact，预警",
                        step, self._consecutive_no_tool)

        # 条件 2: 内容为空或极短
        if len(content) < 10:
            if self._consecutive_no_tool >= self._max_idle_steps:
                self._should_stop = True
                logger.info("[DRDetector] step=%s 连续 %d 步无工具调用且内容极短，判定空转",
                            step, self._consecutive_no_tool)
            self._last_content = content
            return

        # 条件 1: 内容与上一步高度相似
        if self._last_content and self._similarity(content, self._last_content) > self._similarity_threshold:
            if self._consecutive_no_tool >= 2:
                self._should_stop = True
                logger.info("[DRDetector] step=%s 连续 %d 步输出高度相似，判定空转",
                            step, self._consecutive_no_tool)

        self._last_content = content

    def should_stop(self) -> bool:
        return self._should_stop

    def should_warn(self) -> bool:
        """返回是否应注入预警提示（不停止，仅引导）。"""
        if self._should_warn:
            self._should_warn = False
            return True
        return False

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """简单的字符级相似度（Jaccard on character trigrams）。"""
        if not a or not b:
            return 0.0
        # 对长文本取前 500 字符比较（性能考虑）
        a, b = a[:500], b[:500]
        if a == b:
            return 1.0
        set_a = {a[i:i+3] for i in range(len(a) - 2)}
        set_b = {b[i:i+3] for i in range(len(b) - 2)}
        if not set_a or not set_b:
            return 0.0
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        return intersection / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# 消息规范化（每轮调用前执行，纯内存操作，无 LLM 开销）
# ---------------------------------------------------------------------------

def ensure_tool_pairing(messages: list[ChatMessage]) -> list[ChatMessage]:
    """确保 tool_call/tool_result 配对完整。

    对应 codex 的:
    - ensure_call_outputs_present: 补充缺失的 function call output
    - remove_orphan_outputs: 删除孤立的 function call output

    OpenAI function-calling 协议要求：
    1. 每个 assistant(tool_calls) 中的每个 tool_call 必须有对应的 tool result
    2. 每个 tool result 必须有对应的 assistant(tool_calls)
    """
    if not messages:
        return messages

    # 收集所有 assistant tool_call_id → name
    call_ids: dict[str, str] = {}
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                tc_id = tc.get("id")
                if tc_id:
                    call_ids[tc_id] = tc.get("name", "unknown")

    # 收集已有的 tool result ids
    result_ids: set[str] = set()
    for m in messages:
        if m.role == "tool" and m.tool_call_id:
            result_ids.add(m.tool_call_id)

    # 构建修复后的消息列表
    fixed: list[ChatMessage] = []
    fixed_count = 0
    for m in messages:
        # 删除孤立的 tool result（没有对应 assistant tool_call 的）
        if m.role == "tool" and m.tool_call_id and m.tool_call_id not in call_ids:
            logger.debug("[normalize] 删除孤立 tool result: %s", m.tool_call_id)
            fixed_count += 1
            continue

        fixed.append(m)

        # 对每个 assistant(tool_calls)，在其后补充缺失的 tool result
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                tc_id = tc.get("id")
                tc_name = tc.get("name", "unknown")
                if tc_id and tc_id not in result_ids:
                    # v1.0: 明确标注“结果丢失”而非注入假数据
                    fixed.append(ChatMessage(
                        role="tool",
                        content=f"[系统提示: 工具 {tc_name} 的调用结果因上下文压缩已丢失，请勿依赖此结果做决策]",
                        tool_call_id=tc_id,
                        name=tc_name,
                    ))
                    logger.debug("[normalize] 补充缺失 tool result: %s", tc_name)
                    fixed_count += 1

    if fixed_count:
        logger.info("[normalize] ensure_tool_pairing 修复了 %d 条消息", fixed_count)

    return fixed


def normalize_tool_sequence(messages: list[ChatMessage]) -> list[ChatMessage]:
    """v8 根治：强制 OpenAI 协议 —— assistant(tool_calls) 后必须紧跟对应的 tool 消息。

    背景：context_manager 重建历史 / 截断 / 压缩后，可能出现
    - assistant(tool_calls) 与 tool 之间夹着 assistant 文本等非 tool 消息 → 网关 400
      "An assistant message with 'tool_calls' must be followed by tool messages"
    - 孤立的 tool 消息（无前置 assistant(tool_calls)）→ 网关 400
      "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'"

    策略（在 ensure_tool_pairing 之后调用）：
    1. 遇到 assistant(tool_calls)：进入"等待 tool 结果"状态，记录所有 tool_call id
    2. 等待期间：
       - tool 消息且 id 匹配 → 直接通过；全部匹配后放回延迟的消息
       - tool 消息但 id 不匹配（孤立）→ 丢弃
       - 其他消息（assistant 文本等）→ 延迟到该组 tool 结果全部匹配之后再放回
    3. 保证任何非 tool 消息都不会出现在 assistant(tool_calls) 与其 tool 结果之间。
    """
    if not messages:
        return messages

    result: list[ChatMessage] = []
    pending_ids: list[str] = []
    deferred: list[ChatMessage] = []

    def _flush_deferred() -> None:
        nonlocal deferred
        if deferred:
            result.extend(deferred)
            deferred = []

    for m in messages:
        if pending_ids:
            if m.role == "tool" and m.tool_call_id in pending_ids:
                result.append(m)
                pending_ids.remove(m.tool_call_id)
                if not pending_ids:
                    _flush_deferred()
                continue
            if m.role == "tool":
                # 孤立的 tool 消息（不在当前等待集）——丢弃
                logger.debug("[normalize] 丢弃孤立 tool 消息: %s", m.tool_call_id)
                continue
            # 非 tool 消息夹在 assistant(tool_calls) 与 tool 之间 → 延迟
            deferred.append(m)
            continue
        result.append(m)
        if m.role == "assistant" and m.tool_calls:
            pending_ids = [tc.get("id") for tc in m.tool_calls if tc.get("id")]

    _flush_deferred()
    return result


# ---------------------------------------------------------------------------
# v6.0: 工具结果预算 -- 构造本轮 API 副本（不修改原始历史）
# 对应调研结论："工具结果预算（本轮怎么发）vs auto-compact（以后保留什么）两层分离"
# ---------------------------------------------------------------------------

def build_api_copy(
    messages: list[ChatMessage], keep_recent_groups: int = 3,
) -> list[ChatMessage]:
    """v6.0: 基于原始历史构造发给模型的 API 副本，折叠较早的 tool result，不修改原始。

    近期 keep_recent_groups 组工具调用回合保留完整（已由 _truncate_output 截断），
    更早的 tool result 折叠为摘要占位，减少上下文占用。
    保证不破坏 tool_call/tool_result 配对（只截断 content 不删消息）。
    """
    import copy as _copy

    if not messages:
        return messages

    api = [_copy.copy(m) for m in messages]

    # 从后往前找第 keep_recent_groups 个 assistant(tool_calls)，之前的 tool result 折叠
    keep_from = 0
    rounds_seen = 0
    for i in range(len(api) - 1, -1, -1):
        if api[i].role == "assistant" and api[i].tool_calls:
            rounds_seen += 1
            if rounds_seen == keep_recent_groups:
                keep_from = i
                break

    folded = 0
    for i in range(keep_from):
        m = api[i]
        if m.role == "tool" and m.content and len(m.content) > 200:
            orig_len = len(m.content)
            tool_name = m.name or "unknown"
            preview = m.content[:200]
            m.content = f"[已折叠: 工具 {tool_name}，原 {orig_len} 字符，关键信息: {preview}]"
            folded += 1

    if folded:
        logger.debug("[api_copy] 折叠了 %d 条较早的 tool result", folded)
    return api


# ---------------------------------------------------------------------------
# v6.0: 主动自动压缩（LLM handoff summary，对齐 codex/claude code auto-compact）
# ---------------------------------------------------------------------------

def _build_fallback_summary(old_rounds: list[list[ChatMessage]]) -> str:
    """v6.0: 硬编码降级摘要（LLM 不可用时用），逻辑等价于 emergency_compact 的摘要构建。"""
    summary_parts: list[str] = []
    tool_names_seen: list[str] = []
    files_touched: set[str] = set()

    for round_msgs in old_rounds:
        for m in round_msgs:
            if m.role == "assistant" and m.content:
                summary_parts.append(m.content[:200])
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    tool_names_seen.append(tc.get("name", "?"))
                    args = tc.get("arguments", {})
                    if isinstance(args, dict):
                        path = args.get("path") or args.get("repo")
                        if path:
                            files_touched.add(path)
            if m.role == "tool" and m.content:
                preview = m.content[:100]
                if preview.strip():
                    summary_parts.append(f"  result: {preview}")

    summary = "以下是之前对话的摘要：\n"
    if tool_names_seen:
        tool_counts: dict[str, int] = defaultdict(int)
        for name in tool_names_seen:
            tool_counts[name] += 1
        summary += f"已调用工具: {', '.join(f'{k}({v}次)' for k, v in tool_counts.items())}\n"
    if files_touched:
        summary += f"涉及文件: {', '.join(sorted(files_touched)[:20])}\n"
    if summary_parts:
        summary += "关键内容:\n" + "\n".join(summary_parts[:30])
    return summary


async def auto_compact(
    messages: list[ChatMessage],
    context_window: int,
    provider,
) -> list[ChatMessage]:
    """v6.0: 主动自动压缩 -- 用 LLM 生成 handoff summary 替换较早历史，保留近期完整回合。

    参照 codex compact/prompt.md：为接手的 LLM 写交接摘要（进度/决策/约束/下一步/关键数据）。
    切点对齐 turn 边界，不拆散 assistant(tool_calls)+tool results。
    收益不足时跳过（避免无价值 LLM 调用）。LLM 失败时降级为硬编码摘要。
    """
    from app.core.config import settings
    from app.orchestration.prompts import COMPACTION_PROMPT
    from app.orchestration.token_counter import estimate_messages_tokens

    if not messages or len(messages) < 10:
        return messages

    # v6.1: system + developer 都是系统级消息，必须保留不被折叠
    system_msgs = [m for m in messages if m.role in ("system", "developer")]
    non_system = [m for m in messages if m.role not in ("system", "developer")]

    keep_rounds = settings.auto_compact_keep_rounds

    # 回合切分（复用 emergency_compact 逻辑）
    rounds: list[list[ChatMessage]] = []
    current_round: list[ChatMessage] = []
    for msg in reversed(non_system):
        current_round.insert(0, msg)
        if msg.role == "assistant" and msg.tool_calls:
            rounds.insert(0, current_round)
            current_round = []
        elif msg.role == "user" and not current_round[-1:] == [msg]:
            rounds.insert(0, current_round)
            current_round = []
    if current_round:
        rounds.insert(0, current_round)

    if len(rounds) <= keep_rounds:
        return messages

    keep_rounds_data = rounds[-keep_rounds:]
    old_rounds = rounds[:-keep_rounds]
    old_messages_flat = [m for r in old_rounds for m in r]

    # 收益检查：较早回合 token 量不足则跳过
    old_tokens = estimate_messages_tokens(old_messages_flat)
    if old_tokens < settings.auto_compact_min_reclaim_tokens:
        logger.debug("[auto_compact] 较早回合仅 %d tokens，收益不足，跳过", old_tokens)
        return messages

    # 构造待摘要文本
    transcript_parts: list[str] = []
    for m in old_messages_flat:
        if m.role == "assistant" and m.content:
            transcript_parts.append(f"[assistant] {m.content[:300]}")
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                name = tc.get("name", "?")
                args = tc.get("arguments", {})
                transcript_parts.append(f"[tool_call] {name}({str(args)[:100]})")
        if m.role == "tool" and m.content:
            transcript_parts.append(f"[tool_result] {m.content[:200]}")
        if m.role == "user" and m.content:
            transcript_parts.append(f"[user] {m.content[:200]}")
    transcript = "\n".join(transcript_parts)

    # LLM 生成 handoff summary（对齐 codex COMPACTION_PROMPT）
    summary_text = ""
    if provider and transcript:
        try:
            from app.models.schemas import ChatRequest as _CR, ChatMessage as _CM
            req = _CR(
                messages=[
                    _CM(role="system", content=COMPACTION_PROMPT),
                    _CM(role="user", content=transcript[:24000]),
                ],
                model="",
                temperature=0.3,  # v6.2: 摘要生成保持低温度，稳定结构化输出
            )
            resp = await provider.chat(req)
            summary_text = (resp.content or "").strip()
        except Exception as e:
            logger.warning("[auto_compact] LLM 摘要失败，降级硬编码: %s", e)

    if not summary_text:
        summary_text = _build_fallback_summary(old_rounds)

    # v6.1: 注入 SUMMARY_PREFIX（对齐 codex summary_prefix.md），告知接手 LLM 这是前模型摘要，
    # 避免重复已完成的工作；使用 developer 角色（系统级指令不污染对话流）
    from app.orchestration.prompts import SUMMARY_PREFIX
    summary_msg = ChatMessage(
        role="developer",
        content=f"{SUMMARY_PREFIX}\n\n{summary_text}",
    )
    result = system_msgs + [summary_msg] + [m for r in keep_rounds_data for m in r]
    result = ensure_tool_pairing(result)

    logger.info(
        "[auto_compact] %d -> %d 条消息（折叠 %d 回合/%d tokens 为摘要）",
        len(messages), len(result), len(old_rounds), old_tokens,
    )
    return result


# ---------------------------------------------------------------------------
# 紧急压缩（仅在 API 报 context overflow 错误时调用）
# ---------------------------------------------------------------------------

def emergency_compact(messages: list[ChatMessage], context_window: int) -> list[ChatMessage]:
    """紧急压缩：当 API 报 context overflow 时调用。

    参照 codex 的 emergency 策略：
    - 保留 system + 最近的消息
    - 将较早的消息折叠为一条摘要
    - 保持 tool_call/tool_result 配对完整

    策略：
    1. 分离 system 消息（永远保留）
    2. 保留最后 N 组完整的工具调用回合
    3. 较早的回合折叠为一条 system 摘要
    """
    if not messages or len(messages) < 10:
        return messages

    # 分离 system/developer 和其余消息（v6.1: developer 也是系统级，保留）
    system_msgs = [m for m in messages if m.role in ("system", "developer")]
    non_system = [m for m in messages if m.role not in ("system", "developer")]

    # 计算保留最近的回合数（每个回合 = assistant(tool_calls) + tool results）
    # 保留最近 6 组工具调用回合
    keep_rounds = 6

    # 从后往前找完整的回合边界
    rounds: list[list[ChatMessage]] = []
    current_round: list[ChatMessage] = []

    for msg in reversed(non_system):
        current_round.insert(0, msg)
        # 一个回合的边界：assistant 带 tool_calls 的消息之前的 tool results 组成一个回合
        if msg.role == "assistant" and msg.tool_calls:
            rounds.insert(0, current_round)
            current_round = []
        elif msg.role == "user" and not current_round[-1:] == [msg]:
            # user 消息也标记回合边界
            rounds.insert(0, current_round)
            current_round = []

    # 如果最后有剩余消息，也作为一个回合
    if current_round:
        rounds.insert(0, current_round)

    if len(rounds) <= keep_rounds:
        # 不需要压缩
        return messages

    # 保留最近 keep_rounds 个回合
    keep_rounds_data = rounds[-keep_rounds:]
    old_rounds = rounds[:-keep_rounds]

    # 将较早的回合折叠为摘要
    summary_parts = []
    tool_names_seen: list[str] = []
    files_touched: set[str] = set()

    for round_msgs in old_rounds:
        for m in round_msgs:
            if m.role == "assistant" and m.content:
                summary_parts.append(m.content[:200])
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    tool_names_seen.append(tc.get("name", "?"))
                    args = tc.get("arguments", {})
                    if isinstance(args, dict):
                        path = args.get("path") or args.get("repo")
                        if path:
                            files_touched.add(path)
            if m.role == "tool" and m.content:
                # 只取 tool result 的前 100 字符
                preview = m.content[:100]
                if preview.strip():
                    summary_parts.append(f"  result: {preview}")

    summary = "以下是之前对话的摘要：\n"
    if tool_names_seen:
        # 统计每个工具调用次数
        tool_counts: dict[str, int] = defaultdict(int)
        for name in tool_names_seen:
            tool_counts[name] += 1
        summary += f"已调用工具: {', '.join(f'{k}({v}次)' for k, v in tool_counts.items())}\n"
    if files_touched:
        summary += f"涉及文件: {', '.join(sorted(files_touched)[:20])}\n"
    if summary_parts:
        summary += "关键内容:\n" + "\n".join(summary_parts[:30])

    summary_msg = ChatMessage(role="system", content=summary)

    # 组装：system(原) + summary + 最近回合
    result = system_msgs + [summary_msg] + [m for r in keep_rounds_data for m in r]

    # 确保配对完整
    result = ensure_tool_pairing(result)

    logger.info(
        "[emergency] %d -> %d 条消息 (折叠 %d 个回合为摘要)",
        len(messages), len(result), len(old_rounds),
    )
    return result

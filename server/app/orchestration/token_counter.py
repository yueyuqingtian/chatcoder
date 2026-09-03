"""Token 估算与上下文预算管理。

对齐 codex-rs/utils/string/src/truncate.rs 的 approx_token_count 实现：
- 粗略估算：UTF-8 字节长度 / 4（codex 的 APPROX_BYTES_PER_TOKEN = 4）
- 不使用 tiktoken（codex 也不用，太重且不通用）
- 字节长度天然适配中英文混合：中文 3 字节/字 ≈ 0.75 token，英文 1 字节/字 ≈ 0.25 token

对齐 codex 的上下文预算：
- auto_compact_token_limit = context_window * 90%（codex openai_models.rs auto_compact_token_limit()）
- effective_context_window_percent = 95%（codex 默认值）
"""
import logging

from app.models.schemas import ChatMessage
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Token 估算常量（对齐 codex APPROX_BYTES_PER_TOKEN = 4）──
_BYTES_PER_TOKEN = 4
# v6.5: 每条消息结构开销。OpenAI ChatML 每条约 4 token（<im_start>{role}\n...<im_end>\n）。
# 实测 glm-5.2：959条消息真实80k，纯文本99条约25k估算，工具开销需保守避免高估。
_MESSAGE_OVERHEAD = 4
_TOOL_CALL_OVERHEAD = 8   # tool_call 结构额外开销（function + id + type）
_TOOL_RESULT_OVERHEAD = 8  # tool result 结构额外开销（tool_call_id + name）


def _approx_token_count(text: str) -> int:
    """对齐 codex approx_token_count：UTF-8 字节长度 / 4，向上取整。

    codex 实现（truncate.rs:71）:
        pub fn approx_token_count(text: &str) -> usize {
            let len = text.len();  // UTF-8 字节长度
            len.saturating_add(APPROX_BYTES_PER_TOKEN.saturating_sub(1)) / APPROX_BYTES_PER_TOKEN
        }

    旧版用 len(text_chars) / 3.0 是错误的：
    - 中文 1 字符 = 3 字节，codex 算 0.75 token，旧版算 0.33 token（少算一倍）
    - 英文 1 字符 = 1 字节，codex 算 0.25 token，旧版算 0.33 token（多算）
    """
    if not text:
        return 0
    byte_len = len(text.encode("utf-8"))
    return (byte_len + _BYTES_PER_TOKEN - 1) // _BYTES_PER_TOKEN


# 保留旧函数名兼容外部调用，内部改用 _approx_token_count
def rough_token_estimate(text: str) -> int:
    """粗略估算文本的 token 数（对齐 codex 字节长度/4）。"""
    return _approx_token_count(text)


def precise_token_count(text: str) -> int:
    """精确 token 计数。

    v6.1: 对齐 codex，不再使用 tiktoken（codex 也不用）。
    codex 全程使用 approx_token_count（字节/4），统一用这个。
    """
    return _approx_token_count(text)


# ── 上下文预算分配（对齐 codex openai_models.rs）──
# codex: auto_compact_token_limit = context_window * 9 / 10 (90%)
# codex: effective_context_window_percent = 95 (95% 可用于输入)
_AUTO_COMPACT_THRESHOLD = 0.90
_EFFECTIVE_CONTEXT_WINDOW_RATIO = 0.95
# 压缩后目标 token：窗口的此比例（codex compact 后约保留 45%）
_POST_COMPACT_TARGET = 0.45


def estimate_message_tokens(msg: ChatMessage) -> int:
    """估算单条 ChatMessage 的 token 数。"""
    total = _MESSAGE_OVERHEAD
    total += rough_token_estimate(msg.content or "")
    if msg.tool_calls:
        for tc in msg.tool_calls:
            args = tc.get("arguments", {})
            if isinstance(args, dict):
                total += rough_token_estimate(str(args))
            elif isinstance(args, str):
                total += rough_token_estimate(args)
            total += rough_token_estimate(tc.get("name", ""))
            total += _TOOL_CALL_OVERHEAD
    if msg.name:
        total += rough_token_estimate(msg.name)
    return total


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    """估算消息列表的总 token 数。"""
    return sum(estimate_message_tokens(m) for m in messages)


def estimate_breakdown(messages: list[ChatMessage]) -> dict[str, int]:
    """v2.2 (对齐 zcode 3.10): 上下文用量 7 类分类估算。

    分类（对齐 ZCode context-breakdown-1..7 色块）：
    system(系统提示) / tools(工具定义，无法从消息列表精确分离，归入 system)
    / history(历史对话) / tool_results(工具结果) / thinking(思考) / input(当前输入)
    """
    breakdown = {
        "system": 0, "tools": 0, "history": 0,
        "tool_results": 0, "thinking": 0, "input": 0,
    }
    for i, m in enumerate(messages):
        if m.role in ("system", "developer"):
            breakdown["system"] += estimate_message_tokens(m)
        elif m.role == "tool":
            breakdown["tool_results"] += estimate_message_tokens(m)
        elif m.role == "assistant":
            is_thinking = bool(m.content and "[思考]" in str(m.content))
            if is_thinking or (m.content and "thinking" in str(m.content).lower()[:200]):
                breakdown["thinking"] += estimate_message_tokens(m)
            else:
                breakdown["history"] += estimate_message_tokens(m)
        elif m.role == "user":
            # 最后一条 user 消息视为当前输入，其余为历史
            if i == len(messages) - 1:
                breakdown["input"] += estimate_message_tokens(m)
            else:
                breakdown["history"] += estimate_message_tokens(m)
        else:
            breakdown["history"] += estimate_message_tokens(m)
    return breakdown


def get_context_budget(context_window: int) -> dict[str, int]:
    """计算上下文预算分配（对齐 codex openai_models.rs）。

    codex 逻辑:
    - auto_compact_token_limit = context_window * 90%
    - effective_context_window = context_window * 95%（可用于输入）
    - 两者取最小值作为实际触发压缩的上限

    返回:
        input_limit: 有效输入 token 上限（context_window * 95%）
        auto_compact_threshold: 触发自动压缩的阈值（context_window * 90%）
        post_compact_target: 压缩后目标 token 数
        output_reserve: 输出 token 预留（context_window - input_limit）
    """
    input_limit = int(context_window * _EFFECTIVE_CONTEXT_WINDOW_RATIO)
    return {
        "input_limit": input_limit,
        "auto_compact_threshold": int(context_window * _AUTO_COMPACT_THRESHOLD),
        "post_compact_target": int(context_window * _POST_COMPACT_TARGET),
        "output_reserve": context_window - input_limit,
    }


def should_auto_compact(messages: list[ChatMessage], context_window: int) -> bool:
    """判断是否需要触发自动压缩。"""
    budget = get_context_budget(context_window)
    used = estimate_messages_tokens(messages)
    return used > budget["auto_compact_threshold"]


def fits_in_context(
    messages: list[ChatMessage], context_window: int, reserve_output: int | None = None,
) -> bool:
    """判断消息列表是否在上下文窗口内。"""
    used = estimate_messages_tokens(messages)
    budget = get_context_budget(context_window)
    output = reserve_output or budget["output_reserve"]
    return used + output <= context_window


# ───────────────────────────────────────────────────────────────────
# v3.2: Token-Budget 驱动的消息窗口选取
# ───────────────────────────────────────────────────────────────────

def estimate_message_tokens_from_model(msg) -> int:
    """估算数据库 Message 模型的 token 数。

    处理 TEXT / TOOL_CALL / TOOL_RESULT / TASK_CARD 等类型，
    不依赖 ChatMessage 结构，直接从 DB 模型估算。
    """
    from app.core.enums import MsgType
    import json as _json

    total = _MESSAGE_OVERHEAD

    content = msg.content
    if isinstance(content, dict):
        if msg.msg_type == MsgType.TOOL_CALL:
            tool = content.get("tool", "")
            args = content.get("args", {})
            # v6.5: 用 json.dumps 估算 args，比 str() 更接近实际序列化后的体积
            try:
                args_str = _json.dumps(args, ensure_ascii=False) if args else ""
            except (TypeError, ValueError):
                args_str = str(args) if args else ""
            total += rough_token_estimate(tool + args_str)
            total += _TOOL_CALL_OVERHEAD
        elif msg.msg_type == MsgType.TOOL_RESULT:
            output = content.get("output", "") or content.get("error", "") or ""
            tool = content.get("tool", "")
            total += rough_token_estimate(tool + output)
            total += _TOOL_RESULT_OVERHEAD
        else:
            text = content.get("text") or content.get("note") or ""
            total += rough_token_estimate(text)
    elif isinstance(content, str):
        total += rough_token_estimate(content)

    return total


def select_messages_by_token_budget(
    messages: list,
    token_budget: int,
    min_keep: int = 5,
) -> tuple[list, list]:
    """从最新消息开始向前贪心选取，直到 token 预算耗尽。

    这是 Token-Budget 窗口的核心函数，替代固定条数窗口。

    策略：
    1. 从最新消息开始向前遍历
    2. 累加 token，超过 budget 时停止
    3. 至少保留 min_keep 条（即使超 budget，也保底）
    4. 短消息 → 更多条数被保留
    5. 长工具输出 → 自然被挤出窗口

    Args:
        messages: 按时间正序排列的消息列表（最旧在前）
        token_budget: 允许的最大 token 数
        min_keep: 至少保留多少条消息

    Returns:
        (selected, overflow)
        - selected: 被选入窗口的消息（保持时间正序）
        - overflow: 未被选入的旧消息（时间正序，最旧的在前）
    """
    if not messages:
        return [], []

    selected: list = []
    used = 0
    # 从最新（末尾）开始向前
    for i in range(len(messages) - 1, -1, -1):
        msg_tokens = estimate_message_tokens_from_model(messages[i])
        if used + msg_tokens > token_budget and len(selected) >= min_keep:
            break
        used += msg_tokens
        selected.append(messages[i])

    selected.reverse()  # 恢复时间正序
    # overflow = 选中之前的那部分
    selected_set = set(id(m) for m in selected)
    overflow = [m for m in messages if id(m) not in selected_set]

    return selected, overflow


def messages_token_total(messages: list) -> int:
    """计算 DB Message 列表的总 token 数。"""
    return sum(estimate_message_tokens_from_model(m) for m in messages)


# ───────────────────────────────────────────────────────────────────
# v3.3: Per-Agent 动态上下文窗口解析
# ───────────────────────────────────────────────────────────────────

# ── 比例常量（所有窗口/阈值都基于 context_window 的比例）──
# v1.1: 集中化到 settings（痛点5），保留常量名作向后兼容，值从 settings 读取
def _ratio(name: str, default: float) -> float:
    from app.core.config import settings
    return float(getattr(settings, name, default))

AGENT_LOOP_COMPACT_RATIO = 0.90
# plan-166-767: 摘要阈值与注入预算统一为 0.85×目标模型窗口。
# 旧值 0.35 导致历史仅占窗口 35% 就触发摘要（v21 为匹配当年 0.30 预算而调低，
# 预算早已升到 0.80+，属过时配置），用户观感「未达压缩阈值也被摘要」。
# 0.85 语义：未摘要总量 ≤ 0.85×窗口 → 不摘要且全量注入（上下文完整）；
# 超过 0.85×窗口 → 先循环摘要到 ≤ 阈值，再全量注入（无静默丢失）。
# 预算(=0.85) 必须 ≥ 阈值(=0.85)，否则 80%~85% 区间消息既未注入也未摘要而丢失（v21 已踩坑）。
MAIN_SUMMARIZE_RATIO = 0.85
MAIN_WINDOW_RATIO = 0.85          # plan-166-767: 与摘要阈值一致（≥ 阈值），不摘要态全量注入
MAIN_SUMMARIZE_BATCH_RATIO = 0.35  # v6.3: 从 0.06 提升到 0.35，一次摘要更多（循环摘要的每批量）
THREAD_WINDOW_RATIO = 0.85        # v6.3: 从 0.15 提升到 0.85，线程窗口保留更多历史
# 至少保留的最近消息条数（保底，不按比例）
MIN_MESSAGES_KEEP = 5


async def get_agent_context_window(db: AsyncSession, agent) -> int:
    """解析 Agent 的有效上下文窗口大小。

    优先级：
    1. agent.model_id → Model.context_window（如果已配置）
    2. settings.default_context_window（全局兜底）

    这是 v3.3 的核心：不同 Agent 可能绑定不同模型（500K / 1M / 200K），
    所有窗口管理和压缩阈值都应该基于各自模型的真实窗口大小，
    而非全局统一的 500000。
    """
    from app.core.config import settings

    if agent and getattr(agent, "model_id", None):
        from app.persistence.models.model_reg import Model
        model = await db.get(Model, agent.model_id)
        if model and model.context_window and model.context_window > 0:
            return model.context_window

    return settings.default_context_window


def get_main_window_budget(context_window: int) -> int:
    """根据 Leader 模型上下文窗口计算主群聊窗口预算。"""
    return max(4000, int(context_window * MAIN_WINDOW_RATIO))


def get_main_summarize_threshold(context_window: int) -> int:
    """根据 Leader 模型上下文窗口计算主群聊摘要触发阈值。"""
    return max(6000, int(context_window * MAIN_SUMMARIZE_RATIO))


def get_main_summarize_batch_tokens(context_window: int) -> int:
    """根据 Leader 模型上下文窗口计算每次摘要的目标 token 量。"""
    return max(4000, int(context_window * MAIN_SUMMARIZE_BATCH_RATIO))


def get_thread_window_budget(context_window: int) -> int:
    """根据 Agent 模型上下文窗口计算 Thread 窗口预算。"""
    return max(8000, int(context_window * THREAD_WINDOW_RATIO))

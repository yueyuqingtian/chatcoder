"""Agent 运行时 v0.3:真实 agent loop(思考-调用工具-观察-产出)。

- 三层上下文(context.build_agent_context)
- OpenAI function-calling 循环(≤ max_steps)
- 副作用工具经 ToolExecutor → ApprovalManager 阻塞审批
- 详细产出下沉 thread,主群发关键节点 task_card
- 产物抽取入库 Artifact

本期:服务端运行 + 服务端工具执行 + 仅 system_default 模型可调。
"""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import MsgType, SenderType
from app.gateway.ws import manager as ws_manager
from app.models.registry import get_model_registry
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.artifacts import extract_and_persist_artifacts

logger = logging.getLogger(__name__)

# v1.0: per-session 数据库写入锁（替代全局锁，避免并行 agent 退化为串行）
_session_locks: dict[int, asyncio.Lock] = {}
_SQLITE_MODE = settings.database_url.startswith("sqlite")


def _get_session_lock(session_id: int) -> asyncio.Lock:
    """v1.0: 获取 session 级写入锁。PG 模式下返回一个无操作锁（不阻塞）。"""
    if not _SQLITE_MODE:
        # PostgreSQL 支持并发写入，无需锁
        return _NoopLock()
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


class _NoopLock:
    """PG 模式下的无操作锁（async context manager 兼容）。"""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass

# 工具输出最大字符数(防止撑爆模型上下文窗口) - 已废弃，改用 _tool_output_limit 分级
_MAX_TOOL_OUTPUT_CHARS_LEGACY = 3000

# v1.1: 工具输出阈值分级（痛点1）
_TOOL_OUTPUT_LIMITS = {
    "fs_read": "tool_output_chars_read",
    "fs_list": "tool_output_chars_read",
    "fs_grep": "tool_output_chars_grep",
    "codebase_search": "tool_output_chars_grep",
    "terminal_exec": "tool_output_chars_terminal",
    "web_fetch": "tool_output_chars_web",
    "web_search": "tool_output_chars_web",
    "fs_write": "tool_output_chars_write",
    "editor_apply_diff": "tool_output_chars_write",
}


def _tool_output_limit(tool_name: str) -> int:
    """按工具名返回对应的输出字符阈值，从 settings 读取。"""
    from app.core.config import settings
    key = _TOOL_OUTPUT_LIMITS.get(tool_name, "tool_output_chars_default")
    return int(getattr(settings, key, settings.tool_output_chars_default))


def _truncate_output(text: str, limit: int | None = None, tool_name: str = "") -> str:
    """截断工具输出，保留头部和尾部，中间省略。

    Args:
        text: 原始输出
        limit: 显式限制（优先），None 时按 tool_name 从 settings 读取
        tool_name: 工具名，用于分级阈值
    """
    if limit is None:
        limit = _tool_output_limit(tool_name) if tool_name else _MAX_TOOL_OUTPUT_CHARS_LEGACY
    if not text or len(text) <= limit:
        return text
    half = limit // 2
    return (
        text[:half]
        + f"\n\n... [已截断,原始 {len(text)} 字符,仅显示前后各 {half} 字符] ...\n\n"
        + text[-half:]
    )


# ── 纯文本工具意图检测 ──────────────────────────────────────────────
# DeepSeek/GLM 等模型在多轮工具调用后会"退化"为纯文本输出，
# 不再返回结构化 tool_calls，而是直接在 content 中写文件路径列表、
# 命令行或操作描述。这里检测这种退化模式，引导模型重新用正确格式调用。
import re as _re

# 代码文件扩展名正则
_CODE_EXT_RE = _re.compile(
    r'\.(java|py|ts|js|go|rs|c|cpp|h|hpp|vue|jsx|tsx|xml|json|yaml|yml|sql|sh|rb|kt|swift|php|css|scss|less|md)$',
    _re.IGNORECASE,
)
# 工具调用意图动词（中英双语）
_INTENT_VERBS = [
    "让我读取", "让我查看", "让我审", "让我先读", "让我获取",
    "接下来读取", "继续获取", "继续读取", "需要读取", "需要查看",
    "让我检查", "让我分析", "接下来分析", "让我看",
    "let me read", "let me check", "next i will", "i will read",
]
# 命令行模式
_CMD_RE = _re.compile(
    r'^(git\s+\w|fs_read\s|fs_list\s|fs_grep\s|fs_write\s|ls\s|cat\s|find\s|grep\s|cd\s)',
    _re.IGNORECASE,
)


def _detect_tool_call_intent(content: str) -> bool:
    """检测纯文本内容是否包含工具调用意图（模型退化为纯文本输出）。

    v5.3: 大幅收紧检测条件，只在非常明确的退化信号下才返回 True。
    v6.0: 三层结合检测（痛点4优化）
    - 第一层: [INTENT: tool_name] 自检标记
    - 第二层: 结构特征检测（tool(arg) 模式、文件路径+行号）
    - 第三层: 原有正则兜底

    返回 True 表示模型想调用工具但格式不对，需要引导重试。
    返回 False 表示这是模型的最终回答，正常结束。
    """
    if not content or len(content.strip()) < 5:
        return False

    # ── 第一层: [INTENT: tool_name] 自检标记 ──
    if _re.search(r'\[INTENT:\s*\w+\]', content, _re.IGNORECASE):
        return True

    # 模式0: DSML 格式
    if "DSML" in content:
        return True

    # ── 第二层: 结构特征检测 ──
    # tool(arg) 模式
    if _re.search(r'\b(fs_read|fs_write|fs_list|fs_grep|terminal_exec|web_fetch|web_search|git_diff)\s*\(', content, _re.IGNORECASE):
        return True
    # 文件路径 + 行号
    if _re.search(r'[\w/\\]+\.\w+\s*[:：]\s*\d+', content):
        return True

    # ── 第三层: 原有正则兜底 ──
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    cmd_lines = [l for l in lines if _CMD_RE.match(l)]
    if cmd_lines:
        return True

    # 模式2: 高置信度退化 —— 内容很短（<200字）且主要都是文件路径
    # （排除长报告——报告自然包含路径引用）
    if len(content) < 200:
        path_lines = [l for l in lines if _CODE_EXT_RE.search(l) and len(l) < 200]
        non_path_lines = [l for l in lines if not _CODE_EXT_RE.search(l)]
        # 70% 以上是路径行，且有 ≥3 个路径 → 退化
        if len(path_lines) >= 3 and len(non_path_lines) <= 1:
            return True

    # 模式3: 看起来像命令的行
    cmd_lines = [l for l in lines if _CMD_RE.match(l)]
    if cmd_lines:
        return True

    return False


def _build_intent_hint(content: str) -> str:
    """构造引导消息，引用模型具体输出（痛点4：失败反馈具体化）。"""
    snippet = content.strip().splitlines()[0][:80] if content.strip() else ""
    return (
        f"你刚才的输出「{snippet}」似乎想调用工具但格式不对。"
        "请改用标准的 function calling 格式（生成 tool_calls 字段），"
        "或在末尾输出 [INTENT: tool_name] 标记明确意图。"
    )


# ── v3.6: 工具调用格式锚定 ──────────────────────────────────────────
# 多轮对话后模型容易"遗忘"如何正确使用 function calling 格式，
# 退化为纯文本描述工具调用。定期注入格式提醒作为"锚点"。
# v6.1: 英文化 + 使用 developer 角色注入（不污染 user 对话流）

TOOL_FORMAT_ANCHOR_MESSAGE = (
    "Tool call format reminder: When you need to read files, search code, or run commands, "
    "use structured function calls. Never describe tool actions in natural language.\n"
    "Example: to read /src/main.py, make a fs_read(path='/src/main.py') function call, "
    "instead of writing \"I need to read /src/main.py\".\n"
    "If you have already searched some files without finding the information you need, "
    "search other files or output your final result."
)


def _has_tool_calls_in_history(messages: list) -> bool:
    """检查消息历史中是否存在过 tool 角色的消息（即模型已调用过工具）。"""
    return any(getattr(m, "role", "") == "tool" for m in messages[-50:])


# ─────────────────────────────────────────────────────────────────────
from app.orchestration.compaction import emergency_compact
from app.orchestration.context import build_agent_context
from app.orchestration.tools import ToolContext, tool_executor
from app.orchestration.tools.registry import tool_registry
from app.services import session_service, task_service

logger = logging.getLogger(__name__)


@dataclass
class AgentOutput:
    kind: str  # message | done | error | skipped
    text: str = ""
    error: str = ""
    artifact_ids: list[int] = field(default_factory=list)


async def run_agent_loop(
    db: AsyncSession,
    *,
    session_id: int,
    task_id: int,
    agent_id: int,
    agent_name: str,
    agent_role: str = "",
    system_prompt: str,
    template_whitelist: list[str] | None = None,
    max_steps: int | None = None,
    cancel_event: asyncio.Event | None = None,
    reasoning_effort: str | None = None,
) -> AgentOutput:
    """运行单个 agent 的推理循环。

    template_whitelist: 来自 agent_template.tool_whitelist,决定 agent 可调用哪些工具。
    cancel_event: v0.9 任务中断事件,set 后每步检查并退出。
    max_steps: 显式覆盖步数上限（v3.4: 不传则由智能等级决定）。
    """

    # v3.4: max_steps 由智能等级决定，此行保留仅为向后兼容显式传参场景

    # ── 0. 取任务/会话/agent 实体 ──
    task = await task_service.get_task(db, task_id)
    if task is None:
        return AgentOutput(kind="error", error="task not found")
    session = await session_service.get_session(db, session_id)
    if session is None:
        return AgentOutput(kind="error", error="session not found")
    from app.persistence.models.agent import Agent
    agent = await db.get(Agent, agent_id)
    if agent is None:
        return AgentOutput(kind="error", error="agent not found")

    # ── 1. 解析 provider(按 agent.model_id 路由) ──
    registry = get_model_registry()
    provider, reason = await registry.get_provider_for_agent(db, agent)
    if provider is None:
        # 模型不可用是配置/环境问题，标 pending 允许重试而非直接 blocked
        async with _get_session_lock(session_id):
            await task_service.update_task_status(db, task_id, "pending")
        await _emit_main_card(db, session_id, agent_id, agent_name, task_id, task.title,
                              status="pending", note=f"模型不可用({reason}),等待重试",
                              agent_role=agent_role)
        await _broadcast_agent_status(session_id, agent_id, agent_name,
                                      status="failed", task_id=task_id)
        return AgentOutput(kind="skipped", error=reason)

    # ── 2. 标记进行中 + 主群关键节点 ──
    async with _get_session_lock(session_id):
        await task_service.update_task_status(db, task_id, "in_progress")
        await db.commit()  # v1.0: 立即提交，确保轮询能看到 in_progress 状态
    await _emit_main_card(db, session_id, agent_id, agent_name, task_id, task.title,
                          status="in_progress", note="已开始执行,详情见子会话",
                          agent_role=agent_role)
    # 广播 task.updated
    await _broadcast_task_updated(session_id, task_id, "in_progress")
    # v0.9.1: 广播 agent.status,让前端 AgentPanel 实时显示执行状态
    await _broadcast_agent_status(session_id, agent_id, agent_name,
                                  status="running", task_id=task_id)

    # v0.9: 解析会话级工作目录(优先 session.workspace_root,兜底全局)
    from app.core.config import resolve_workspace_root
    workspace_root = resolve_workspace_root(session.workspace_root)

    # v3.3: 解析 Agent 的 per-model 上下文窗口（替代全局 settings.default_context_window）
    # 不同 Agent 可能绑定不同模型（如 A=500K，B=1M），压缩阈值应基于各自模型
    from app.orchestration.token_counter import get_agent_context_window
    agent_context_window = await get_agent_context_window(db, agent)
    # v6.4: 最小窗口保护 —— 若 model.context_window 配置过小，用默认值兜底
    if agent_context_window < 100000:
        logger.warning(
            "[agent] task=%s agent_context_window=%d 过小(<100k)，用默认 500000 兜底",
            task_id, agent_context_window,
        )
        agent_context_window = 500000
    logger.info("[agent] task=%s agent_context_window=%d, 压缩阈值=%.0f%% (%d tokens)",
                task_id, agent_context_window,
                settings.auto_compact_threshold_ratio * 100,
                int(agent_context_window * settings.auto_compact_threshold_ratio))

    # v6.1: 对齐 codex -- 不传 temperature，只用 reasoning_effort 控制推理深度
    # codex build_responses_request 完全不传 temperature
    from app.core.config import resolve_max_steps
    if max_steps is None:
        max_steps = resolve_max_steps()

    # v3.5: 边际效应递减检测器
    from app.orchestration.compaction import DiminishingReturnsDetector
    dr_detector = DiminishingReturnsDetector()

    logger.info("[agent] %s(task=%s) max_steps=%d (熔断兜底)", agent_name, task_id, max_steps)

    write_paths: list[str] = []
    final_text = ""

    try:
        # ── 3. 组装上下文(三层) ──
        messages = await build_agent_context(
            db, agent=agent, task=task, session=session, system_prompt=system_prompt,
        )
        tool_schemas = tool_registry.all_schemas(template_whitelist)

        # v3.6: 注入 MCP 工具 schemas
        # v1.0: per-agent scope——MCP 工具仅注册到临时 dict，不污染全局 registry
        from app.services.skill_service import get_agent_mcp_servers
        from app.orchestration.tools.mcp_wrapper import build_mcp_tools_for_agent
        _scoped_mcp_tools: dict[str, object] = {}  # name -> tool instance
        try:
            mcp_servers = await get_agent_mcp_servers(db, agent)
            if mcp_servers:
                mcp_tools = build_mcp_tools_for_agent(mcp_servers)
                for mt in mcp_tools:
                    tool_schemas.append(mt.function_schema())
                    # v1.0: 注册到 per-invocation scope，不污染全局
                    _scoped_mcp_tools[mt.name] = mt
                    # 全局 registry 中不存在时才注册（兼容 executor 查找）
                    if not tool_registry.get(mt.name):
                        tool_registry.register(mt)
                logger.info("[agent] %s 注入 %d 个 MCP 工具 (来自 %d 个 server, scoped)",
                           agent_name, len(mcp_tools), len(mcp_servers))
        except Exception:
            logger.warning("[agent] MCP 工具加载失败(非阻塞)", exc_info=True)

        # ── 3.5 v6.1: 目标延续提示（对齐 codex goals/continuation.md）──
        # 对长任务注入 continuation prompt，让模型聚焦完整目标而非缩小范围，
        # 并在完成前做证据驱动的完成审计。
        if task.description and len(task.description) > 0 and max_steps > 20:
            try:
                from app.orchestration.turn_scheduler import get_budget_tracker
                from app.orchestration.prompts import build_continuation_prompt
                _tracker = get_budget_tracker(session_id)
                _goal = build_continuation_prompt(
                    objective=task.description,
                    tokens_used=_tracker.total_tokens,
                    token_budget=_tracker.token_budget,
                )
                messages.append(ChatMessage(role="developer", content=_goal))
                logger.info("[agent] task=%s 注入目标延续提示", task_id)
            except Exception:
                logger.debug("[agent] 目标延续提示注入失败(非阻塞)", exc_info=True)

        # ── 4. agent loop ──
        _called_tool_keys: set[str] = set()  # 已调用过的 tool+args 签名(去重)
        _text_retry_count = 0  # 纯文本工具意图重试次数
        _MAX_TEXT_RETRIES = 3  # 最多引导 3 次,超过后视为最终回答
        _budget_warned = False  # v1.0: token 预算警告只注入一次

        for step in range(1, max_steps + 1):
            # v0.9: 中断检查
            if cancel_event and cancel_event.is_set():
                logger.warning("[agent] task=%s 收到中断信号,退出循环", task_id)
                await task_service.update_task_status(db, task_id, "blocked")
                await db.commit()  # v1.0: 立即提交，确保刷新页面后状态正确
                await _emit_main_card(
                    db, session_id, agent_id, agent_name, task_id, task.title,
                    status="blocked", note="用户已中断任务",
                    agent_role=agent_role,
                )
                await _broadcast_task_updated(session_id, task_id, "blocked")
                return AgentOutput(kind="error", error="任务被用户中断")

            # 每步让出事件循环,避免长循环饿死 WS/HTTP 等其他协程
            await asyncio.sleep(0)
            # v0.9: 广播步骤进度
            await _broadcast_event(session_id, {
                "event": "task.step",
                "payload": {"task_id": task_id, "step": step, "max_steps": max_steps,
                            "agent_id": agent_id, "agent_name": agent_name},
            })

            # v5.3: 移除每步压缩（micro_compact / auto_compact）
            # 问题根因：micro_compact 每步把旧 tool result 替换为占位符，
            # 多轮后消息历史充斥假数据 → 模型困惑 → 退化为 DSML 文本输出。
            # Trae 等成熟工具不做每步压缩，保持消息真实，模型行为稳定。
            # 仅保留 ensure_tool_pairing 作为配对安全网。
            from app.orchestration.compaction import ensure_tool_pairing
            messages = ensure_tool_pairing(messages)

            # v6.4: 移除每步开头的估算式 should_auto_compact 检查（与 agent_loop.py 对齐）
            # 根因：估算 token 与 API 真实 prompt_tokens 偏差大，过早触发 auto_compact。
            # 改为仅在 API 响应后用精确 prompt_tokens 判断（见下方 step 后段）。

            # v3.6: 格式锚定 -- 每 FORM_ANCHOR_INTERVAL 步注入格式提醒，防止多轮后退化
            # v6.1: developer 角色注入（不污染 user 对话流）
            _FORM_ANCHOR_INTERVAL = 16  # v6.0: 放宽格式锚定间隔，减少对正常对话的干扰
            if step > 1 and step % _FORM_ANCHOR_INTERVAL == 0 and _has_tool_calls_in_history(messages):
                logger.debug("[agent] task=%s step=%s 注入格式锚定提醒", task_id, step)
                messages.append(ChatMessage(role="developer", content=TOOL_FORMAT_ANCHOR_MESSAGE))
                # 格式锚定后重新确保配对
                from app.orchestration.compaction import ensure_tool_pairing
                messages = ensure_tool_pairing(messages)

            # v6.2: 工具调用场景低温度（Chat Completions 必须显式低温，否则默认 1.0 破坏 function calling）
            _has_tools = bool(tool_schemas)
            # v6.0: 用 API 副本发请求（折叠较早 tool result），原始 messages 保留完整供压缩
            from app.orchestration.compaction import build_api_copy
            api_messages = build_api_copy(messages)
            request = ChatRequest(
                messages=api_messages,
                model="",
                tools=tool_schemas or None,
                temperature=settings.agent_tool_temperature if _has_tools else settings.agent_text_temperature,
                reasoning_effort=reasoning_effort or (settings.agent_reasoning_effort if _has_tools else None),
            )
            # v4.4: 流式 LLM 调用——实时广播 delta，前端逐字渲染
            try:
                response = await _stream_chat_and_broadcast(
                    provider, request,
                    session_id=session_id, task_id=task_id,
                    agent_id=agent_id, agent_name=agent_name,
                )
            except Exception as api_err:
                err_str = str(api_err)
                logger.warning("[agent] task=%s step=%s 模型调用失败,尝试紧急压缩: %s",
                               task_id, step, err_str[:200])
                # v2.0: 任何模型网关错误都尝试紧急压缩(不仅是 context overflow)
                # GLM 等网关返回 "出现了点小意外" 但实际可能是上下文过大
                try:
                    messages = emergency_compact(messages, agent_context_window)
                    # 额外:强制截断所有 tool result 内容到 500 字符
                    messages = [
                        ChatMessage(
                            role=m.role,
                            content=(m.content[:500] + "...(截断)" if m.role == "tool" and m.content and len(m.content) > 500 else m.content),
                            name=m.name,
                            tool_call_id=m.tool_call_id,
                            tool_calls=m.tool_calls,
                        ) for m in messages
                    ]
                    # v3.6: 紧急压缩后同样确保配对完整 + 使用低温重试
                    messages = ensure_tool_pairing(messages)
                    request = ChatRequest(
                        messages=messages, model="",
                        tools=tool_schemas or None,
                        temperature=settings.agent_tool_temperature if tool_schemas else settings.agent_text_temperature,
                        reasoning_effort=reasoning_effort or (settings.agent_reasoning_effort if tool_schemas else None),
                    )
                    logger.info("[agent] task=%s 紧急压缩后消息数=%d,重试...", task_id, len(messages))
                    response = await provider.chat(request)
                    logger.info("[agent] task=%s 紧急压缩重试成功", task_id)
                except Exception as retry_err:
                    logger.error("[agent] task=%s 紧急压缩重试也失败: %s", task_id, retry_err)
                    raise retry_err
            logger.warning("[agent] task=%s step=%s 模型返回 finish=%s calls=%d",
                           task_id, step, response.finish_reason, len(response.tool_calls or []))

            # v1.0: 接线 BudgetTracker —— 每次模型调用后记录 token 消耗，超预算熔断
            from app.orchestration.turn_scheduler import get_budget_tracker
            _tracker = get_budget_tracker(session_id)
            if response.usage and response.usage.total_tokens > 0:
                if not _tracker.consume_tokens(response.usage.total_tokens):
                    logger.warning("[agent] task=%s token 预算熔断，优雅结束", task_id)
                    final_text = response.content or "(预算耗尽，任务中止)"
                    break
                # v4.3: 使用 API 精确数据广播 token 用量（response.usage 来自网关，精确可靠）
                _api_prompt = response.usage.prompt_tokens if response.usage else 0
                _api_completion = response.usage.completion_tokens if response.usage else 0
                # 计算系统提示词 + 工具定义的 token（一次性精确计算）
                _sys_tokens = _compute_static_overhead(system_prompt, tool_schemas)
                _total_used = _api_prompt + _api_completion + _sys_tokens
                await _broadcast_event(session_id, {
                    "event": "usage.update",
                    "payload": {
                        "agent_name": agent_name,
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "step": step,
                        "prompt_tokens": _api_prompt,
                        "completion_tokens": _api_completion,
                        "total_tokens": _api_prompt + _api_completion,
                        "total_context_used": _total_used,
                        "system_tokens": _sys_tokens,
                        "budget_used": _tracker.total_tokens,
                        "context_tokens_current": _api_prompt,  # v4.7: 当前上下文占用
                        "finish_reason": response.finish_reason,
                        "context_window": agent_context_window,
                        # v1.2: 精确 token 统计（缓存输入 + 推理 token）
                        "cached_input_tokens": getattr(response.usage, 'cached_input_tokens', 0) or 0,
                        "reasoning_tokens": getattr(response.usage, 'reasoning_tokens', 0) or 0,
                    },
                })

            # v6.2/v6.4: 调用后真实占用驱动压缩 —— API 精确 prompt_tokens 达到窗口 90% 即压缩。
            # 比循环开头的估算更可靠（估算只算消息体，API 含 tool schema/system 等真实开销）。
            if response.usage and response.usage.prompt_tokens > 0:
                _real_ratio = response.usage.prompt_tokens / agent_context_window if agent_context_window > 0 else 0
                if _real_ratio >= settings.auto_compact_threshold_ratio:
                    logger.warning(
                        "[agent] task=%s step=%s 真实上下文占用 %.1f%% (p=%d/w=%d) >= %.0f%%，触发自动压缩",
                        task_id, step, _real_ratio * 100, response.usage.prompt_tokens,
                        agent_context_window, settings.auto_compact_threshold_ratio * 100,
                    )
                    from app.orchestration.compaction import auto_compact
                    messages = await auto_compact(messages, agent_context_window, provider)
                else:
                    logger.debug(
                        "[agent] task=%s step=%s 上下文占用 %.1f%% (p=%d/w=%d) < %.0f%%，无需压缩",
                        task_id, step, _real_ratio * 100, response.usage.prompt_tokens,
                        agent_context_window, settings.auto_compact_threshold_ratio * 100,
                    )

            # v4.3: 流式广播模型的推理/思考内容（DeepSeek reasoning / Claude thinking）
            if response.thinking:
                await _stream_agent_thinking(
                    session_id, task_id=task_id, agent_id=agent_id,
                    agent_name=agent_name, thinking=response.thinking,
                )
                # 将思考内容写入 thread（thinking=true，前端可折叠展示）
                await _emit_thread(
                    db, session_id=session_id, thread_id=task_id,
                    agent_id=agent_id, agent_name=agent_name,
                    text=response.thinking,
                    thinking=True,
                )

            # v5.3: 边际效应递减检测 —— 只在真正空转时才结束
            dr_detector.observe(step, response)
            if dr_detector.should_stop():
                logger.info("[agent] task=%s 检测到持续空转,提前结束 (step=%s)", task_id, step)
                final_text = response.content or "(任务执行完毕)"
                break

            # v1.0: Token 预算优雅提示 —— 每个阈值只注入一次，避免污染对话
            # v6.1: developer 角色注入（不污染 user 对话流）
            _token_warning = _maybe_token_budget_warning(messages, agent_context_window)
            if _token_warning and not _budget_warned:
                _budget_warned = True
                messages.append(ChatMessage(role="developer", content=_token_warning))

            # v3.5: 并发执行只读工具 + 错误级联取消
            if response.tool_calls:
                # 循环检测: 仅检测完全相同的 tool+args 连续重复
                _dup_sigs = []
                for tc in response.tool_calls:
                    _tc_key = f"{tc.get('name','')}:{json.dumps(tc.get('arguments',{}),sort_keys=True,ensure_ascii=False)[:200]}"
                    _dup_sigs.append(_tc_key)

                # 全部是之前已调用过的完全相同签名 = 死循环
                _all_dup = all(k in _called_tool_keys for k in _dup_sigs) and len(_dup_sigs) > 0
                for k in _dup_sigs:
                    _called_tool_keys.add(k)

                if _all_dup:
                    logger.warning("[agent] task=%s step=%s 完全重复的工具调用,强制输出", task_id, step)
                    messages.append(ChatMessage(
                        role="developer",  # v6.1: developer 角色注入
                        content=(
                            "You are calling the same tool with the exact same arguments repeatedly.\n"
                            "Produce the result directly based on the information already gathered:\n"
                            "- Use fs_write to write files\n"
                            "- Output text directly for conclusions\n"
                            "Do not keep calling the same tool with identical arguments."
                        ),
                    ))
                    request = ChatRequest(
                        messages=messages, model="", tools=None,
                        temperature=settings.agent_text_temperature,
                    )
                    try:
                        response = await provider.chat(request)
                    except Exception:
                        pass
                    if response.content:
                        final_text = response.content
                        break
                    continue

                # 判断是否全部为只读工具
                # v4.0 修复: 旧版 all() 对空迭代返回 True，导致找不到的工具被误判为只读
                from app.orchestration.tools.registry import tool_registry as _tr
                _found_tools = [
                    _tr.get(tc.get("name", ""))
                    for tc in response.tool_calls
                    if _tr.get(tc.get("name", ""))
                ]
                _all_readonly = (
                    len(_found_tools) == len(response.tool_calls)
                    and len(_found_tools) > 0
                    and all(t.risk_level == "low" for t in _found_tools)
                )

            # 4a. 有 tool_calls → 执行 → 追加消息 → 继续
            if response.tool_calls:
                # v1.3: 中间步骤正文也落库，前端按时间顺序展示在思考块与工具调用之间
                if response.content and response.content.strip():
                    await _emit_thread(
                        db, session_id=session_id, thread_id=task_id,
                        agent_id=agent_id, agent_name=agent_name,
                        text=response.content,
                    )
                # 追加 assistant 消息(含 tool_calls,供 function-calling 往返)
                # v4.0: content 必须为 None（OpenAI 协议要求 assistant 带 tool_calls 时 content 为 null）
                messages.append(ChatMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                ))

                # v3.5: 并发执行只读工具（Claude Code StreamingToolExecutor 策略）
                # v1.0: 引入 Semaphore 控制并发数 + PG 模式独立 DB Session
                # SQLite 模式下禁用并发（避免 database is locked 导致消息丢失）
                if _all_readonly and len(response.tool_calls) > 1 and not _SQLITE_MODE:
                    _cascade_cancel = asyncio.Event()
                    _sem = asyncio.Semaphore(min(10, len(response.tool_calls)))

                    async def _exec_one_readonly(tc_item):
                        async with _sem:
                            if _cascade_cancel.is_set():
                                return None
                            _t_name = tc_item.get("name", "")
                            _t_args = tc_item.get("arguments", {}) or {}
                            _t_id = tc_item.get("id", "") or "call_default"  # 模型返回的 id
                            _t_key = "tc_" + uuid.uuid4().hex[:12]  # 内部追踪 key
                            await _broadcast_event(session_id, {
                                "event": "tool.call",
                                "payload": {"task_id": task_id, "agent_id": agent_id,
                                            "agent_name": agent_name, "tool": _t_name,
                                            "args_preview": _truncate_args(_t_args, 200)},
                            })
                            # v1.0: SQLite 模式用共享 session+锁，PG 模式用独立 session
                            if _SQLITE_MODE:
                                await _emit_thread_tool_call(
                                    db, session_id=session_id, thread_id=task_id,
                                    agent_id=agent_id, agent_name=agent_name,
                                    tool=_t_name, args=_t_args, call_key=_t_key,
                                )
                            else:
                                from app.persistence.database import async_session_factory
                                async with async_session_factory() as _db_local:
                                    await _emit_thread_tool_call(
                                        _db_local, session_id=session_id, thread_id=task_id,
                                        agent_id=agent_id, agent_name=agent_name,
                                        tool=_t_name, args=_t_args, call_key=_t_key,
                                    )
                                    await _db_local.commit()
                            _ctx = ToolContext(
                                workspace_root=workspace_root,
                                session_id=session_id, task_id=task_id,
                                agent_id=agent_id, agent_name=agent_name,
                                cancel_event=cancel_event,
                            )
                            _ts0 = time.monotonic()
                            _r = await tool_executor.execute(
                                tool_name=_t_name, args=_t_args, call_key=_t_key,
                                agent=agent, ctx=_ctx,
                                on_approval_request=_make_approval_emitter(session_id),
                            )
                            _dur = int((time.monotonic() - _ts0) * 1000)
                            if not _r.ok:
                                _cascade_cancel.set()
                            return (_t_name, _t_key, _t_args, _r, _dur, _t_id)

                    _tasks = [asyncio.create_task(_exec_one_readonly(tc)) for tc in response.tool_calls]
                    # v4.8: 并发工具执行加 60 秒超时，防止单个工具挂起导致整个 agent 阻塞
                    try:
                        _results = await asyncio.wait_for(
                            asyncio.gather(*_tasks, return_exceptions=True),
                            timeout=60.0,
                        )
                    except asyncio.TimeoutError:
                        logger.error("[agent] task=%s 并发工具执行超时，级联取消", task_id)
                        _cascade_cancel.set()
                        for tc in response.tool_calls:
                            messages.append(ChatMessage(
                                role="tool", content="[工具执行超时]",
                                name=tc.get("name", ""), tool_call_id=tc.get("id", ""),
                            ))
                        continue
                    for idx, _r_item in enumerate(_results):
                        _tc_orig = response.tool_calls[idx]
                        _t_id = _tc_orig.get("id", "") or "call_default"
                        _t_name = _tc_orig.get("name", "")
                        if isinstance(_r_item, Exception) or _r_item is None:
                            # v2.0: 失败的工具也必须追加 result!
                            # OpenAI 规范要求每个 tool_call 都有对应的 tool result
                            _err_msg = str(_r_item) if isinstance(_r_item, Exception) else "执行返回空结果"
                            logger.warning("[agent] task=%s tool %s 执行失败: %s", task_id, _t_name, _err_msg[:200])
                            messages.append(ChatMessage(
                                role="tool", content=f"[工具执行失败] {_err_msg[:500]}",
                                name=_t_name, tool_call_id=_t_id,
                            ))
                            continue
                        _t_name2, _t_key, _t_args, _r, _dur, _t_id2 = _r_item
                        await _broadcast_event(session_id, {
                            "event": "tool.result",
                            "payload": {"task_id": task_id, "tool": _t_name2,
                                        "ok": _r.ok, "duration_ms": _dur,
                                        "output_preview": (_r.output or _r.error)[:300]},
                        })
                        await _emit_thread_tool_result(
                            db, session_id=session_id, thread_id=task_id,
                            agent_id=agent_id, agent_name=agent_name,
                            tool=_t_name2, call_key=_t_key,
                            ok=_r.ok, output=_r.output, error=_r.error,
                            duration_ms=_dur,
                        )
                        messages.append(ChatMessage(
                            role="tool", content=_truncate_output(_r.output or _r.error, tool_name=_t_name2),
                            name=_t_name2, tool_call_id=_t_id2,
                        ))
                    continue

                # 串行执行（含写操作工具）
                for tc in response.tool_calls:
                    tool_name = tc.get("name", "")
                    args = tc.get("arguments", {}) or {}
                    call_key = "tc_" + uuid.uuid4().hex[:12]
                    tc_model_id = tc.get("id", "") or "call_default"  # 模型返回的 id,用于 tool_call_id 匹配

                    # v0.9: 中断检查(工具执行前)
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("任务被用户中断")

                    # 广播 tool.call 事件
                    await _broadcast_event(session_id, {
                        "event": "tool.call",
                        "payload": {"task_id": task_id, "agent_id": agent_id,
                                    "agent_name": agent_name, "tool": tool_name,
                                    "args_preview": _truncate_args(args, 200)},
                    })

                    # 写 thread:工具调用卡(结构化,前端可做折叠卡片)
                    await _emit_thread_tool_call(
                        db, session_id=session_id, thread_id=task_id,
                        agent_id=agent_id, agent_name=agent_name,
                        tool=tool_name, args=args, call_key=call_key,
                    )

                    ctx = ToolContext(
                        workspace_root=workspace_root,
                        session_id=session_id, task_id=task_id,
                        agent_id=agent_id, agent_name=agent_name,
                        cancel_event=cancel_event,
                    )
                    _tool_start_ts = time.monotonic()
                    try:
                        # v4.8.2: 串行工具执行加 60 秒超时，防止单个工具挂起
                        result = await asyncio.wait_for(
                            tool_executor.execute(
                                tool_name=tool_name, args=args, call_key=call_key,
                                agent=agent, ctx=ctx,
                                on_approval_request=_make_approval_emitter(session_id),
                            ),
                            timeout=60.0,
                        )
                    except asyncio.TimeoutError:
                        logger.error("[agent] task=%s tool %s 执行超时", task_id, tool_name)
                        from app.orchestration.tools.base import ToolResult
                        result = ToolResult(ok=False, output="", error=f"[工具执行超时(60s)] {tool_name}")
                    except Exception as _tool_exc:
                        logger.warning("[agent] task=%s tool %s 执行异常: %s", task_id, tool_name, _tool_exc)
                        from app.orchestration.tools.base import ToolResult
                        result = ToolResult(ok=False, output="", error=f"[工具执行异常] {_tool_exc}")
                    _duration_ms = int((time.monotonic() - _tool_start_ts) * 1000)

                    # 记录 fs_write 产物路径
                    if tool_name == "fs_write" and result.ok and "path" in args:
                        write_paths.append(args["path"])

                    # 广播 tool.result 事件
                    await _broadcast_event(session_id, {
                        "event": "tool.result",
                        "payload": {"task_id": task_id, "tool": tool_name,
                                    "ok": result.ok, "duration_ms": _duration_ms,
                                    "output_preview": (result.output or result.error)[:300]},
                    })

                    # 写 thread:工具结果(结构化,与 tool_call 同 call_key 关联)
                    await _emit_thread_tool_result(
                        db, session_id=session_id, thread_id=task_id,
                        agent_id=agent_id, agent_name=agent_name,
                        tool=tool_name, call_key=call_key,
                        ok=result.ok, output=result.output, error=result.error,
                        duration_ms=_duration_ms,
                    )

                    # 追加 tool 结果给 LLM(截断防止上下文超限)
                    _tool_content = _truncate_output(result.output or result.error, tool_name=tool_name)
                    messages.append(ChatMessage(
                        role="tool", content=_tool_content,
                        name=tool_name, tool_call_id=tc_model_id,
                    ))
                # 继续下一轮
                continue

            # 4b. 纯文本产出 → 检测是否为工具调用意图（DeepSeek/GLM 常见退化模式）
            # v6.2: 移除 finish_reason != "stop" 限制 ——
            # 模型常以 stop + 命令文本（"cd xxx && echo ..."）输出工具意图，
            # 若要求非 stop 才检测会完全绕过兜底，把命令当成最终回答直接结束。
            # _detect_tool_call_intent 内部已严格（v5.3 收紧），且 _text_retry_count=3 兜底，
            # 正常最终回答不会误判。
            if (not response.tool_calls and response.content):
                _intent = _detect_tool_call_intent(response.content)
                if _intent and _text_retry_count < _MAX_TEXT_RETRIES and step < max_steps:
                    _text_retry_count += 1
                    logger.info(
                        "[agent] task=%s step=%s 检测到纯文本工具意图(退化),引导重试 (%d/%d)",
                        task_id, step, _text_retry_count, _MAX_TEXT_RETRIES,
                    )
                    # 追加 assistant 纯文本 + developer 引导消息（v6.1: 英文化 + developer 角色）
                    messages.append(ChatMessage(role="assistant", content=response.content))
                    _retry_msg = (
                        "You just output plain text instead of a tool call. You MUST call tools through structured function calling.\n\n"
                        "Correct example (JSON tool_call):\n"
                        '- Read file: {"name":"fs_read","arguments":{"path":"src/main.py"}}\n'
                        '- Search code: {"name":"fs_grep","arguments":{"pattern":"class User","path":"src/"}}\n'
                        '- Write file: {"name":"fs_write","arguments":{"path":"output.txt","content":"..."}}\n\n'
                        "Never output like this (wrong):\n"
                        "- \"Let me read src/main.py file...\"\n"
                        "- \"Next I need to check config.py content\"\n\n"
                        f"You have described tool calls in plain text {_text_retry_count} times in a row. "
                        "If this fails again, the task may be aborted. Make the function call directly!"
                    ) if _text_retry_count >= 2 else (
                        "You just output plain text instead of a tool call. You MUST call tools through structured function calling.\n\n"
                        "Correct example (JSON tool_call):\n"
                        '- Read file: {"name":"fs_read","arguments":{"path":"src/main.py"}}\n'
                        '- Search code: {"name":"fs_grep","arguments":{"pattern":"class User","path":"src/"}}\n'
                        '- Write file: {"name":"fs_write","arguments":{"path":"output.txt","content":"..."}}\n\n'
                        "Never output like this (wrong):\n"
                        "- \"Let me read src/main.py file...\"\n"
                        "- \"Next I need to check config.py content\"\n\n"
                        "Make the function call now!"
                    )
                    messages.append(ChatMessage(role="developer", content=_retry_msg))
                    continue

            # 真正的最终回答 → 结束
            final_text = response.content or ""
            # v1.0: 流式广播最终文本（分块推送，前端实时渲染）
            if final_text:
                await _stream_broadcast_text(
                    session_id, task_id=task_id, agent_id=agent_id,
                    agent_name=agent_name, text=final_text,
                )
            break
        else:
            # 超过 max_steps → 视为"受阻",绝不假装完成
            logger.warning("agent %s task %s 达到 max_steps=%s,标记受阻", agent_id, task_id, max_steps)
            partial = (response.content if 'response' in locals() else "") or "(无中间产出)"
            await task_service.update_task_status(db, task_id, "blocked", note=f"未在 {max_steps} 步内完成,需要重试或调整需求")
            await _emit_main_card(
                db, session_id, agent_id, agent_name, task_id, task.title,
                status="blocked",
                note=f"未在 {max_steps} 步内完成,需要重试或调整需求",
                agent_role=agent_role,
            )
            await _broadcast_task_updated(session_id, task_id, "blocked")
            await _broadcast_agent_status(session_id, agent_id, agent_name,
                                          status="failed", task_id=task_id)
            # 写 thread 记录最后状态(便于诊断)
            await _emit_thread(
                db, session_id=session_id, thread_id=task_id,
                agent_id=agent_id, agent_name=agent_name,
                text=f"⚠️ 达到步数上限({max_steps}),任务标记为受阻,未产出最终成果。\n\n最后状态:\n{partial}",
            )
            return AgentOutput(
                kind="error",
                error=f"达到步数上限({max_steps}),任务未完成",
            )

        # ── 5. 产出写 thread ──
        await _emit_thread(
            db, session_id=session_id, thread_id=task_id,
            agent_id=agent_id, agent_name=agent_name,
            text=final_text or "(空产出)",
        )

        # v1.0: 将最终产出也发送到主聊天窗口，让用户直接看到结果
        if final_text and len(final_text.strip()) > 10:
            async with _get_session_lock(session_id):
                await session_service.create_message(
                    db, session_id=session_id, sender_type=SenderType.AGENT, sender_id=agent_id,
                    msg_type=MsgType.TEXT,
                    content={"text": final_text, "agent_name": agent_name},
                    thread_id=None,
                )
                await db.commit()

        # ── 6. 抽取产物 ──
        artifact_ids = await extract_and_persist_artifacts(
            db, task_id=task_id, text=final_text, write_paths=write_paths,
        )

        # ── 6.5 v3.1: 提取工作记忆(learned_facts) ──
        try:
            await _extract_and_save_learned_facts(
                db, agent=agent, session_id=session_id,
                task_id=task_id, task_title=task.title,
                final_text=final_text, write_paths=write_paths,
            )
        except Exception:
            logger.debug("[agent] 工作记忆提取失败(非阻塞)", exc_info=True)

        # ── 7. 审查门(v2.5: 仅 needs_review=True 才走审查) ──
        needs_review = bool(getattr(task, "needs_review", False))

        if needs_review:
            await task_service.update_task_status(db, task_id, "in_review")
            await _emit_main_card(
                db, session_id, agent_id, agent_name, task_id, task.title,
                status="in_review",
                note=f"产出就绪,待审核({len(artifact_ids)} 个产物)",
                agent_role=agent_role,
            )
            await _broadcast_task_updated(session_id, task_id, "in_review")

            from app.orchestration.review import review_task
            final_status = await review_task(
                db, task=task, session=session,
                producing_agent=agent, producing_output=final_text,
                write_paths=write_paths,
            )
            await _broadcast_task_updated(session_id, task_id, final_status)
        else:
            # 不需要审查 → 直接标记完成
            final_status = "done"
            await task_service.update_task_status(db, task_id, "done")
            await _emit_main_card(
                db, session_id, agent_id, agent_name, task_id, task.title,
                status="done",
                note=f"完成({len(artifact_ids)} 个产物)" if artifact_ids else "完成",
                agent_role=agent_role,
            )
            await _broadcast_task_updated(session_id, task_id, "done")

        await _broadcast_agent_status(
            session_id, agent_id, agent_name,
            status="done" if final_status == "done" else "failed",
            task_id=task_id,
        )

        return AgentOutput(
            kind="message", text=final_text, artifact_ids=artifact_ids,
        )

    except Exception as e:
        err_msg = str(e)
        logger.exception("agent loop 异外 task=%s agent=%s", task_id, agent_id)

        await task_service.update_task_status(db, task_id, "blocked", note=f"执行异常:{err_msg[:200]}")
        await _emit_main_card(
            db, session_id, agent_id, agent_name, task_id, task.title,
            status="blocked", note=f"执行异常:{err_msg[:150]}",
            agent_role=agent_role,
        )
        # v4.8.3: 广播时带上错误信息，前端立即显示
        await _broadcast_task_updated(session_id, task_id, "blocked", f"执行异常:{err_msg[:150]}")
        await _broadcast_agent_status(session_id, agent_id, agent_name,
                                      status="failed", task_id=task_id)
        # 重新抛出异常，让 scheduler._run_one 的瞬态错误重试逻辑生效
        raise


# ───────────────────────── 辅助 ─────────────────────────


async def _extract_and_save_learned_facts(
    db: AsyncSession, *, agent, session_id: int,
    task_id: int, task_title: str,
    final_text: str, write_paths: list[str],
) -> None:
    """v3.1: 从任务执行结果中提取关键事实，保存到 Agent 的 learned_facts。

    同一个 Agent 在后续任务中会看到这些事实，实现跨任务经验传承。
    例如：发现了某个 API 的用法、某个配置的坑、代码约定等。
    """
    if not final_text or len(final_text) < 50:
        return

    provider = get_model_registry().get_default_provider()
    if provider is None:
        return

    context_brief = f"任务: {task_title}\n产出摘要: {final_text[:2000]}"
    if write_paths:
        context_brief += f"\n涉及文件: {', '.join(write_paths[:10])}"

    try:
        request = ChatRequest(
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        "你是一个经验提取助手。从以下任务执行记录中提取 1-3 条对后续工作有价值的关键事实。"
                        "只提取真正有参考价值的信息，如：\n"
                        "- 发现的代码模式/约定\n"
                        "- 重要文件位置和作用\n"
                        "- 遇到的问题和解决方案\n"
                        "- 技术决策和理由\n"
                        "每条控制在 100 字以内。如果没有值得提取的，返回空。"
                        "用 JSON 数组格式返回，例如: [{\"fact\": \"...\"}]"
                    ),
                ),
                ChatMessage(role="user", content=context_brief),
            ],
            model="",
        )
        resp = await provider.chat(request)
        import json as _json
        raw = resp.content or ""
        # 容错解析 JSON 数组
        import re as _re
        match = _re.search(r"\[.*\]", raw, _re.DOTALL)
        if not match:
            return
        facts_list = _json.loads(match.group(0))
        if not isinstance(facts_list, list) or not facts_list:
            return

        # 追加到 agent.learned_facts
        import time as _time
        ts = _time.strftime("%Y-%m-%dT%H:%M:%S")
        existing = list(agent.learned_facts or [])
        for f in facts_list[:3]:
            if isinstance(f, dict) and f.get("fact"):
                existing.append({
                    "session_id": session_id,
                    "task_id": task_id,
                    "text": str(f["fact"])[:200],
                    "ts": ts,
                })

        # 限制每个 agent 最多保留 50 条事实（避免无限增长）
        if len(existing) > 50:
            existing = existing[-50:]

        agent.learned_facts = existing
        await db.flush()
        logger.info("[agent] 提取 %d 条工作记忆 agent=%s task=%s", len(facts_list[:3]), agent.name, task_id)
    except Exception:
        logger.debug("[agent] 工作记忆提取异常(非阻塞)", exc_info=True)


def _truncate_args(args: dict, limit: int = 800) -> str:
    """JSON 美化序列化工具参数,避免 repr(dict) 看起来像乱码。"""
    try:
        s = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "\n...(已截断)"


# v4.3: 上下文精确管理 —— 静态开销缓存
_static_overhead_cache: dict[str, int] = {}

def _compute_static_overhead(system_prompt: str, tool_schemas: list | None) -> int:
    """v4.4: 精确计算工具定义的 token 开销（带缓存）。
    
    注意：API 返回的 prompt_tokens 已包含 system 消息，因此这里只计算 tools 定义。
    system prompt 不计入以避免重复。
    """
    cache_key = f"{hash(str(tool_schemas))}"
    if cache_key in _static_overhead_cache:
        return _static_overhead_cache[cache_key]
    
    from app.orchestration.token_counter import precise_token_count
    tokens = 0
    if tool_schemas:
        import json
        tools_json = json.dumps(tool_schemas, ensure_ascii=False)
        tokens += precise_token_count(tools_json)
    _static_overhead_cache[cache_key] = tokens
    return tokens


def _maybe_token_budget_warning(messages: list[ChatMessage], context_window: int) -> str | None:
    """v6.1: Token budget graceful warning (English, aligned with codex TokenBudgetRemainingContext).

    When messages approach the context window limit, inject a wrap-up instruction so the
    model proactively produces the final result instead of hitting the wall.
    """
    from app.orchestration.token_counter import precise_token_count
    used = sum(precise_token_count(m.content or "") for m in messages)
    ratio = used / context_window if context_window > 0 else 0
    remaining = max(0, int(context_window - used))

    if ratio > 0.90:
        return (
            f"Context space is nearly exhausted (currently using {ratio:.0%}, ~{remaining} tokens left). "
            "Produce the final result immediately based on available information. Do not call more tools."
        )
    if ratio > 0.80:
        return (
            f"Context space is {ratio:.0%} used (~{remaining} tokens left). "
            "Keep subsequent actions under control and prioritize the core objective. Avoid extensive tool calls."
        )
    return None


# v1.0: 流式广播——将最终文本分块推送给前端
_STREAM_CHUNK_SIZE = 80  # 每次推送的字符数
_STREAM_INTERVAL = 0.02  # 推送间隔(秒)，模拟逐字输出效果


async def _stream_broadcast_text(
    session_id: int, *, task_id: int, agent_id: int, agent_name: str, text: str,
) -> None:
    """v1.0: 将最终文本分块广播，前端实时渲染逐字输出。

    事件协议:
    - token.delta: {"task_id", "agent_id", "agent_name", "delta": "..."}
    - token.done:  {"task_id", "agent_id", "agent_name", "full_text": "..."}
    """
    # 分块推送
    for i in range(0, len(text), _STREAM_CHUNK_SIZE):
        chunk = text[i:i + _STREAM_CHUNK_SIZE]
        await _broadcast_event(session_id, {
            "event": "token.delta",
            "payload": {
                "task_id": task_id, "agent_id": agent_id,
                "agent_name": agent_name, "delta": chunk,
            },
        })
        # 短暂延迟，让前端有时间渲染（也避免 WS 洪泛）
        if i + _STREAM_CHUNK_SIZE < len(text):
            await asyncio.sleep(_STREAM_INTERVAL)

    # 推送完成事件
    await _broadcast_event(session_id, {
        "event": "token.done",
        "payload": {
            "task_id": task_id, "agent_id": agent_id,
            "agent_name": agent_name, "full_text": text,
        },
    })


# v4.3: 流式广播——将模型的推理/思考内容分块推送给前端
_THINKING_CHUNK_SIZE = 60  # 思考内容分块更细，提升流式感


async def _stream_agent_thinking(
    session_id: int, *, task_id: int, agent_id: int, agent_name: str, thinking: str,
) -> None:
    """v4.3: 将模型的推理/思考内容分块广播，前端实时展示可折叠的"深度思考"块。

    事件协议:
    - thinking.delta: {"agent_id", "turn_id", "delta": "..."}
    - thinking.done:  {"agent_id", "turn_id", "full_text": "..."}
    """
    for i in range(0, len(thinking), _THINKING_CHUNK_SIZE):
        chunk = thinking[i:i + _THINKING_CHUNK_SIZE]
        await _broadcast_event(session_id, {
            "event": "thinking.delta",
            "payload": {
                "agent_id": agent_id, "turn_id": task_id,
                "delta": chunk,
            },
        })
        if i + _THINKING_CHUNK_SIZE < len(thinking):
            await asyncio.sleep(_STREAM_INTERVAL)

    # 推送思考完成事件
    await _broadcast_event(session_id, {
        "event": "thinking.done",
        "payload": {
            "agent_id": agent_id, "turn_id": task_id,
            "full_text": thinking,
        },
    })


# v4.4: 真正的流式 LLM 调用——实时广播 content/thinking delta，替代伪流式分块
async def _stream_chat_and_broadcast(
    provider,
    request: ChatRequest,
    *,
    session_id: int,
    task_id: int,
    agent_id: int,
    agent_name: str,
) -> "ChatResponse":
    """v4.4: 调用 provider 的 stream_structured 实现真正的逐 token 流式渲染。

    对每个到达的 delta 立即广播 WS 事件，前端逐字渲染。
    流结束后返回完整的 ChatResponse（含 tool_calls、usage 等）。
    """
    from app.models.schemas import ChatResponse, Usage as UsageModel

    full_content = ""
    full_thinking = ""
    tool_calls = []
    finish_reason = "stop"
    usage = UsageModel()

    try:
        async for event in provider.stream_structured(request):
            if event["type"] == "thinking":
                delta = event.get("delta", "")
                full_thinking += delta
                await _broadcast_event(session_id, {
                    "event": "thinking.delta",
                    "payload": {
                        "agent_id": agent_id, "turn_id": task_id,
                        "delta": delta,
                    },
                })
            elif event["type"] == "content":
                delta = event.get("delta", "")
                full_content += delta
                await _broadcast_event(session_id, {
                    "event": "token.delta",
                    "payload": {
                        "agent_id": agent_id, "turn_id": task_id,
                        "delta": delta,
                    },
                })
            elif event["type"] == "done":
                full_content = event.get("content") or full_content
                full_thinking = event.get("thinking") or full_thinking
                tool_calls = event.get("tool_calls", [])
                finish_reason = event.get("finish_reason", "stop")
                usage = event.get("usage", UsageModel())
                break
    except Exception:
        logger.exception("[agent] task=%s 流式调用异常，回退非流式", task_id)
        # 回退：使用传统非流式调用
        response = await provider.chat(request)
        full_content = response.content or ""
        full_thinking = response.thinking or ""
        tool_calls = response.tool_calls or []
        finish_reason = response.finish_reason
        usage = response.usage

    # 发送完成事件
    await _broadcast_event(session_id, {
        "event": "thinking.done",
        "payload": {
            "agent_id": agent_id, "turn_id": task_id,
            "full_text": full_thinking,
        },
    }) if full_thinking else None
    await _broadcast_event(session_id, {
        "event": "token.done",
        "payload": {
            "agent_id": agent_id, "turn_id": task_id,
            "full_text": full_content,
        },
    }) if full_content else None

    return ChatResponse(
        content=full_content or None,
        thinking=full_thinking or None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
    )


async def _emit_thread(
    db: AsyncSession, *, session_id: int, thread_id: int,
    agent_id: int, agent_name: str, text: str, thinking: bool = False,
) -> None:
    """写入子会话文本消息。
    
    v4.3: thinking=True 时写入思考内容（前端显示为可折叠的"深度思考"块）。
    """
    content = {"text": text, "agent_name": agent_name}
    if thinking:
        content["thinking"] = True
    async with _get_session_lock(session_id):
        await session_service.create_message(
            db, session_id=session_id, sender_type=SenderType.AGENT, sender_id=agent_id,
            msg_type=MsgType.TEXT, content=content,
            thread_id=thread_id,
        )
        await db.commit()


async def _emit_thread_tool_call(
    db: AsyncSession, *, session_id: int, thread_id: int,
    agent_id: int, agent_name: str,
    tool: str, args: dict, call_key: str,
) -> None:
    """结构化工具调用消息。"""
    async with _get_session_lock(session_id):
        await session_service.create_message(
            db, session_id=session_id, sender_type=SenderType.AGENT, sender_id=agent_id,
            msg_type=MsgType.TOOL_CALL,
            content={
                "tool": tool, "args": args, "call_key": call_key,
                "agent_name": agent_name,
            },
            thread_id=thread_id,
        )
        await db.commit()


async def _emit_thread_tool_result(
    db: AsyncSession, *, session_id: int, thread_id: int,
    agent_id: int, agent_name: str,
    tool: str, call_key: str,
    ok: bool, output: str, error: str, duration_ms: int,
) -> None:
    """结构化工具结果消息。"""
    async with _get_session_lock(session_id):
        await session_service.create_message(
            db, session_id=session_id, sender_type=SenderType.AGENT, sender_id=agent_id,
            msg_type=MsgType.TOOL_RESULT,
            content={
                "tool": tool, "call_key": call_key,
                "ok": ok, "output": output, "error": error,
                "duration_ms": duration_ms, "agent_name": agent_name,
            },
            thread_id=thread_id,
        )
        await db.commit()


async def _emit_main_card(
    db: AsyncSession, session_id: int, agent_id: int, agent_name: str,
    task_id: int, task_title: str, *, status: str, note: str,
    agent_role: str = "",
) -> None:
    """主群任务卡片。"""
    content = {
        "task_id": task_id, "title": task_title, "status": status,
        "assignee": agent_name, "note": note, "agent_name": agent_name,
    }
    if agent_role:
        content["agent_role"] = agent_role
    async with _get_session_lock(session_id):
        await session_service.create_message(
            db, session_id=session_id, sender_type=SenderType.AGENT, sender_id=agent_id,
            msg_type=MsgType.TASK_CARD,
            content=content,
            thread_id=None,
        )
        await db.commit()


async def _broadcast_task_updated(session_id: int, task_id: int, status: str, note: str = "") -> None:
    event = {
        "event": "task.updated",
        "payload": {"task_id": task_id, "status": status, "note": note} if note else {"task_id": task_id, "status": status},
    }
    try:
        await ws_manager.broadcast(session_id, event)
    except Exception:
        logger.debug("task.updated 广播失败(可能无连接)", exc_info=True)


async def _broadcast_event(session_id: int, event: dict) -> None:
    """v0.9: 通用事件广播(task.step/tool.call/tool.result 等)。"""
    try:
        await ws_manager.broadcast(session_id, event)
    except Exception:
        logger.debug("事件广播失败(可能无连接): %s", event.get("event"), exc_info=True)


async def _broadcast_agent_status(
    session_id: int, agent_id: int, agent_name: str, *,
    status: str, task_id: int | None = None,
    step: int | None = None, max_steps: int | None = None, tool: str | None = None,
) -> None:
    """v0.9.1: 广播 agent 视角的聚合状态,前端 AgentPanel 实时渲染。

    status ∈ {running, done, failed, idle}
    """
    payload: dict = {
        "agent_id": agent_id, "agent_name": agent_name, "status": status,
    }
    if task_id is not None:
        payload["task_id"] = task_id
    if step is not None:
        payload["step"] = step
    if max_steps is not None:
        payload["max_steps"] = max_steps
    if tool is not None:
        payload["tool"] = tool
    await _broadcast_event(session_id, {"event": "agent.status", "payload": payload})


def _make_approval_emitter(session_id: int):
    """构造审批请求回调:仅广播 WS 事件（不创建聊天消息，由前端 ComposerBox 内联展示）。"""

    async def _emit(approval_id: str, detail: dict) -> None:
        # v4.2: 不再创建审批聊天消息，改为纯 WS 广播
        # 前端在 ComposerBox 中以内联 banner 展示，审批后自动消失
        await ws_manager.broadcast(
            session_id,
            {"event": "approval.request", "payload": {"approval_id": approval_id, "detail": detail}},
        )

    return _emit

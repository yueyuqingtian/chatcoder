"""Agent 推理循环（v2：主/子代理统一，当前实际生效实现）。

职责：流式模型调用 + 思考/文本广播 + 工具执行 + token 预算 + 产物抽取 + 回滚写盘埋点。
由 engine.start_turn（主代理）与 subagent.SubagentManager（子代理）统一调用。
旧版 agent_runtime.py（v0.1 团队编排路径）已废弃删除。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import MsgType, SenderType
from app.gateway.ws import manager as ws_manager
from app.models.registry import get_model_registry
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.agent_events import broadcast
from app.orchestration.tools import ToolContext, tool_executor
from app.orchestration.tools.registry import tool_registry
from app.services import message_service, rollback_service, task_service

logger = logging.getLogger(__name__)

MAX_TOOL_OUTPUT_CHARS = 16000  # v15: 与 fs_read 上限(settings.tool_output_chars_read)对齐；历史上曾为 8000
_STREAM_CHUNK_SIZE = 80
_STREAM_INTERVAL = 0.01

# v9: 写盘工具集合——执行时记录前后内容（精确回滚依据）
_WRITE_TOOLS = ("fs_write", "editor_apply_diff", "multi_file_edit")


def _truncate_output(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if not text or len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n\n... [已截断,原始 {len(text)} 字符] ...\n\n" + text[-half:]


# v19: 部分网关把思考内容以 <thinking>…</thinking> 标签混入正文文本，
# 落库/广播前剥离，思考部分并入 thinking 消息（ThinkingBlock 展示），正文只保留干净文本。
_INLINE_THINKING_RE = re.compile(r"<(?:thinking|thought)>(.*?)</(?:thinking|thought)>", re.DOTALL | re.IGNORECASE)


def _split_inline_thinking(text: str) -> tuple[str, str]:
    """剥离正文中的内联思考标签，返回 (干净正文, 思考内容)。"""
    if not text or ("<thinking" not in text.lower() and "<thought" not in text.lower()):
        return text or "", ""
    parts = _INLINE_THINKING_RE.findall(text)
    clean = _INLINE_THINKING_RE.sub("", text).strip()
    return clean, "\n".join(p.strip() for p in parts if p.strip())


def _truncate_args(args: dict, limit: int = 800) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "\n...(已截断)"


def _line_change_stat(before: str | None, after: str | None) -> tuple[int, int]:
    """行级变更统计（v2.2，对齐 ZCode computeLineChangeStat 简化版）。

    返回 (additions, deletions)：按行 diff 计数（SequenceMatcher opcode 聚合）。
    """
    if before is None or after is None:
        return (0, 0)
    try:
        import difflib
        diff = list(difflib.SequenceMatcher(
            None, before.splitlines(), after.splitlines(),
        ).get_opcodes())
        additions = sum(
            (b2 - b1) for tag, _a1, _a2, b1, b2 in diff
            if tag in ("insert", "replace")
        )
        deletions = sum(
            (a2 - a1) for tag, a1, a2, _b1, _b2 in diff
            if tag in ("delete", "replace")
        )
        return (int(additions), int(deletions))
    except Exception:
        return (0, 0)


@dataclass
class AgentOutput:
    kind: str  # message | done | error | cancelled | skipped
    text: str = ""
    error: str = ""
    artifact_ids: list[int] = field(default_factory=list)


async def run_agent_loop(
    db: AsyncSession,
    *,
    session_id: int,
    turn_id: int,
    agent,
    context_messages: list[ChatMessage],
    tool_schemas: list[dict],
    workspace: str,
    max_steps: int | None = None,
    cancel_event: asyncio.Event | None = None,
    token_budget: int | None = None,
    subagent_context: dict | None = None,
    reasoning_effort: str | None = None,
    task_id: int | None = None,
    model_id: int | None = None,
    multimodal: bool = False,
) -> AgentOutput:
    """运行单个 agent 推理循环。

    context_messages 已由 context_manager 组装（system + developer + 历史 + 指令）。
    subagent_context: 主代理专用，含子代理管理能力（spawn_subagent/collect_results 工具）。
    multimodal: 当前模型支持图片输入时，read_attachment/view_image 的图片结果
    会以 image_url 内容块追加一条 user 消息，让模型真正"看到"图片（v15）。
    """
    agent_id = agent.id
    agent_name = agent.name
    thread_id = None if agent.kind == "main" else agent.id  # 子代理消息进自己的线程

    # v2.2 (对齐 zcode 3.12): 会话权限模式（executor 审批门前决策）
    permission_mode = "default"
    try:
        from app.persistence.models.message import Session as _Session
        _sess = await db.get(_Session, session_id)
        permission_mode = getattr(_sess, "permission_mode", None) or "default"
    except Exception:
        logger.warning("[agent] turn=%s 读取会话权限模式失败，用 default", turn_id, exc_info=True)

    # 解析 provider（v10: 会话级模型覆盖优先——engine 传入 session.model_id，
    # 优先于 agent.model_id，解决"配置无默认模型 + 页面会话选择模型"不可用问题）
    registry = get_model_registry()
    provider, reason = None, None
    if model_id is not None:
        from app.persistence.models.model_reg import Model
        _model = await db.get(Model, model_id)
        provider, reason = await registry.get_provider_for_model(db, _model)
    if provider is None:
        provider, reason = await registry.get_provider_for_agent(db, agent)
    if provider is None:
        await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                              agent_id=agent_id, agent_name=agent_name,
                              msg_type=MsgType.ERROR,
                              content={"text": f"模型不可用({reason})", "agent_name": agent_name})
        return AgentOutput(kind="skipped", error=reason)

    if max_steps is None:
        max_steps = settings.agent_max_steps

    # v6.4: 启动时打印压缩阈值诊断日志
    try:
        from app.orchestration.token_counter import get_agent_context_window
        _diag_window = await get_agent_context_window(db, agent)
        if _diag_window < 100000:
            logger.warning(
                "[agent] turn=%s agent_window=%d 过小(<100k)，用默认 500000 兜底",
                turn_id, _diag_window,
            )
            _diag_window = 500000
        logger.info(
            "[agent] turn=%s agent_context_window=%d, 压缩阈值=%.0f%% (%d tokens)",
            turn_id, _diag_window,
            settings.auto_compact_threshold_ratio * 100,
            int(_diag_window * settings.auto_compact_threshold_ratio),
        )
    except Exception:
        logger.warning("[agent] turn=%s 压缩阈值诊断日志获取失败", turn_id, exc_info=True)

    await broadcast(session_id, {
        "event": "agent.started",
        "payload": {"agent_id": agent_id, "kind": agent.kind, "name": agent_name,
                    "turn_id": turn_id},
    })
    await broadcast(session_id, {
        "event": "agent.updated",
        "payload": {"agent_id": agent_id, "status": "running"},
    })

    messages = list(context_messages)
    total_tokens = 0
    final_text = ""
    write_paths: list[str] = []
    # v2.2 (对齐 zcode 3.9): todo 提醒机制——模型维护的执行清单连续 N 步未更新时注入提醒
    _todo_active = False       # 本 turn 是否已创建过清单
    _todo_updated_at_step = 0  # 清单最后一次更新的步数
    # v6.5: 估算校准系数 -- 用 API 真实 prompt_tokens 动态校准估算值。
    # 字节/4 对中文偏高，不同模型/网关分词差异大，静态常量无法精准。
    # 每次拿到 API 真实值就更新系数，用于前置压缩估算，实现自适应精准压缩。
    _calib_factor = 1.0  # real / est，初始1.0（无校准）
    # v2.2 (对齐 zcode 3.14): 重复工具调用检测——工具名+参数签名 → 连续次数
    _call_sigs: dict[str, int] = {}
    # 待注入提醒（下一步循环构建 api_messages 后追加，避免被重建丢弃）
    _pending_reminders: list[str] = []

    try:
        for step in range(1, max_steps + 1):
            if cancel_event and cancel_event.is_set():
                logger.warning("[agent] task turn=%s 收到中断信号", turn_id)
                return AgentOutput(kind="cancelled", error="任务被用户中断")

            await asyncio.sleep(0)

            # 上下文压缩（v6.4: 移除每步开头的估算式 should_auto_compact 检查）
            # 根因：估算 token 与 API 真实 prompt_tokens 偏差大，且当 model.context_window
            # 配置较小时会过早触发 auto_compact（用户反馈 20k 就摘要）。
            # 改为仅在 API 响应后用精确 prompt_tokens 判断（见下方 step 后段）。
            agent_window = 0
            try:
                from app.orchestration.compaction import build_api_copy, ensure_tool_pairing, normalize_tool_sequence
                from app.orchestration.token_counter import get_agent_context_window, estimate_messages_tokens as _est_tokens
                agent_window = await get_agent_context_window(db, agent)
                # v6.4: 最小窗口保护 —— 若 model.context_window 配置过小，用默认值兜底
                # 避免因模型窗口配置错误导致压缩阈值过低、过早摘要
                if agent_window < 100000:
                    logger.warning(
                        "[agent] turn=%s agent_window=%d 过小(<100k)，用默认 500000 兜底",
                        turn_id, agent_window,
                    )
                    agent_window = 500000
                messages = ensure_tool_pairing(messages)
                # v8 根治: 强制 assistant(tool_calls) 后紧跟 tool 结果，杜绝 400
                messages = normalize_tool_sequence(messages)
                # v15: 预算驱动折叠 —— 上下文占用 < api_copy_fold_ratio 时保留全部工具结果
                _fold_budget = int(agent_window * settings.api_copy_fold_ratio)
                api_messages = build_api_copy(messages, fold_budget_tokens=_fold_budget)

                # v6.5: 前置压缩检查 -- 用校准系数调整估算，超阈值则先压缩
                _est_raw = _est_tokens(api_messages)
                _est_prompt = int(_est_raw * _calib_factor)
                _pre_threshold = int(agent_window * settings.auto_compact_threshold_ratio)
                if _est_prompt >= _pre_threshold:
                    logger.info("[agent] turn=%s step=%s 前置估算 prompt=%d (raw=%d calib=%.3f) >= 阈值 %d，调用前压缩", turn_id, step, _est_prompt, _est_raw, _calib_factor, _pre_threshold)
                    await broadcast(session_id, {"event": "compact.started", "payload": {"agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id, "used_tokens": _est_prompt, "context_window": agent_window, "ratio": round(_est_prompt / agent_window * 100, 1)}})
                    from app.orchestration.compaction import auto_compact as _ac_pre
                    messages = await _ac_pre(messages, agent_window, provider)
                    messages = ensure_tool_pairing(messages)
                    messages = normalize_tool_sequence(messages)
                    api_messages = build_api_copy(messages, fold_budget_tokens=int(agent_window * settings.api_copy_fold_ratio))
                    _est_after = int(_est_tokens(api_messages) * _calib_factor)
                    logger.info("[agent] turn=%s step=%s 前置压缩后 prompt=%d -> %d", turn_id, step, _est_prompt, _est_after)
                    await broadcast(session_id, {"event": "usage.update", "payload": {"agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id, "prompt_tokens": _est_after, "completion_tokens": 0, "total_tokens": _est_after, "context_window": agent_window, "usage_source": "est_after_compact", "cached_input_tokens": 0, "reasoning_tokens": 0, "agent_kind": agent.kind}})
                    await broadcast(session_id, {"event": "compact.completed", "payload": {"agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id}})
                else:
                    logger.debug("[agent] turn=%s step=%s 前置估算 prompt=%d (raw=%d calib=%.3f) < 阈值 %d，不压缩", turn_id, step, _est_prompt, _est_raw, _calib_factor, _pre_threshold)
            except Exception:
                logger.warning("[agent] turn=%s step=%s 前置压缩检查失败，降级为原始消息继续", turn_id, step, exc_info=True)
                api_messages = messages

            # v2.2: todo 提醒——存在清单且连续 N 步未更新 → 注入 system 提醒
            # （对齐 ZCode buildTodoReminderBody：防模型"开清单后跑偏"）
            if _todo_active and step - _todo_updated_at_step >= settings.todo_reminder_interval:
                api_messages = [*api_messages, ChatMessage(
                    role="system",
                    content=(
                        "[任务清单提醒] 本任务存在执行清单，但最近几步未调用 todo_write 更新。"
                        "请审视当前进度：已完成步骤标记 completed，进行中步骤标记 in_progress，"
                        "必要时调整清单内容后再继续执行。"
                    ),
                )]
                _todo_updated_at_step = step  # 重置计数，避免每步重复注入
                logger.info(
                    "[agent] turn=%s step=%s 注入 todo 提醒（清单 %d 步未更新）",
                    turn_id, step, settings.todo_reminder_interval,
                )
            # v2.2: 注入待发的重复调用提醒（上一步工具执行中检测到）
            if _pending_reminders:
                api_messages = [*api_messages, *[
                    ChatMessage(role="system", content=r) for r in _pending_reminders
                ]]
                _pending_reminders = []

            request = ChatRequest(
                messages=api_messages, model="",
                tools=tool_schemas or None,
                temperature=settings.agent_tool_temperature if tool_schemas else settings.agent_text_temperature,
                reasoning_effort=reasoning_effort or (settings.agent_reasoning_effort if tool_schemas else None),
                max_tokens=settings.agent_max_output_tokens or None,
            )
            # v6.4 临时诊断：打印实际发送给 API 的消息数和角色分布
            if step == 1:
                _role_counts = {}
                for _m in api_messages:
                    _role_counts[_m.role] = _role_counts.get(_m.role, 0) + 1
                logger.warning(
                    "[agent] turn=%s step=1 发送给API的消息数=%d 角色分布=%s",
                    turn_id, len(api_messages), _role_counts,
                )

            try:
                response = await _stream_chat_and_broadcast(
                    provider, request,
                    session_id=session_id, turn_id=turn_id,
                    agent_id=agent_id, agent_name=agent_name,
                    cancel_event=cancel_event,
                    thread_id=thread_id,
                )
                # v19: 兜底剥离内联思考标签（流式路径已剥离则为 no-op）
                if response.content:
                    _c, _t = _split_inline_thinking(response.content)
                    if _t:
                        response.content = _c or None
                        response.thinking = (response.thinking + "\n" + _t).strip() if response.thinking else _t
                # v6.4: 流式调用被用户中断 → 立即退出循环
                if cancel_event and cancel_event.is_set():
                    logger.warning("[agent] turn=%s 流式调用后检测到中断信号，退出循环", turn_id)
                    final_text = response.content or ""
                    if final_text:
                        await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                              agent_id=agent_id, agent_name=agent_name,
                                              msg_type=MsgType.TEXT,
                                              content={"text": final_text, "agent_name": agent_name})
                    return AgentOutput(kind="cancelled", error="任务被用户中断")
            except Exception as api_err:
                logger.warning("[agent] turn=%s 模型调用失败，尝试紧急压缩: %s", turn_id, str(api_err)[:200])
                try:
                    from app.orchestration.compaction import emergency_compact
                    from app.orchestration.token_counter import get_agent_context_window
                    agent_window = await get_agent_context_window(db, agent)
                    messages = emergency_compact(messages, agent_window)
                    # v8 根治: 紧急压缩后同样规范化工具消息序列，防止孤立 tool / 夹层消息
                    messages = normalize_tool_sequence(messages)
                    request = ChatRequest(
                        messages=messages, model="", tools=tool_schemas or None,
                        temperature=settings.agent_tool_temperature if tool_schemas else settings.agent_text_temperature,
                        max_tokens=settings.agent_max_output_tokens or None,
                    )
                    response = await provider.chat(request)
                    # v19: 紧急压缩非流式重试路径同样剥离内联思考标签
                    if response.content:
                        _c, _t = _split_inline_thinking(response.content)
                        if _t:
                            response.content = _c or None
                            response.thinking = (response.thinking + "\n" + _t).strip() if response.thinking else _t
                except Exception as retry_err:
                    logger.error("[agent] turn=%s 紧急压缩重试失败: %s", turn_id, retry_err)
                    await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                          agent_id=agent_id, agent_name=agent_name,
                                          msg_type=MsgType.ERROR,
                                          content={"text": f"模型调用失败: {str(api_err)[:200]}", "agent_name": agent_name})
                    return AgentOutput(kind="error", error=str(api_err))

            # 诊断日志：每步模型输出摘要
            logger.info(
                "[agent] turn=%s step=%s finish=%s calls=%s content_len=%s",
                turn_id, step, response.finish_reason,
                [tc.get("name") for tc in (response.tool_calls or [])],
                len(response.content or ""),
            )

            # v6.5: token 统计与广播 -- 网关可能不返回 usage（stream 末尾无 usage chunk），
            # 此时用 estimate_messages_tokens 估算 prompt_tokens 兜底，确保：
            # 1) usage.update 总被广播（前端占用显示实时更新）
            # 2) auto_compact 压缩逻辑能基于真实占用触发（避免永不压缩）
            from app.orchestration.token_counter import estimate_messages_tokens as _est_tokens
            _est_prompt = _est_tokens(api_messages) if api_messages else 0
            _api_prompt = 0
            _api_completion = 0
            _api_total = 0
            _usage_source = "none"
            if response.usage:
                _api_prompt = response.usage.prompt_tokens or 0
                _api_completion = response.usage.completion_tokens or 0
                _api_total = response.usage.total_tokens or 0
                if _api_prompt > 0:
                    _usage_source = "api"

            if _usage_source == "none":
                # 网关未返回 usage，用估算值兜底
                _final_prompt = _est_prompt
                _final_completion = 0
                logger.warning(
                    "[agent] turn=%s step=%s 网关未返回 usage，用估算 prompt=%d (est) 兜底",
                    turn_id, step, _est_prompt,
                )
            else:
                _final_prompt = _api_prompt
                _final_completion = _api_completion
                # v6.5: 用 API 真实值更新校准系数（real/est），指数平滑避免抖动。
                # 下一次前置估算会更准，实现自适应精准压缩。
                if _est_prompt > 0 and _api_prompt > 0:
                    _new_factor = _api_prompt / _est_prompt
                    _calib_factor = _calib_factor * 0.5 + _new_factor * 0.5
                    logger.debug(
                        "[agent] turn=%s step=%s 校准系数更新: %.3f (real=%d est=%d new=%.3f)",
                        turn_id, step, _calib_factor, _api_prompt, _est_prompt, _new_factor,
                    )

            total_tokens += (_api_total or (_final_prompt + _final_completion))
            if token_budget and total_tokens > token_budget:
                logger.warning("[agent] turn=%s token 预算熔断", turn_id)
                final_text = response.content or "(预算耗尽，任务中止)"
                break

            # v6.5: total_tokens 发送 prompt_tokens（真实当前上下文占用），
            # prompt_tokens 包含 system+history+tools+当前输入，是"窗口被占用了多少"的真实值。
            # v2.2 (对齐 zcode 3.10): 用量分类 breakdown（前端用量环 hover 展示分类条）
            _breakdown: dict = {}
            try:
                from app.orchestration.token_counter import estimate_breakdown
                _breakdown = estimate_breakdown(api_messages)
            except Exception:
                logger.debug("[agent] breakdown 估算失败(非阻塞)", exc_info=True)

            await broadcast(session_id, {
                "event": "usage.update",
                "payload": {
                    "agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id,
                    "prompt_tokens": _final_prompt,
                    "completion_tokens": _final_completion,
                    "total_tokens": _final_prompt,
                    "context_window": agent_window,
                    "usage_source": _usage_source,
                    "cached_input_tokens": getattr(response.usage, 'cached_input_tokens', 0) or 0 if response.usage else 0,
                    "reasoning_tokens": getattr(response.usage, 'reasoning_tokens', 0) or 0 if response.usage else 0,
                    "breakdown": _breakdown,
                    # v19: 前端圆环仅统计主代理占用，子代理不覆盖
                    "agent_kind": agent.kind,
                },
            })
            logger.info(
                "[agent] turn=%s step=%s 上下文占用 prompt=%d (source=%s) / window=%d = %.1f%%",
                turn_id, step, _final_prompt, _usage_source, agent_window,
                (_final_prompt / agent_window * 100) if agent_window > 0 else 0,
            )

            # v1.1: 用量流水落库（全软件统计的数据源），失败不阻断主流程
            try:
                from app.persistence.models.usage_record import UsageRecord
                _model_name = ""
                if model_id is not None:
                    from app.persistence.models.model_reg import Model
                    _m = await db.get(Model, model_id)
                    _model_name = _m.name if _m else ""
                db.add(UsageRecord(
                    session_id=session_id, turn_id=turn_id, agent_id=agent_id,
                    model_id=model_id, model_name=_model_name,
                    prompt_tokens=_final_prompt, completion_tokens=_final_completion,
                    reasoning_tokens=(getattr(response.usage, "reasoning_tokens", 0) or 0) if response.usage else 0,
                    cached_tokens=(getattr(response.usage, "cached_input_tokens", 0) or 0) if response.usage else 0,
                    usage_source=_usage_source,
                ))
                await db.commit()
                # v1.1: 主代理同步持久化最后一次真实占用（重启/切会话后圆环口径一致）
                if thread_id is None and _final_prompt > 0:
                    _srow = await db.get(_Session, session_id)
                    if _srow is not None:
                        from datetime import datetime
                        _srow.last_prompt_tokens = _final_prompt
                        _srow.last_usage_at = str(datetime.utcnow())
                        await db.commit()
            except Exception:
                logger.debug("usage 流水落库失败(非阻塞)", exc_info=True)

            # v6.4: 调用后真实占用驱动压缩 -- 用 API 精确 prompt_tokens 判断
            # 替代旧的 should_auto_compact 估算检查，避免过早摘要。
            # 阈值 = agent_window * auto_compact_threshold_ratio (默认 0.90)
            if _final_prompt > 0 and agent_window > 0:
                _real_ratio = _final_prompt / agent_window
                _threshold = settings.auto_compact_threshold_ratio
                if _real_ratio >= _threshold:
                    logger.info(
                        "[agent] turn=%s step=%s 真实上下文占用 %.1f%% (p=%d/w=%d) >= %.0f%%，触发自动压缩",
                        turn_id, step, _real_ratio * 100, _final_prompt,
                        agent_window, _threshold * 100,
                    )
                    # v6.5: 压缩前广播，让前端显示"正在压缩上下文"反馈
                    await broadcast(session_id, {
                        "event": "compact.started",
                        "payload": {
                            "agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id,
                            "used_tokens": _final_prompt,
                            "context_window": agent_window,
                            "ratio": round(_real_ratio * 100, 1),
                        },
                    })
                    from app.orchestration.compaction import auto_compact
                    messages = await auto_compact(messages, agent_window, provider)
                    # v6.5: 压缩后广播，前端关闭反馈提示
                    await broadcast(session_id, {
                        "event": "compact.completed",
                        "payload": {
                            "agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id,
                        },
                    })
                else:
                    logger.debug(
                        "[agent] turn=%s step=%s 上下文占用 %.1f%% (p=%d/w=%d) < %.0f%%，无需压缩",
                        turn_id, step, _real_ratio * 100, _final_prompt,
                        agent_window, _threshold * 100,
                    )

            # 思考写入消息
            if response.thinking:
                await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                      agent_id=agent_id, agent_name=agent_name,
                                      msg_type=MsgType.THINKING,
                                      content={"text": response.thinking, "agent_name": agent_name,
                                               "thinking": True})

            # 工具调用
            if response.tool_calls:
                # v1.3: 有工具调用且有文本内容时，先提交文本消息再提交工具调用
                # 否则前端 streamingBuffers 被 tool_call 的 message.created 清空后，
                # 文本内容永久丢失（后端没提交，前端也读不到）
                if response.content:
                    await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                          agent_id=agent_id, agent_name=agent_name,
                                          msg_type=MsgType.TEXT,
                                          content={"text": response.content, "agent_name": agent_name})
                messages.append(ChatMessage(
                    role="assistant", content=response.content,
                    tool_calls=response.tool_calls,
                    # v1.2: thinking 模式网关要求工具调用回合的历史 assistant 回传
                    # reasoning_content（模型本步产生的思考），否则下一轮调用 400
                    reasoning_content=response.thinking or None,
                ))
                for tc in response.tool_calls:
                    # v6.4: 工具执行前检查中断信号
                    if cancel_event and cancel_event.is_set():
                        logger.warning("[agent] turn=%s 工具执行前检测到中断信号，退出循环", turn_id)
                        return AgentOutput(kind="cancelled", error="任务被用户中断")
                    tool_name = tc.get("name", "")
                    args = tc.get("arguments", {}) or {}
                    call_key = "tc_" + uuid.uuid4().hex[:12]

                    # v2.2 (对齐 zcode 3.14): 重复调用签名检测——连续 ≥2 步同工具同参数
                    # 注入提醒（对齐 buildRepeatedToolCallReminderBody），防死循环空转。
                    try:
                        _sig = tool_name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
                        _sig_count = _call_sigs.get(_sig, 0) + 1
                        _call_sigs[_sig] = _sig_count
                        if _sig_count >= 2:
                            _call_sigs = {k: v for k, v in _call_sigs.items() if k == _sig or v < _sig_count}
                            _pending_reminders.append(
                                f"[重复调用提醒] 你已连续 {_sig_count} 次调用 {tool_name} "
                                "且参数完全相同，但结果未推动任务进展。"
                                "请停止重复，改用其他方法：检查文件是否存在、换用不同工具或参数、"
                                "或总结当前发现并询问用户。"
                            )
                            logger.warning(
                                "[agent] turn=%s step=%s 重复调用提醒: %s x%d",
                                turn_id, step, tool_name, _sig_count,
                            )
                    except Exception:
                        pass

                    # ── 子代理工具（主代理专用）──
                    if subagent_context and tool_name in ("spawn_subagent", "collect_results"):
                        tool_output = await _run_subagent_tool(
                            db, tool_name=tool_name, args=args,
                            session_id=session_id, turn_id=turn_id,
                            agent=agent, workspace=workspace,
                            subagent_context=subagent_context,
                        )
                        messages.append(ChatMessage(
                            role="tool", content=tool_output,
                            name=tool_name, tool_call_id=tc.get("id", ""),
                        ))
                        continue

                    await broadcast(session_id, {
                        "event": "tool.call",
                        "payload": {"turn_id": turn_id, "agent_id": agent_id,
                                    "tool": tool_name, "args_preview": _truncate_args(args, 200)},
                    })
                    await message_service.create_message(
                        db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                        sender_type=SenderType.AGENT.value, sender_id=agent_id,
                        msg_type=MsgType.TOOL_CALL.value,
                        content={"tool": tool_name, "args": args, "call_key": call_key,
                                 "agent_name": agent_name},
                    )

                    ctx = ToolContext(
                        workspace_root=workspace, session_id=session_id,
                        task_id=turn_id, agent_id=agent_id, agent_name=agent_name,
                        cancel_event=cancel_event,
                        db=db,
                        permission_mode=permission_mode,
                    )
                    _ts0 = time.monotonic()
                    # v9: 写盘工具执行前读取原文件内容（精确回滚依据：只撤销 AI 改动部分）
                    _pre_paths = rollback_service.resolve_write_paths(tool_name, args) if tool_name in _WRITE_TOOLS else []
                    _pre_before = {p: rollback_service._read_file_text(workspace, p) for p in _pre_paths}
                    # v10: 对已存在的目标文件额外建立磁盘 checkpoint（.chatcoder/checkpoints 兜底备份），
                    # 与精确回滚的 before/after 记录双保险，防数据库记录异常时无法恢复。
                    # v1.1: 取消穿透——长工具执行期间轮询 cancel_event，命中即取消底层任务
                    _exec_task = asyncio.ensure_future(tool_executor.execute(
                        tool_name=tool_name, args=args, call_key=call_key,
                        agent=agent, ctx=ctx,
                        on_approval_request=_make_approval_emitter(session_id),
                    ))
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(_poll_cancel(_exec_task, cancel_event)),
                            timeout=120.0,
                        )
                    except asyncio.TimeoutError:
                        _exec_task.cancel()
                        from app.orchestration.tools.base import ToolResult
                        result = ToolResult(ok=False, output="", error=f"[工具执行超时(120s)] {tool_name}")
                    except asyncio.CancelledError:
                        from app.orchestration.tools.base import ToolResult
                        result = ToolResult(ok=False, output="", error="[已被用户中断]")
                    except Exception as exc:
                        from app.orchestration.tools.base import ToolResult
                        result = ToolResult(ok=False, output="", error=f"[工具执行异常] {exc}")
                    _dur = int((time.monotonic() - _ts0) * 1000)

                    # 写盘工具：登记路径 + 记录 before/after（v9 精确回滚依据）
                    _change_stat = None
                    if tool_name in _WRITE_TOOLS and result.ok:
                        _total_add = 0
                        _total_del = 0
                        for target in _pre_paths:
                            write_paths.append(str(target))
                            # v10: 文件级 checkpoint 兜底（写盘前快照备份）
                            if _pre_before.get(target) is not None:
                                rollback_service.checkpoint_file(workspace, target)
                            await rollback_service.record_turn_write(
                                db, session_id=session_id, turn_id=turn_id, tool=tool_name,
                                path=target,
                                before=_pre_before.get(target),
                                after=rollback_service._read_file_text(workspace, target),
                            )
                            # v2.2 (对齐 zcode 3.7): 行级变更统计（+N -M），工具卡摘要展示
                            _add, _del = _line_change_stat(
                                _pre_before.get(target),
                                rollback_service._read_file_text(workspace, target),
                            )
                            _total_add += _add
                            _total_del += _del
                        if _total_add or _total_del:
                            _change_stat = {
                                "path": str(_pre_paths[0]),
                                "additions": _total_add,
                                "deletions": _total_del,
                            }

                    await broadcast(session_id, {
                        "event": "tool.result",
                        "payload": {"turn_id": turn_id, "tool": tool_name,
                                    "ok": result.ok, "duration_ms": _dur,
                                    "output_preview": (result.output or result.error)[:300],
                                    **({"change_stat": _change_stat} if _change_stat else {})},
                    })
                    await message_service.create_message(
                        db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                        sender_type=SenderType.AGENT.value, sender_id=agent_id,
                        msg_type=MsgType.TOOL_RESULT.value,
                        content={"tool": tool_name, "call_key": call_key,
                                 "ok": result.ok, "output": result.output[:MAX_TOOL_OUTPUT_CHARS],
                                 "error": result.error, "duration_ms": _dur,
                                 "agent_name": agent_name,
                                 **({"change_stat": _change_stat} if _change_stat else {})},
                    )
                    messages.append(ChatMessage(
                        role="tool", content=_truncate_output(result.output or result.error),
                        name=tool_name, tool_call_id=tc.get("id", ""),
                    ))
                    # v2.2: todo_write 成功 → 重置 todo 提醒计数
                    if tool_name == "todo_write" and result.ok:
                        _todo_active = True
                        _todo_updated_at_step = step
                    # v15: 图片类工具结果 → 多模态模型追加 image_url 消息，让模型真正看到图片。
                    # （此前 base64 只放在 ToolResult.data，从未进入对话，模型只能看到
                    #   "Base64 length: N" 文本，被迫转向 OCR/命令行猜图）
                    if multimodal and result.ok and tool_name in ("read_attachment", "view_image"):
                        _b64 = (result.data or {}).get("base64")
                        if _b64:
                            import mimetypes as _mt
                            _fname = str((result.data or {}).get("filename") or (result.data or {}).get("path") or "")
                            _mime = _mt.guess_type(_fname)[0] or "image/png"
                            messages.append(ChatMessage(
                                role="user",
                                content=f"[系统] 上面工具 {tool_name} 读取的图片内容如下，请直接查看：",
                                content_blocks=[{"type": "image_url", "image_url": {"url": f"data:{_mime};base64,{_b64}"}}],
                            ))
                continue

            # 最终文本
            final_text = response.content or ""
            if final_text:
                await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                      agent_id=agent_id, agent_name=agent_name,
                                      msg_type=MsgType.TEXT,
                                      content={"text": final_text, "agent_name": agent_name})
            break

        else:
            final_text = response.content if "response" in locals() else ""
            await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                  agent_id=agent_id, agent_name=agent_name,
                                  msg_type=MsgType.ERROR,
                                  content={"text": f"达到步数上限({max_steps})，任务未完成", "agent_name": agent_name})
            return AgentOutput(kind="error", error=f"达到步数上限({max_steps})")

        # 产物抽取（v7: 关联到主 turn 对应的任务；task_id 为空时兜底用 turn_id）
        artifact_ids: list[int] = []
        if final_text and len(final_text.strip()) > 10:
            try:
                from app.orchestration.artifacts import extract_and_persist_artifacts
                artifact_ids = await extract_and_persist_artifacts(
                    db, task_id=task_id or turn_id, text=final_text, write_paths=write_paths,
                )
            except Exception:
                logger.warning("[agent] 产物抽取失败(非阻塞)", exc_info=True)

        # 主代理最终文字与产物也写入主线程（子代理已写 thread）
        if agent.kind == "main" and artifact_ids:
            await message_service.create_message(
                db, session_id=session_id, turn_id=turn_id, thread_id=None,
                sender_type=SenderType.AGENT.value, sender_id=agent_id,
                msg_type=MsgType.ARTIFACT.value,
                content={
                    "artifact_ids": artifact_ids,
                    "agent_name": agent_name,
                    # v7: 携带实际写盘文件列表，前端产物卡片据此展示文件清单
                    "files": list(dict.fromkeys(write_paths)),
                },
            )

        await broadcast(session_id, {
            "event": "agent.completed",
            "payload": {"agent_id": agent_id, "summary": final_text, "artifact_ids": artifact_ids},
        })
        return AgentOutput(kind="message", text=final_text, artifact_ids=artifact_ids)

    except Exception as e:
        logger.exception("agent loop 异常 turn=%s agent=%s", turn_id, agent_name)
        await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                              agent_id=agent_id, agent_name=agent_name,
                              msg_type=MsgType.ERROR,
                              content={"text": f"执行异常: {str(e)[:200]}", "agent_name": agent_name})
        return AgentOutput(kind="error", error=str(e))


# ── 辅助 ──

async def _poll_cancel(task: asyncio.Task, cancel_event: asyncio.Event | None):
    """等待工具任务完成；期间轮询取消信号，命中则取消底层任务（停止按钮对长工具即时生效）。"""
    while not task.done():
        if cancel_event is not None and cancel_event.is_set():
            task.cancel()
            raise asyncio.CancelledError("cancelled by user")
        await asyncio.sleep(0.2)
    return await task


async def _emit_agent_msg(db, *, session_id, turn_id, thread_id, agent_id, agent_name,
                          msg_type, content) -> None:
    await message_service.create_message(
        db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
        sender_type=SenderType.AGENT.value, sender_id=agent_id,
        msg_type=msg_type.value, content=content,
    )


async def _stream_chat_and_broadcast(provider, request, *, session_id, turn_id, agent_id, agent_name,
                                     cancel_event: asyncio.Event | None = None,
                                     thread_id: int | None = None):
    """流式调用 provider，实时广播 thinking/content delta。

    v6.4: 支持 cancel_event —— 流式输出中检测到中断信号时立即终止，返回已收到的部分内容。
    v19: 事件 payload 携带 thread_id（子代理线程），前端据此将子代理流式内容分桶到右面板，
    不再混入主消息流；流式结束后统一剥离正文中的内联 <thinking> 标签。
    """
    from app.models.schemas import ChatResponse, Usage as UsageModel
    full_content = ""
    full_thinking = ""
    tool_calls = []
    finish_reason = "stop"
    usage = UsageModel()
    _cancelled = False
    try:
        async for event in provider.stream_structured(request):
            # v6.4: 流式输出中检查中断信号，立即停止
            if cancel_event and cancel_event.is_set():
                logger.warning("[agent] turn=%s 流式输出中收到中断信号，停止接收", turn_id)
                _cancelled = True
                finish_reason = "cancelled"
                break
            if event["type"] == "thinking":
                delta = event.get("delta", "")
                full_thinking += delta
                await broadcast(session_id, {
                    "event": "thinking.delta",
                    "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": delta,
                                "thread_id": thread_id},
                })
            elif event["type"] == "content":
                delta = event.get("delta", "")
                full_content += delta
                await broadcast(session_id, {
                    "event": "token.delta",
                    "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": delta,
                                "thread_id": thread_id},
                })
            elif event["type"] == "done":
                full_content = event.get("content") or full_content
                full_thinking = event.get("thinking") or full_thinking
                tool_calls = event.get("tool_calls", [])
                finish_reason = event.get("finish_reason", "stop")
                usage = event.get("usage", UsageModel())
                break
    except Exception:
        if _cancelled:
            pass  # 中断导致的异常，忽略
        else:
            logger.exception("[agent] turn=%s 流式调用异常，回退非流式", turn_id)
            response = await provider.chat(request)
            full_content = response.content or ""
            full_thinking = response.thinking or ""
            tool_calls = response.tool_calls or []
            finish_reason = response.finish_reason
            usage = response.usage

    # v19: 剥离正文中的内联思考标签（部分网关把思考写进 content）
    if full_content:
        _clean, _inline_think = _split_inline_thinking(full_content)
        if _inline_think:
            full_thinking = (full_thinking + "\n" + _inline_think).strip() if full_thinking else _inline_think
            full_content = _clean

    if full_thinking:
        await broadcast(session_id, {
            "event": "thinking.done",
            "payload": {"agent_id": agent_id, "turn_id": turn_id, "full_text": full_thinking,
                        "thread_id": thread_id},
        })
    if full_content:
        await broadcast(session_id, {
            "event": "token.done",
            "payload": {"agent_id": agent_id, "turn_id": turn_id, "full_text": full_content,
                        "thread_id": thread_id},
        })
    return ChatResponse(
        content=full_content or None, thinking=full_thinking or None,
        tool_calls=tool_calls, finish_reason=finish_reason, usage=usage,
    )


def _make_approval_emitter(session_id: int):
    """审批请求回调：仅广播 WS 事件。"""

    async def _emit(approval_id: str, detail: dict) -> None:
        await ws_manager.broadcast(
            session_id,
            {"event": "approval.request", "payload": {"approval_id": approval_id, "detail": detail}},
        )

    return _emit


# ── 子代理工具处理 ──

async def _run_subagent_tool(db, *, tool_name, args, session_id, turn_id, agent, workspace,
                             subagent_context) -> str:
    """spawn_subagent / collect_results 工具实现。

    返回给模型的文本结果。
    """
    import json as _json

    if tool_name == "collect_results":
        manager = subagent_context.get("manager")
        if manager is None:
            return "No subagents were spawned in this turn."
        results = manager.results()
        if not results:
            pending = manager.pending_count()
            return f"No subagents have finished yet ({pending} still running)."
        lines = []
        for r in results:
            status = "completed" if r["status"] == "done" else "failed"
            lines.append(f"- subagent#{r['agent_id']} [{status}] {(r['summary'] or r['error'] or '')[:500]}")
        if manager.pending_count():
            lines.append(f"- ({manager.pending_count()} subagents still running)")
        return "Subagent results:\n" + "\n".join(lines)

    if tool_name == "spawn_subagent":
        manager = subagent_context.get("manager")
        project = subagent_context.get("project")
        session = subagent_context.get("session")
        cancel_event = subagent_context.get("cancel_event")
        main_task_id = subagent_context.get("main_task_id")
        # v12: 兜底解析主任务（步骤载体）——context 缺失时按 turn 查最早的任务记录，
        # 保证子代理任务 parent_task_id 始终指向真实的主任务（而非退化用 turn_id）。
        if not main_task_id:
            from app.persistence.models.task import Task
            mt_res = await db.execute(
                select(Task).where(
                    Task.session_id == session_id,
                    Task.turn_id == turn_id,
                ).order_by(Task.id.asc()).limit(1)
            )
            mt = mt_res.scalars().first()
            main_task_id = mt.id if mt else None
        if manager is None or project is None:
            return "Error: subagent manager unavailable"

        task_title = str(args.get("task_title", ""))[:200]
        task_desc = str(args.get("task_description", ""))[:4000]
        acceptance = str(args.get("acceptance_criteria", ""))[:500]
        # v20: explore=true → 只读探索子代理，结果直接返回给主代理（见下方同步等待）。
        explore = bool(args.get("explore", False))
        if not task_title:
            return "Error: task_title is required"

        # v10/v20: 子代理数量硬性限制——超过配置上限时拒绝新子代理。
        # 按"运行中"数量计（探索子代理完成后不占名额，主代理仍可继续 spawn 实现子任务）。
        from app.core.config import settings
        if manager.pending_count() >= settings.max_subagents_per_turn:
            return (
                f"Error: subagent limit reached (max {settings.max_subagents_per_turn} running). "
                "Do not spawn more subagents. Collect the results of already spawned ones "
                "with collect_results, or perform the remaining work yourself directly."
            )

        # 创建子代理 Agent + Task
        from app.core.enums import AgentKind
        from app.persistence.models.agent import Agent
        sub_agent = Agent(kind=AgentKind.SUB.value, name=task_title[:40],
                          model_id=subagent_context.get("model_id") or agent.model_id,
                          session_id=session_id,
                          turn_id=turn_id, parent_agent_id=agent.id)
        db.add(sub_agent)
        await db.flush()

        task = await task_service.create_task(
            db, session_id=session_id, turn_id=turn_id,
            title=task_title, description=task_desc,
            acceptance_criteria=acceptance, agent_id=sub_agent.id,
            parent_task_id=main_task_id or turn_id, priority=1,
        )
        await db.flush()

        # v10: 子任务创建后立即提交并广播，前端任务卡实时展示拆分步骤
        # （此前仅 flush，前端 refreshTasks 查不到新子任务，任务卡无新步骤）
        from app.orchestration.agent_events import broadcast
        try:
            await db.commit()
            await broadcast(session_id, {
                "event": "task.updated",
                "payload": {"task_id": task.id, "status": "pending", "note": (task_desc or "")[:200]},
            })
        except Exception:
            logger.warning("[agent] 子任务创建提交/广播失败(非阻塞)", exc_info=True)

        # 构建子代理上下文（v19: 注入用户原始请求 + 主会话摘要，修复继承断裂）
        from app.orchestration.context_manager import build_subagent_context
        _orig_req = ""
        try:
            from app.persistence.models.message import Message
            _ures = await db.execute(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.sender_type == "user",
                ).order_by(Message.id.desc()).limit(1)
            )
            _um = _ures.scalars().first()
            if _um and isinstance(_um.content, dict):
                _orig_req = str(_um.content.get("text") or "")
        except Exception:
            _orig_req = ""
        bundle = await build_subagent_context(
            db, agent=sub_agent, session=session, project=project, task=task,
            handoff_summary=task_desc,
            original_request=_orig_req,
        )
        # 子代理工具范围：全量工具（不含子代理递归工具）
        from app.orchestration.tools.registry import tool_registry
        sub_tools = tool_registry.all_schemas()

        # v20: 探索子代理只读工具（探索 = 调研/阅读/分析，不产生写盘副作用）
        if explore:
            from app.orchestration.subagent_tools import EXPLORE_TOOLS
            explore_schemas = tool_registry.all_schemas(EXPLORE_TOOLS)
            if explore_schemas:
                sub_tools = explore_schemas

        from app.orchestration.subagent import get_subagent_manager
        mgr = manager or get_subagent_manager(session_id)
        # v20: explore 子代理同步等待——主代理拿结论后继续串行整合，
        # 避免主代理把"等待"变成反复轮询 collect_results 的空转。
        if explore:
            await broadcast(session.id, {
                "event": "agent.started",
                "payload": {"agent_id": sub_agent.id, "kind": "sub",
                            "name": sub_agent.name, "turn_id": turn_id},
            })
            handle = await mgr.spawn_and_wait(
                db, agent=sub_agent, turn_id=turn_id, task=task,
                handoff_summary=task_desc, context_bundle=bundle,
                tool_schemas=sub_tools, workspace=workspace,
                cancel_event=cancel_event,
            )
            if handle is not None and handle.status == "done":
                return (
                    f"Exploration subagent #{sub_agent.id} finished for: {task_title}.\n"
                    f"Findings:\n{(handle.findings or handle.summary or '')[:4000]}"
                )
            return (
                f"Exploration subagent #{sub_agent.id} failed for: {task_title}.\n"
                f"Error: {(handle.error if handle else 'unknown')[:1000]}"
            )
        sub_agent_id = mgr.spawn(
            db, agent=sub_agent, turn_id=turn_id, task=task,
            handoff_summary=task_desc, context_bundle=bundle,
            tool_schemas=sub_tools, workspace=workspace,
            cancel_event=cancel_event,
        )
        await broadcast(session.id, {
            "event": "agent.started",
            "payload": {"agent_id": sub_agent_id, "kind": "sub",
                        "name": sub_agent.name, "turn_id": turn_id},
        })
        return f"Subagent #{sub_agent_id} spawned for task: {task_title}. It runs in an isolated context. Use collect_results to gather its output."

    return "Error: unknown subagent tool"

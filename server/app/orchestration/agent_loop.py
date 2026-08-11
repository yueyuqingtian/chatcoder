"""Agent 推理循环（v2：主/子代理统一，当前实际生效实现）。

职责：流式模型调用 + 思考/文本广播 + 工具执行 + token 预算 + 产物抽取 + 回滚写盘埋点。
由 engine.start_turn（主代理）与 subagent.SubagentManager（子代理）统一调用。
旧版 agent_runtime.py（v0.1 团队编排路径）已废弃删除。
"""
import asyncio
import json
import logging
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

MAX_TOOL_OUTPUT_CHARS = 8000  # v1.1: 已弃用，保留仅向后兼容；实际用 _tool_output_limit
_STREAM_CHUNK_SIZE = 80
_STREAM_INTERVAL = 0.01

# v9: 写盘工具集合——执行时记录前后内容（精确回滚依据）
_WRITE_TOOLS = ("fs_write", "editor_apply_diff", "multi_file_edit")


def _truncate_output(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if not text or len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n\n... [已截断,原始 {len(text)} 字符] ...\n\n" + text[-half:]


def _truncate_args(args: dict, limit: int = 800) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "\n...(已截断)"


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
) -> AgentOutput:
    """运行单个 agent 推理循环。

    context_messages 已由 context_manager 组装（system + developer + 历史 + 指令）。
    subagent_context: 主代理专用，含子代理管理能力（spawn_subagent/collect_results 工具）。
    """
    agent_id = agent.id
    agent_name = agent.name
    thread_id = None if agent.kind == "main" else agent.id  # 子代理消息进自己的线程

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
        pass

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
    # v6.5: 估算校准系数 -- 用 API 真实 prompt_tokens 动态校准估算值。
    # 字节/4 对中文偏高，不同模型/网关分词差异大，静态常量无法精准。
    # 每次拿到 API 真实值就更新系数，用于前置压缩估算，实现自适应精准压缩。
    _calib_factor = 1.0  # real / est，初始1.0（无校准）

    try:
        for step in range(1, max_steps + 1):
            if cancel_event and cancel_event.is_set():
                logger.warning("[agent] task turn=%s 收到中断信号", turn_id)
                return AgentOutput(kind="error", error="任务被用户中断")

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
                api_messages = build_api_copy(messages)

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
                    api_messages = build_api_copy(messages)
                    _est_after = int(_est_tokens(api_messages) * _calib_factor)
                    logger.info("[agent] turn=%s step=%s 前置压缩后 prompt=%d -> %d", turn_id, step, _est_prompt, _est_after)
                    await broadcast(session_id, {"event": "usage.update", "payload": {"agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id, "prompt_tokens": _est_after, "completion_tokens": 0, "total_tokens": _est_after, "context_window": agent_window, "usage_source": "est_after_compact", "cached_input_tokens": 0, "reasoning_tokens": 0}})
                    await broadcast(session_id, {"event": "compact.completed", "payload": {"agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id}})
                else:
                    logger.debug("[agent] turn=%s step=%s 前置估算 prompt=%d (raw=%d calib=%.3f) < 阈值 %d，不压缩", turn_id, step, _est_prompt, _est_raw, _calib_factor, _pre_threshold)
            except Exception:
                api_messages = messages

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
                )
                # v6.4: 流式调用被用户中断 → 立即退出循环
                if cancel_event and cancel_event.is_set():
                    logger.warning("[agent] turn=%s 流式调用后检测到中断信号，退出循环", turn_id)
                    final_text = response.content or ""
                    if final_text:
                        await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                              agent_id=agent_id, agent_name=agent_name,
                                              msg_type=MsgType.TEXT,
                                              content={"text": final_text, "agent_name": agent_name})
                    return AgentOutput(kind="error", error="任务被用户中断")
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
                },
            })
            logger.info(
                "[agent] turn=%s step=%s 上下文占用 prompt=%d (source=%s) / window=%d = %.1f%%",
                turn_id, step, _final_prompt, _usage_source, agent_window,
                (_final_prompt / agent_window * 100) if agent_window > 0 else 0,
            )

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
                ))
                for tc in response.tool_calls:
                    # v6.4: 工具执行前检查中断信号
                    if cancel_event and cancel_event.is_set():
                        logger.warning("[agent] turn=%s 工具执行前检测到中断信号，退出循环", turn_id)
                        return AgentOutput(kind="error", error="任务被用户中断")
                    tool_name = tc.get("name", "")
                    args = tc.get("arguments", {}) or {}
                    call_key = "tc_" + uuid.uuid4().hex[:12]

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
                    )
                    _ts0 = time.monotonic()
                    # v9: 写盘工具执行前读取原文件内容（精确回滚依据：只撤销 AI 改动部分）
                    _pre_paths = rollback_service.resolve_write_paths(tool_name, args) if tool_name in _WRITE_TOOLS else []
                    _pre_before = {p: rollback_service._read_file_text(workspace, p) for p in _pre_paths}
                    # v10: 对已存在的目标文件额外建立磁盘 checkpoint（.chatcoder/checkpoints 兜底备份），
                    # 与精确回滚的 before/after 记录双保险，防数据库记录异常时无法恢复。
                    try:
                        result = await asyncio.wait_for(
                            tool_executor.execute(
                                tool_name=tool_name, args=args, call_key=call_key,
                                agent=agent, ctx=ctx,
                                on_approval_request=_make_approval_emitter(session_id),
                            ),
                            timeout=120.0,
                        )
                    except asyncio.TimeoutError:
                        from app.orchestration.tools.base import ToolResult
                        result = ToolResult(ok=False, output="", error=f"[工具执行超时(120s)] {tool_name}")
                    except Exception as exc:
                        from app.orchestration.tools.base import ToolResult
                        result = ToolResult(ok=False, output="", error=f"[工具执行异常] {exc}")
                    _dur = int((time.monotonic() - _ts0) * 1000)

                    # 写盘工具：登记路径 + 记录 before/after（v9 精确回滚依据）
                    if tool_name in _WRITE_TOOLS and result.ok:
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

                    await broadcast(session_id, {
                        "event": "tool.result",
                        "payload": {"turn_id": turn_id, "tool": tool_name,
                                    "ok": result.ok, "duration_ms": _dur,
                                    "output_preview": (result.output or result.error)[:300]},
                    })
                    await message_service.create_message(
                        db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                        sender_type=SenderType.AGENT.value, sender_id=agent_id,
                        msg_type=MsgType.TOOL_RESULT.value,
                        content={"tool": tool_name, "call_key": call_key,
                                 "ok": result.ok, "output": result.output[:4000],
                                 "error": result.error, "duration_ms": _dur,
                                 "agent_name": agent_name},
                    )
                    messages.append(ChatMessage(
                        role="tool", content=_truncate_output(result.output or result.error),
                        name=tool_name, tool_call_id=tc.get("id", ""),
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
                logger.debug("[agent] 产物抽取失败(非阻塞)", exc_info=True)

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

async def _emit_agent_msg(db, *, session_id, turn_id, thread_id, agent_id, agent_name,
                          msg_type, content) -> None:
    await message_service.create_message(
        db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
        sender_type=SenderType.AGENT.value, sender_id=agent_id,
        msg_type=msg_type.value, content=content,
    )


async def _stream_chat_and_broadcast(provider, request, *, session_id, turn_id, agent_id, agent_name,
                                     cancel_event: asyncio.Event | None = None):
    """流式调用 provider，实时广播 thinking/content delta。

    v6.4: 支持 cancel_event —— 流式输出中检测到中断信号时立即终止，返回已收到的部分内容。
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
                    "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": delta},
                })
            elif event["type"] == "content":
                delta = event.get("delta", "")
                full_content += delta
                await broadcast(session_id, {
                    "event": "token.delta",
                    "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": delta},
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

    if full_thinking:
        await broadcast(session_id, {
            "event": "thinking.done",
            "payload": {"agent_id": agent_id, "turn_id": turn_id, "full_text": full_thinking},
        })
    if full_content:
        await broadcast(session_id, {
            "event": "token.done",
            "payload": {"agent_id": agent_id, "turn_id": turn_id, "full_text": full_content},
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
            return "No subagents have finished yet."
        lines = []
        for r in results:
            status = "completed" if r["status"] == "done" else "failed"
            lines.append(f"- subagent#{r['agent_id']} [{status}] {(r['summary'] or r['error'] or '')[:500]}")
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
        if not task_title:
            return "Error: task_title is required"

        # v10: 子代理数量硬性限制——超过配置上限时拒绝新子代理，
        # 提示主代理合并子任务或自行串行处理，防止无限拆分资源失控。
        from app.core.config import settings
        if len(manager._handles) >= settings.max_subagents_per_turn:
            return (
                f"Error: subagent limit reached (max {settings.max_subagents_per_turn} per turn). "
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
            logger.debug("[agent] 子任务创建提交/广播失败(非阻塞)", exc_info=True)

        # 构建子代理上下文（独立，仅 handoff + 项目规则）
        from app.orchestration.context_manager import build_subagent_context
        bundle = await build_subagent_context(
            db, agent=sub_agent, session=session, project=project, task=task,
            handoff_summary=task_desc,
        )
        # 子代理工具范围：全量工具（不含子代理递归工具）
        from app.orchestration.tools.registry import tool_registry
        sub_tools = tool_registry.all_schemas()

        from app.orchestration.subagent import get_subagent_manager
        mgr = manager or get_subagent_manager(session_id)
        sub_agent_id = mgr.spawn(
            db, agent=sub_agent, turn_id=turn_id, task=task,
            handoff_summary=task_desc, context_bundle=bundle,
            tool_schemas=sub_tools, workspace=workspace,
            cancel_event=cancel_event,
        )
        await broadcast(session_id, {
            "event": "agent.started",
            "payload": {"agent_id": sub_agent_id, "kind": "sub",
                        "name": sub_agent.name, "turn_id": turn_id},
        })
        return f"Subagent #{sub_agent_id} spawned for task: {task_title}. It runs in an isolated context. Use collect_results to gather its output."

    return "Error: unknown subagent tool"

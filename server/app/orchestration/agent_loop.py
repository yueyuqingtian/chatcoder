# ═══════════════════════════════════════════════════════════
# DEPRECATED: 本文件为未完成的重构尝试，实际使用 agent_runtime.py
# 请勿在此文件修改 agent 逻辑，所有变更请到 agent_runtime.py
# 计划后续迭代收敛为一套实现
# ═══════════════════════════════════════════════════════════
"""Agent 推理循环（v2：主/子代理统一，从 v0.1 agent_runtime 重构提取）。

[已废弃] 实际使用 agent_runtime.py，本文件保留仅作历史参考。
职责：流式模型调用 + 思考/文本广播 + 工具执行 + token 预算 + 产物抽取。
移除团队/Leader 概念，改为 agent + context bundle 驱动。
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
from app.orchestration.agent_events import broadcast
from app.orchestration.tools import ToolContext, tool_executor
from app.orchestration.tools.registry import tool_registry
from app.services import message_service, task_service

logger = logging.getLogger(__name__)

MAX_TOOL_OUTPUT_CHARS = 8000  # v1.1: 已弃用，保留仅向后兼容；实际用 _tool_output_limit
_STREAM_CHUNK_SIZE = 80
_STREAM_INTERVAL = 0.01


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
) -> AgentOutput:
    """运行单个 agent 推理循环。

    context_messages 已由 context_manager 组装（system + developer + 历史 + 指令）。
    subagent_context: 主代理专用，含子代理管理能力（spawn_subagent/collect_results 工具）。
    """
    agent_id = agent.id
    agent_name = agent.name
    thread_id = None if agent.kind == "main" else agent.id  # 子代理消息进自己的线程

    # 解析 provider
    registry = get_model_registry()
    provider, reason = await registry.get_provider_for_agent(db, agent)
    if provider is None:
        await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                              agent_id=agent_id, agent_name=agent_name,
                              msg_type=MsgType.ERROR,
                              content={"text": f"模型不可用({reason})", "agent_name": agent_name})
        return AgentOutput(kind="skipped", error=reason)

    if max_steps is None:
        max_steps = settings.agent_max_steps

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

    try:
        for step in range(1, max_steps + 1):
            if cancel_event and cancel_event.is_set():
                logger.warning("[agent] task turn=%s 收到中断信号", turn_id)
                return AgentOutput(kind="error", error="任务被用户中断")

            await asyncio.sleep(0)

            # 上下文压缩（复用 v0.1 compaction）
            agent_window = 0
            try:
                from app.orchestration.compaction import build_api_copy, ensure_tool_pairing
                from app.orchestration.token_counter import should_auto_compact, get_agent_context_window
                agent_window = await get_agent_context_window(db, agent)
                messages = ensure_tool_pairing(messages)
                api_messages = build_api_copy(messages)
                if should_auto_compact(messages, agent_window):
                    from app.orchestration.compaction import auto_compact
                    messages = await auto_compact(messages, agent_window, provider)
                    api_messages = build_api_copy(messages)
            except Exception:
                api_messages = messages

            request = ChatRequest(
                messages=api_messages, model="",
                tools=tool_schemas or None,
                temperature=settings.agent_tool_temperature if tool_schemas else settings.agent_text_temperature,
                reasoning_effort=reasoning_effort or (settings.agent_reasoning_effort if tool_schemas else None),
            )

            try:
                response = await _stream_chat_and_broadcast(
                    provider, request,
                    session_id=session_id, turn_id=turn_id,
                    agent_id=agent_id, agent_name=agent_name,
                )
            except Exception as api_err:
                logger.warning("[agent] turn=%s 模型调用失败，尝试紧急压缩: %s", turn_id, str(api_err)[:200])
                try:
                    from app.orchestration.compaction import emergency_compact
                    from app.orchestration.token_counter import get_agent_context_window
                    agent_window = await get_agent_context_window(db, agent)
                    messages = emergency_compact(messages, agent_window)
                    request = ChatRequest(
                        messages=messages, model="", tools=tool_schemas or None,
                        temperature=settings.agent_tool_temperature if tool_schemas else settings.agent_text_temperature,
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

            # token 统计与广播
            if response.usage and response.usage.total_tokens > 0:
                total_tokens += response.usage.total_tokens
                if token_budget and total_tokens > token_budget:
                    logger.warning("[agent] turn=%s token 预算熔断", turn_id)
                    final_text = response.content or "(预算耗尽，任务中止)"
                    break
                await broadcast(session_id, {
                    "event": "usage.update",
                    "payload": {
                        "agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id,
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                        "context_window": agent_window,
                        # v1.2: 精确 token 统计
                        "cached_input_tokens": getattr(response.usage, 'cached_input_tokens', 0) or 0,
                        "reasoning_tokens": getattr(response.usage, 'reasoning_tokens', 0) or 0,
                    },
                })

            # 思考写入消息
            if response.thinking:
                await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                      agent_id=agent_id, agent_name=agent_name,
                                      msg_type=MsgType.THINKING,
                                      content={"text": response.thinking, "agent_name": agent_name,
                                               "thinking": True})

            # 工具调用
            if response.tool_calls:
                messages.append(ChatMessage(
                    role="assistant", content=response.content,
                    tool_calls=response.tool_calls,
                ))
                for tc in response.tool_calls:
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

                    # 写盘工具 checkpoint（回滚 §4.10）
                    if tool_name in ("fs_write", "editor_apply_diff") and result.ok:
                        target = args.get("path")
                        if target:
                            write_paths.append(str(target))

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

        # 产物抽取
        artifact_ids: list[int] = []
        if final_text and len(final_text.strip()) > 10:
            try:
                from app.orchestration.artifacts import extract_and_persist_artifacts
                artifact_ids = await extract_and_persist_artifacts(
                    db, task_id=turn_id, text=final_text, write_paths=write_paths,
                )
            except Exception:
                logger.debug("[agent] 产物抽取失败(非阻塞)", exc_info=True)

        # 主代理最终文字与产物也写入主线程（子代理已写 thread）
        if agent.kind == "main" and artifact_ids:
            await message_service.create_message(
                db, session_id=session_id, turn_id=turn_id, thread_id=None,
                sender_type=SenderType.AGENT.value, sender_id=agent_id,
                msg_type=MsgType.ARTIFACT.value,
                content={"artifact_ids": artifact_ids, "agent_name": agent_name},
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


async def _stream_chat_and_broadcast(provider, request, *, session_id, turn_id, agent_id, agent_name):
    """流式调用 provider，实时广播 thinking/content delta。"""
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
        if manager is None or project is None:
            return "Error: subagent manager unavailable"

        task_title = str(args.get("task_title", ""))[:200]
        task_desc = str(args.get("task_description", ""))[:4000]
        acceptance = str(args.get("acceptance_criteria", ""))[:500]
        if not task_title:
            return "Error: task_title is required"

        # 创建子代理 Agent + Task
        from app.core.enums import AgentKind
        from app.persistence.models.agent import Agent
        sub_agent = Agent(kind=AgentKind.SUB.value, name=task_title[:40],
                          model_id=agent.model_id, session_id=session_id,
                          turn_id=turn_id, parent_agent_id=agent.id)
        db.add(sub_agent)
        await db.flush()

        task = await task_service.create_task(
            db, session_id=session_id, turn_id=turn_id,
            title=task_title, description=task_desc,
            acceptance_criteria=acceptance, agent_id=sub_agent.id,
            parent_task_id=turn_id, priority=1,
        )
        await db.flush()

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

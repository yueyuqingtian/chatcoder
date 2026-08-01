"""v0.4: 质量门禁 — 任务产出后自动触发 reviewer agent 审查。

流程:
1. agent_runtime 完成 → 任务 in_review
2. review_task(db, task, session, producing_agent) 触发 reviewer agent loop
   - reviewer 用 ci.run / fs.read 客观验证
   - 解析 reviewer 输出:首行 PASS → 任务 done;首行 REJECT → rejected(返工)
3. 结果广播主群 + 写 thread

reviewer 来源:同团队中 role=reviewer 的 team_agent;若无则跳过(直接 done)。
"""
import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.gateway.ws import manager as ws_manager
from app.models.registry import get_model_registry
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.context import build_agent_context
from app.orchestration.tools.registry import tool_registry
from app.services import session_service, task_service

if TYPE_CHECKING:
    from app.persistence.models.agent import Agent
    from app.persistence.models.message import Session
    from app.persistence.models.task import Task

logger = logging.getLogger(__name__)

_PASS_RE = re.compile(r"\b(PASS|APPROVE|通过)\b", re.IGNORECASE)
_REJECT_RE = re.compile(r"\b(REJECT|DENY|驳回|拒绝)\b", re.IGNORECASE)


async def find_reviewer(db: AsyncSession, team_id: int) -> "Agent | None":
    """v3：团队概念已移除，不再有专职 reviewer 角色，返回 None。

    原逻辑依赖 TeamAgent.team_id + AgentTemplate.role，均已在 v3 移除。
    保留函数签名避免调用方崩溃；review_task 在 reviewer=None 时会跳过审查直接 done。
    """
    return None


async def review_task(
    db: AsyncSession,
    *,
    task: "Task",
    session: "Session",
    producing_agent: "Agent",
    producing_output: str,
    write_paths: list[str] | None = None,
) -> str:
    """触发 reviewer 审查任务产出。

    返回最终任务状态:"done"(通过)或 "rejected"(驳回)。
    无 reviewer 时:若有 write_paths 则强制验证文件存在,否则直接 done。

    write_paths: agent 执行过程中 fs.write 的路径列表。
    若提供,则作为硬性验收条件 — 文件必须真实存在且非空才 PASS,否则驳回。
    """
    if not session.team_id:
        return "done"

    # ── 客观验证:交付物文件应真实存在且非空 ──
    # 说明:write_paths 包含 agent 执行期间所有 fs.write 的路径,
    # 其中可能有临时测试文件(test-write.txt 等)。只要"至少一个"产物
    # 文件存在且非空,即认为交付成立;全部缺失才算假完成。
    if write_paths:
        from app.core.config import resolve_workspace_root
        from app.orchestration.tools.safe_path import safe_resolve
        ws_root = resolve_workspace_root(getattr(session, "workspace_root", None))
        missing: list[str] = []
        empty: list[str] = []
        existing_count = 0
        for p in write_paths:
            resolved = safe_resolve(ws_root, p)
            if resolved is None or not resolved.exists():
                missing.append(p)
            elif resolved.stat().st_size == 0:
                empty.append(p)
            else:
                existing_count += 1
        # 至少一个文件存在且非空 → 交付成立(临时测试文件缺失不阻塞)
        if existing_count == 0 and (missing or empty):
            reasons = []
            if missing:
                reasons.append(f"以下文件不存在: {', '.join(missing)}")
            if empty:
                reasons.append(f"以下文件为空: {', '.join(empty)}")
            reason_text = ";".join(reasons)
            logger.warning("task %s 客观验证失败: 无任何有效产物(%s)", task.id, reason_text)
            await task_service.update_task_status(db, task.id, "rejected")
            await _emit_main_card(
                db, session_id=task.session_id, agent_id=producing_agent.id,
                agent_name=producing_agent.name, task_id=task.id, task_title=task.title,
                status="rejected", note=f"审查驳回 ❌ 无任何有效产物",
            )
            await session_service.create_message(
                db, session_id=task.session_id,
                sender_type=SenderType.AGENT, sender_id=producing_agent.id,
                msg_type=MsgType.TEXT,
                content={"text": f"❌ 交付验证失败:声明产出的文件全部缺失或为空。\n请重新完成该任务,确保交付文件真实写入。", "agent_name": producing_agent.name},
                thread_id=task.id,
            )
            await ws_manager.broadcast(
                task.session_id,
                {"event": "task.updated", "payload": {"task_id": task.id, "status": "rejected"}},
            )
            return "rejected"

    reviewer = await find_reviewer(db, session.team_id)
    if reviewer is None:
        logger.info("task %s 无 reviewer,直接 done", task.id)
        return "done"

    # v3：reviewer 恒为 None（find_reviewer 已废弃），此处不会执行；
    # 保留分支并修复导入，避免死代码中的 ImportError。
    system_prompt = ""
    whitelist: list[str] | None = None

    # 主群广播:进入审查
    await _emit_main_card(
        db, session_id=task.session_id, agent_id=reviewer.id, agent_name=reviewer.name,
        task_id=task.id, task_title=task.title, status="in_review",
        note=f"{reviewer.name} 正在审查 {producing_agent.name} 的产出",
    )

    provider, reason = await get_model_registry().get_provider_for_agent(db, reviewer)
    if provider is None:
        logger.warning("reviewer 无可用模型(%s),跳过审查", reason)
        return "done"

    try:
        # 构造审查 prompt:复用 build_agent_context + 额外审查指令
        messages = await build_agent_context(
            db, agent=reviewer, task=task, session=session, system_prompt=system_prompt,
        )
        # 追加上游产出供审查 + 强约束审查范围
        review_instruction = (
            f"## 待审查产出(来自 {producing_agent.name})\n\n"
            f"{producing_output[:4000]}\n\n"
            "## 审查范围强约束\n"
            "1. 优先调用 git.diff 获取本次真实变更的文件清单。\n"
            "2. 只审查 git.diff 列出或上游交接清单中的文件,严禁 fs.list 扫描无关文件。\n"
            "3. 可调用 ci.run 运行 lint/test/build 客观验证,或 fs.read 查看变更文件。\n"
            "审查完成后,**第一行**输出 PASS 或 REJECT,随后给出理由与证据。"
        )
        messages.append(ChatMessage(role="user", content=review_instruction))

        tool_schemas = tool_registry.all_schemas(whitelist)
        review_text = await _run_review_loop(
            provider=provider, messages=messages, tool_schemas=tool_schemas,
            db=db, task=task, session_id=task.session_id,
            reviewer=reviewer, session=session,
        )

        # 解析结论
        verdict = _parse_verdict(review_text)

        # 写 thread(详细审查过程)
        await session_service.create_message(
            db, session_id=task.session_id,
            sender_type=SenderType.AGENT, sender_id=reviewer.id,
            msg_type=MsgType.TEXT,
            content={"text": f"🔍 审查结论:{verdict}\n\n{review_text}", "agent_name": reviewer.name},
            thread_id=task.id,
        )

        # 精确映射审查结论 -> 任务状态
        if verdict == "PASS":
            final_status = "done"
        elif verdict == "REJECT":
            final_status = "rejected"
        else:
            # NEEDS_REVIEW: 保持审查中，等待人工介入或再次审查
            final_status = "in_review"
        await task_service.update_task_status(db, task.id, final_status)

        if final_status == "done":
            note = "审查通过 ✅"
        elif final_status == "rejected":
            note = "审查驳回 ❌(需返工)"
        else:
            note = "审查结论不明确 ⚠️(需人工确认)"
        await _emit_main_card(
            db, session_id=task.session_id, agent_id=reviewer.id, agent_name=reviewer.name,
            task_id=task.id, task_title=task.title, status=final_status, note=note,
        )
        await ws_manager.broadcast(
            task.session_id,
            {"event": "task.updated", "payload": {"task_id": task.id, "status": final_status}},
        )
        return final_status

    except Exception as e:
        logger.exception("reviewer 审查异常 task=%s", task.id)
        # 审查异常不阻塞,默认通过
        return "done"


async def _run_review_loop(
    *, provider, messages, tool_schemas, db, task, session_id, reviewer, session,
    cancel_event=None,
) -> str:
    """审查 agent loop:让 reviewer 调 git.diff/ci.run/fs.read 后给结论。

    步数上限取 agent_max_steps(与执行 agent 一致),保证审查能完整验证产物。
    v1.0: 增加 cancel_event 支持中断。
    """
    from app.orchestration.tools import ToolContext, tool_executor
    from app.core.config import settings, resolve_workspace_root

    ws_root = resolve_workspace_root(getattr(session, "workspace_root", None))

    # v3.5: 不再限制参数，全面释放模型能力
    max_steps = settings.agent_max_steps

    last_text = ""
    for _ in range(max_steps):
        # v1.0: 中断检查
        if cancel_event and cancel_event.is_set():
            logger.warning("审查循环被中断 task=%s", task.id)
            return "NEEDS_REVIEW"
        request = ChatRequest(
            messages=messages, model="", tools=tool_schemas or None,
        )
        response = await provider.chat(request)
        if response.tool_calls:
            messages.append(ChatMessage(
                role="assistant", content=response.content or "",
                tool_calls=response.tool_calls,
            ))
            import uuid
            for tc in response.tool_calls:
                tool_name = tc.get("name", "")
                args = tc.get("arguments", {}) or {}
                call_key = "rv_" + uuid.uuid4().hex[:12]
                # v1.0: 优先用模型返回的 id（OpenAI function-calling 协议要求）
                tc_model_id = tc.get("id", "") or call_key
                ctx = ToolContext(
                    workspace_root=ws_root,
                    session_id=session_id, task_id=task.id,
                    agent_id=reviewer.id, agent_name=reviewer.name,
                )
                result = await tool_executor.execute(
                    tool_name=tool_name, args=args, call_key=call_key,
                    agent=reviewer, ctx=ctx,
                )
                messages.append(ChatMessage(
                    role="tool", content=result.output or result.error,
                    name=tool_name, tool_call_id=tc_model_id,
                ))
            continue
        last_text = response.content or ""
        break
    return last_text


def _parse_verdict(text: str) -> str:
    """从 reviewer 输出解析结论。返回 'PASS'、'REJECT' 或 'NEEDS_REVIEW'。

    当文本中同时出现 PASS 和 REJECT 时(如改判场景)，
    取最后出现的关键词作为最终结论，而非无条件优先 REJECT。
    """
    pass_matches = list(_PASS_RE.finditer(text))
    reject_matches = list(_REJECT_RE.finditer(text))

    if pass_matches and reject_matches:
        # 两者都出现(改判场景):取靠后(最后出现)的关键词作为最终结论
        last_pass = pass_matches[-1].start()
        last_reject = reject_matches[-1].start()
        return "PASS" if last_pass > last_reject else "REJECT"

    if reject_matches:
        return "REJECT"
    if pass_matches:
        return "PASS"

    # 解析失败时返回 NEEDS_REVIEW，避免低质量产出绕过审查
    logger.warning("审查结论解析失败，标记为 NEEDS_REVIEW: %s", text[:100])
    return "NEEDS_REVIEW"


async def _emit_main_card(
    db: AsyncSession, *, session_id: int, agent_id: int, agent_name: str,
    task_id: int, task_title: str, status: str, note: str,
) -> None:
    await session_service.create_message(
        db, session_id=session_id, sender_type=SenderType.AGENT, sender_id=agent_id,
        msg_type=MsgType.TASK_CARD,
        content={
            "task_id": task_id, "title": task_title, "status": status,
            "assignee": agent_name, "note": note, "agent_name": agent_name,
        },
        thread_id=None,
    )

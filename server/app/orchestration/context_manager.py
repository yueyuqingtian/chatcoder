"""分层上下文构建器（v2）。

统一入口 build(agent, turn, session, project) → ContextBundle。
分层片段（注意力递减）：
1. Current Goal  2. Working Directory & Tool Rules  3. Git Repos  4. Project Rules
5. Project Structure  6. Session Memory  7. Subagent Handoff  8. Skills/MCP
9. Global Context  10. Token Budget
"""
import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import ChatMessage
from app.orchestration.prompts import build_main_system_prompt, build_subagent_system_prompt
from app.orchestration.rules_loader import load_session_rules, project_structure_brief

logger = logging.getLogger(__name__)


@dataclass
class ContextBundle:
    system: str
    developer_parts: list[str] = field(default_factory=list)
    history: list[ChatMessage] = field(default_factory=list)
    instruction: str = ""

    def to_messages(self) -> list[ChatMessage]:
        """组装 system + 合并 developer + 历史 + user 指令。"""
        messages = [ChatMessage(role="system", content=self.system)]
        if self.developer_parts:
            messages.append(ChatMessage(role="developer", content="\n\n".join(self.developer_parts)))
        messages.extend(self.history)
        if self.instruction:
            messages.append(ChatMessage(role="user", content=self.instruction))
        return messages


async def _load_skills_and_mcp(db: AsyncSession) -> tuple[str, str]:
    """全局技能/MCP 摘要（尽力而为，失败返回空）。"""
    skills_text = mcp_text = ""
    try:
        from app.services.skill_service import get_global_skills
        skills = await get_global_skills(db)
        if skills:
            parts = [f"- {s.display_name or s.name}: {(s.description or '')[:200]}" for s in skills[:10]]
            skills_text = "\n".join(parts)
    except Exception:
        logger.warning("[context] 全局技能加载失败", exc_info=True)
    try:
        from app.services.skill_service import get_global_mcp_servers
        servers = await get_global_mcp_servers(db)
        if servers:
            mcp_text = "\n".join(f"- {s.display_name or s.name}" for s in servers[:10])
    except Exception:
        logger.warning("[context] MCP 服务器加载失败", exc_info=True)
    return skills_text, mcp_text


async def _load_memories(db: AsyncSession, session_id: int) -> str:
    """注入记忆条目（D8）。"""
    try:
        from app.services.memory_service import load_memories
        entries = await load_memories(db, session_id)
        if entries:
            return "\n".join(f"- {e.text}" for e in entries)
    except Exception:
        logger.warning("[context] 记忆加载失败 session=%s", session_id, exc_info=True)
    return ""


async def _session_memory_summary(db: AsyncSession, session_id: int) -> str:
    """turn 级分层记忆摘要（最近 N 轮）。"""
    try:
        from sqlalchemy import select
        from app.persistence.models.turn import Turn
        res = await db.execute(
            select(Turn).where(Turn.session_id == session_id, Turn.status == "completed")
            .order_by(Turn.id.desc()).limit(5)
        )
        turns = list(res.scalars().all())
        lines = [f"- Turn {t.id}: {(t.summary or '')[:150]}" for t in reversed(turns) if t.summary]
        return "\n".join(lines) if lines else ""
    except Exception:
        logger.warning("[context] 会话记忆摘要失败 session=%s", session_id, exc_info=True)
        return ""


async def build_main_context(
    db: AsyncSession, *, agent, session, project, turn, user_message: str,
    attachments: list[dict] | None = None,
) -> ContextBundle:
    """构建主代理上下文。"""
    workspace = session.worktree_path or (project.path if project else "")
    bundle = ContextBundle(
        system=build_main_system_prompt(),
        instruction=user_message,
    )
    # 1. Current Goal
    bundle.developer_parts.append(f"## Current Goal\n{user_message[:2000]}")
    # 2. Working Directory & Tool Rules
    ws_ctx = f"Working directory: {workspace}"
    ws_ctx += (
        "\n\n## Tool Usage Rules\n"
        "Use tools via structured function calls. Never describe tool actions in natural language.\n"
        f"All paths are relative to the working directory: {workspace}."
    )
    bundle.developer_parts.append(ws_ctx)
    # 3. Git Repos（并入结构摘要）
    structure = await project_structure_brief(workspace)
    if structure:
        bundle.developer_parts.append(f"## Project Structure\n{structure}")
    # 4. Project Rules
    rules = await load_session_rules(workspace, project.rules_docs if project else None)
    if rules:
        bundle.developer_parts.append(f"## Project Rules\n{rules}")
    # 5. Session Memory（turn 摘要 + 记忆条目）
    mem_summary = await _session_memory_summary(db, session.id)
    if mem_summary:
        bundle.developer_parts.append(f"## Session Memory\n{mem_summary}")
    memories = await _load_memories(db, session.id)
    if memories:
        bundle.developer_parts.append(f"## Your Memory (from previous tasks)\n{memories}")

    # v6.4: 注入历史消息窗口 —— 修复上下文丢失问题
    # 根因：build_main_context 原本只注入 turn 摘要，不注入历史消息，
    # 导致 AI 每个 turn 都看不到之前的完整对话，只能看到摘要片段。
    # 现在直接取未摘要的历史消息，用 token 预算贪心选取，转成 ChatMessage 注入。
    try:
        from app.orchestration.context_memory import (
            _fetch_main_messages, _resolve_leader_context_window,
        )
        from app.orchestration.token_counter import (
            select_messages_by_token_budget, get_main_window_budget, MIN_MESSAGES_KEEP,
        )
        from app.models.schemas import ChatMessage as _CM

        context_window = await _resolve_leader_context_window(db, session)
        window_budget = get_main_window_budget(context_window)

        # v6.4: shared_context 是动态属性，可能不存在，用 getattr 安全访问
        ctx = getattr(session, "shared_context", None) or {}
        if not isinstance(ctx, dict):
            ctx = {}
        summarized_ids = set(ctx.get("summarized_ids") or [])

        # v6.4: 提高limit到2000，覆盖全部历史消息（原来200条会丢失早期对话）
        all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
        unsummarized = [m for m in all_msgs if m.id not in summarized_ids]

        # v6.5: 保留 text + tool_call + tool_result（过滤 thinking/plan 等非对话类型）。
        # 旧版只保留 text，导致 AI 看不到工具调用历史，上下文严重偏低且无法复用工具结果。
        from app.core.enums import MsgType as _MsgType
        _keep_types = {_MsgType.TEXT.value, _MsgType.TOOL_CALL.value, _MsgType.TOOL_RESULT.value}
        unsummarized = [m for m in unsummarized if m.msg_type in _keep_types]

        # Token-budget 选取：从最新向前贪心，直到预算耗尽
        recent, _ = select_messages_by_token_budget(
            unsummarized, window_budget, min_keep=MIN_MESSAGES_KEEP,
        )

        # v6.5: 在历史前插入明确标记，让AI知道这是完整历史对话而非摘要
        if recent:
            bundle.history.append(_CM(
                role="user",
                content="## Complete Conversation History (full text, not summary)\nBelow is the complete history of our conversation so far. This is the actual original text, NOT a compressed summary. You can see and reference all of it."
            ))
            bundle.history.append(_CM(
                role="assistant",
                content="Understood. I can see the complete conversation history below and will reference it as needed."
            ))

        # v6.5: 转成 ChatMessage，正确处理 text/tool_call/tool_result 三种类型。
        # tool_call -> assistant 带 tool_calls；tool_result -> tool 角色消息。
        # 保证 OpenAI tool_calls/tool 结果配对，避免网关 400 报错。
        #
        # v8: agent_loop 把"同一轮 assistant(文本+tool_calls)"落库为 text + tool_call 两条消息。
        # 若照旧各转成独立消息，会出现 assistant(tool_calls) 与 tool 结果之间夹着
        # assistant(文本) 的顺序，违反 OpenAI 协议（assistant(tool_calls) 后必须紧跟 tool），
        # 网关报 "An assistant message with 'tool_calls' must be followed by tool messages"。
        # 因此将紧随 tool_call 之前的 agent 文本合并进同一条 assistant(tool_calls) 消息。
        _pending_agent_text = ""
        for m in recent:
            if m.msg_type == _MsgType.TEXT.value:
                text = m.content.get("text") or m.content.get("note") or ""
                if not text:
                    continue
                if m.sender_type == "user":
                    if _pending_agent_text:
                        bundle.history.append(_CM(role="assistant", content=_pending_agent_text))
                        _pending_agent_text = ""
                    bundle.history.append(_CM(role="user", content=text))
                else:
                    # 暂存 agent 文本，等待下一个 tool_call 合并
                    _pending_agent_text = text
            elif m.msg_type == _MsgType.TOOL_CALL.value:
                # 工具调用作为 assistant 消息（带 tool_calls），合并暂存的 agent 文本
                tool_name = m.content.get("tool", "")
                args = m.content.get("args", {}) or {}
                call_key = m.content.get("call_key", "") or f"call_{m.id}"
                if isinstance(args, str):
                    try:
                        import json as _json
                        args = _json.loads(args)
                    except Exception:
                        args = {"_raw": args}
                bundle.history.append(_CM(
                    role="assistant",
                    content=_pending_agent_text or None,
                    tool_calls=[{
                        "id": call_key,
                        "name": tool_name,
                        "arguments": args,
                    }],
                ))
                _pending_agent_text = ""
            elif m.msg_type == _MsgType.TOOL_RESULT.value:
                # 工具结果作为 tool 角色消息
                if _pending_agent_text:
                    bundle.history.append(_CM(role="assistant", content=_pending_agent_text))
                    _pending_agent_text = ""
                tool_name = m.content.get("tool", "")
                call_key = m.content.get("call_key", "") or f"call_{m.id}"
                output = m.content.get("output", "") or ""
                error = m.content.get("error", "") or ""
                result_text = output or error or "(无输出)"
                # 截断过长的工具输出，避免单条结果撑爆窗口
                if len(result_text) > 4000:
                    result_text = result_text[:4000] + "\n...(工具输出已截断)"
                bundle.history.append(_CM(
                    role="tool",
                    content=result_text,
                    name=tool_name,
                    tool_call_id=call_key,
                ))
        if _pending_agent_text:
            # 循环结束：暂存的 agent 文本（无后续 tool_call）作为独立 assistant 消息
            bundle.history.append(_CM(role="assistant", content=_pending_agent_text))

        logger.info(
            "[context] session=%s 注入历史消息 %d 条 (window=%dK, budget=%d tokens, summarized=%d, recent=%d)",
            session.id, len(bundle.history),
            context_window // 1000, window_budget, len(summarized_ids), len(recent),
        )
        # v6.4 诊断：打印前3条和后3条历史消息的摘要
        for _i, _m in enumerate(recent[:3] + recent[-3:]):
            _text = (_m.content.get("text") or _m.content.get("note") or "")[:80]
            logger.info("[context] recent[%d] sender=%s text=%s", _i, _m.sender_type, _text)
    except Exception:
        logger.warning("[context] 注入历史消息失败(非阻塞)", exc_info=True)

    # 6. Skills / MCP
    skills, mcp = await _load_skills_and_mcp(db)
    if skills:
        bundle.developer_parts.append(f"## Available Skills\n{skills}")
    if mcp:
        bundle.developer_parts.append(f"## Available MCP Servers\n{mcp}")
    # 附件注入
    if attachments:
        doc_parts = [a.get("content") for a in attachments if a.get("content")]
        if doc_parts:
            bundle.instruction += "\n\n## 用户上传的附件\n" + "\n\n".join(str(p) for p in doc_parts)
    return bundle


async def build_subagent_context(
    db: AsyncSession, *, agent, session, project, task,
    handoff_summary: str,
) -> ContextBundle:
    """构建子代理上下文（独立，仅看交接摘要 + 项目规则）。"""
    workspace = session.worktree_path or (project.path if project else "")
    bundle = ContextBundle(
        system=build_subagent_system_prompt(task.title or "", task.acceptance_criteria or ""),
        instruction=f"Start working on: {task.title}",
    )
    bundle.developer_parts.append(f"## Current Task\nTitle: {task.title}")
    if task.description:
        bundle.developer_parts.append(f"Description: {task.description}")
    if handoff_summary:
        bundle.developer_parts.append(f"## Handoff Summary (from main agent)\n{handoff_summary}")
    ws_ctx = f"Working directory: {workspace}"
    ws_ctx += (
        "\n\n## Tool Usage Rules\n"
        "Use tools via structured function calls. Never describe tool actions in natural language.\n"
        f"All paths are relative to the working directory: {workspace}."
    )
    bundle.developer_parts.append(ws_ctx)
    rules = await load_session_rules(workspace, project.rules_docs if project else None)
    if rules:
        bundle.developer_parts.append(f"## Project Rules\n{rules}")
    return bundle

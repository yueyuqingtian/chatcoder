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

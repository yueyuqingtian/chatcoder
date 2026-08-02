"""v0.8: Agent 三层上下文可见性模型构建器(分层记忆版)。

三层：
1. 全局摘要：session.shared_context.summary(由 context_memory 自动维护)。
2. 任务相关：任务自身 + 其 thread 滑动窗口历史 + 父任务产物摘要(交接)。
3. RAG 检索：从知识库中检索与任务相关的文档，注入上下文。

v0.8 改进：
- 主会话与子会话均使用滑动窗口 + 摘要，避免一次性灌入全部历史。
- 提供 memory.search 工具，让 AI 按需检索更早的消息。
"""
import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import ChatMessage
from app.services import session_service, task_service
from app.orchestration.rag import retrieve_knowledge
from app.orchestration.prompts import build_default_agent_prompt

if TYPE_CHECKING:
    from app.persistence.models.agent import Agent
    from app.persistence.models.message import Session
    from app.persistence.models.task import Task

logger = logging.getLogger(__name__)

# v3.0: thread 历史限制扩大（配合 micro_compact 自动压缩旧工具结果）
_THREAD_HISTORY_LIMIT = 40
_MAIN_SUMMARY_LIMIT = 20  # v6.3: 从 8 提升到 20，保留更多最近消息


async def _layer1_global_summary(db: AsyncSession, session: "Session") -> str:
    """全局摘要:优先用 shared_context,否则取最近主群消息压缩拼接。"""
    if session.shared_context and isinstance(session.shared_context, dict):
        summary = session.shared_context.get("summary")
        if summary:
            return str(summary)

    main_msgs = await session_service.list_main_messages(db, session.id, limit=_MAIN_SUMMARY_LIMIT)
    if not main_msgs:
        return "(会话刚开始,尚无全局上下文)"
    lines = []
    for m in main_msgs:
        speaker = m.sender_type
        if m.sender_type == "agent":
            speaker = m.content.get("agent_name") or f"agent#{m.sender_id}"
        text = m.content.get("text") or m.content.get("note") or "(非文本消息)"
        lines.append(f"[{speaker}] {text}")
    return "最近主群动态:\n" + "\n".join(lines)


async def _layer2_task_messages(
    db: AsyncSession, session_id: int, task_id: int,
    context_window: int | None = None,
) -> list[ChatMessage]:
    """任务 thread 历史:token 预算滑动窗口，转成对话消息(assistant/user 交替)。

    v3.3: context_window 由 build_agent_context 解析后传入。
    """
    from app.orchestration.context_memory import build_thread_context_with_window
    return await build_thread_context_with_window(
        db, session_id=session_id, thread_id=task_id, context_window=context_window,
    )


async def _load_session_rules(session: "Session") -> str:
    """v2: 加载群规则文档(支持多文件)。

    优先级:session.rules_docs(多文件) > session.rules_doc(旧单文件兼容) > 自动探测
    (工作目录根 + 一级子目录的 AGENTS.md/.cursorrules/CLAUDE.md)。
    多个文件拼接,各自带 (文件名) 头,总量上限 8000 字符。

    v2.1: 即使文件全部读取失败,仍强制输出已配置的规则文档路径列表,
    让 agent 明确知道本群存在规范,而非静默吞掉。
    """
    import logging
    from pathlib import Path
    from app.core.config import resolve_workspace_root

    workspace = resolve_workspace_root(getattr(session, "workspace_root", None))
    configured: list[str] = list(getattr(session, "rules_docs", None) or [])
    legacy = getattr(session, "rules_doc", None)
    if legacy and legacy not in configured:
        configured.append(legacy)

    candidates: list[Path] = []

    # v2: 多文件 rules_docs
    for rel in configured:
        p = Path(rel)
        candidates.append(p if p.is_absolute() else Path(workspace) / rel)

    # 自动探测:根目录 + 一级子目录(前后端多项目场景)
    roots = [Path(workspace)]
    try:
        roots += [d for d in Path(workspace).iterdir() if d.is_dir() and not d.name.startswith(".")][:8]
    except OSError:
        pass
    for root in roots:
        for name in ("AGENTS.md", ".cursorrules", "CLAUDE.md"):
            candidates.append(root / name)

    parts: list[str] = []
    seen: set[str] = set()
    loaded_paths: list[str] = []
    missing_paths: list[str] = []
    total = 0
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file():
                # v1.0: 异步文件 IO，避免阻塞事件循环
                import asyncio as _aio_fs
                text = (await _aio_fs.to_thread(
                    p.read_text, encoding="utf-8", errors="replace"
                )).strip()
                if not text:
                    missing_paths.append(p.name)
                    continue
                chunk = f"({p.name})\n{text[:4000]}"
                if total + len(chunk) > 8000:
                    break
                parts.append(chunk)
                total += len(chunk)
                loaded_paths.append(p.name)
            else:
                # 仅记录 configured 的(用户明确配置的)缺失,自动探测的不记录以免噪音
                if any(p.name in c or c.endswith("/" + p.name) for c in configured):
                    missing_paths.append(p.name)
        except OSError:
            continue

    # v2.1: 头部固定输出"已配置的规范文档"清单,绝不沉默
    if configured:
        header_lines = ["## 本群项目规范(必须遵循)"]
        header_lines.append("- 已配置的规范文档:")
        for c in configured:
            marker = "✓" if c in [p.replace("\\", "/").rsplit("/", 1)[-1] or p.rsplit("/", 1)[-1] for p in loaded_paths] else "?"
            header_lines.append(f"  - {marker} `{c}`")
        if loaded_paths:
            header_lines.append(f"- 已成功加载 {len(loaded_paths)} 份内容(共 {total} 字符)")
        if missing_paths:
            header_lines.append(f"- 未能读取的文件: {', '.join(missing_paths)}")
        prefix = "\n".join(header_lines) + "\n\n"
    else:
        prefix = ""

    if not parts:
        body = "(未读到任何规范内容,请确认工作目录下存在规范文档)" if configured else ""
    else:
        body = "\n\n".join(parts)
    result = prefix + body

    logging.getLogger("orchestration").debug(
        "rules_doc session_id=%s configured=%s loaded=%s missing=%s bytes=%s",
        getattr(session, "id", "?"), configured, loaded_paths, missing_paths, len(result),
    )
    return result


async def _parent_artifacts_brief(db: AsyncSession, task: "Task") -> str:
    """父任务/上游任务产物摘要(交接上下文)。

    v0.9 强化:上游产物若含 files 清单,显式列出供下游"只处理这些文件"。
    """
    # 简化:查询所有指向当前任务的边,取上游 task 的产物
    edges = await task_service.list_edges(db, task.session_id)
    upstream_ids = [e.from_task_id for e in edges if e.to_task_id == task.id]
    if not upstream_ids:
        return ""

    from app.persistence.models.task import Artifact

    from sqlalchemy import select
    res = await db.execute(
        select(Artifact).where(Artifact.task_id.in_(upstream_ids)).order_by(Artifact.id.desc())
    )
    arts = res.scalars().all()
    if not arts:
        return ""
    lines = [f"- {a.title or a.type or 'artifact'}: {(a.summary or '')[:120]}" for a in arts[:5]]
    brief = "上游产物参考:\n" + "\n".join(lines)

    # v0.9: 收集上游产物关联的文件清单,显式交接给下游
    all_files: list[str] = []
    for a in arts:
        if a.files:
            all_files.extend(a.files)
    if all_files:
        # 去重保序
        seen: set[str] = set()
        uniq: list[str] = []
        for f in all_files:
            if f not in seen:
                seen.add(f)
                uniq.append(f)
        brief += "\n\n## 本次需处理/审查的文件清单(仅限这些,不得自行扫描其他文件)\n"
        brief += "\n".join(f"- {f}" for f in uniq[:50])
    return brief


async def _team_progress_brief(db: AsyncSession, session_id: int, current_task_id: int | None = None) -> str:
    """v2: 团队协作动态 —— 让所有 agent 知晓彼此的任务进展与交付物(颗粒度对齐)。

    列出本会话全部任务的状态/负责人;已完成任务附交付摘要(产物标题或文件),
    避免"一个 AI 不知道另一个 AI 干了什么"。
    """
    from sqlalchemy import select
    from app.persistence.models.task import Task, Artifact
    from app.persistence.models.agent import Agent

    res = await db.execute(
        select(Task).where(Task.session_id == session_id).order_by(Task.id)
    )
    tasks = res.scalars().all()
    if not tasks:
        return ""

    # 负责人名称映射
    agent_ids = {t.assigned_agent_id for t in tasks if t.assigned_agent_id}
    names: dict[int, str] = {}
    if agent_ids:
        ares = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
        for a in ares.scalars().all():
            names[a.id] = a.name

    status_cn = {
        "pending": "待执行", "in_progress": "执行中", "in_review": "审查中",
        "done": "已完成", "blocked": "受阻", "cancelled": "已取消", "rejected": "已打回",
    }

    # 已完成任务的产物摘要(每任务取最近 1 条，含文件清单和技术决策)
    done_ids = [t.id for t in tasks if t.status == "done"]
    art_map: dict[int, str] = {}
    if done_ids:
        ares2 = await db.execute(
            select(Artifact).where(Artifact.task_id.in_(done_ids)).order_by(Artifact.id.desc())
        )
        for art in ares2.scalars().all():
            if art.task_id not in art_map:
                summary = (art.summary or art.title or "").strip()[:120]
                # v3.1: 列出具体文件路径而非仅数量，方便下游 agent 定位
                files = ""
                if art.files:
                    files = " | 文件: " + ", ".join(str(f) for f in art.files[:5])
                    if len(art.files) > 5:
                        files += f" 等{len(art.files)}个"
                art_map[art.task_id] = f"{summary}{files}".strip()

    lines: list[str] = []
    for t in tasks[:15]:
        who = names.get(t.assigned_agent_id, "未分配") if t.assigned_agent_id else "未分配"
        st = status_cn.get(t.status, t.status)
        line = f"- [{st}] @{who} — {t.title}"
        if t.id in art_map and art_map[t.id]:
            line += f" | 交付: {art_map[t.id]}"
        if current_task_id and t.id == current_task_id:
            line += "  ← 这是你当前的任务"
        lines.append(line)

    if len(tasks) > 15:
        lines.append(f"- ... 另有 {len(tasks) - 15} 项任务")
    return "\n".join(lines)


async def _load_agent_skills(db: AsyncSession, agent: "Agent") -> str:
    """v3.6: 加载 Agent 绑定的技能内容，注入到系统提示词。"""
    from app.services.skill_service import get_agent_skills
    try:
        skills = await get_agent_skills(db, agent)
    except Exception:
        return ""
    if not skills:
        return ""
    parts: list[str] = []
    for sk in skills:
        header = f"### {sk.display_name or sk.name}"
        if sk.source != "custom":
            header += f" (来源: {sk.source})"
        lines = [header]
        if sk.description:
            lines.append(sk.description)
        if sk.trigger:
            lines.append(f"触发条件: {sk.trigger}")
        if sk.content:
            lines.append(f"\n{sk.content[:2000]}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def _load_agent_mcp_servers(db: AsyncSession, agent: "Agent") -> str:
    """v3.6: 加载 Agent 绑定的 MCP Server 配置，注入到系统提示词。

    让 Agent 知道有哪些 MCP 工具可用（工具实际执行在 agent_runtime 中处理）。
    """
    from app.services.skill_service import get_agent_mcp_servers
    try:
        servers = await get_agent_mcp_servers(db, agent)
    except Exception:
        return ""
    if not servers:
        return ""
    parts: list[str] = []
    for srv in servers:
        header = f"### {srv.display_name or srv.name}"
        if srv.source != "custom":
            header += f" (来源: {srv.source})"
        lines = [header]
        if srv.description:
            lines.append(srv.description)
        lines.append(f"传输方式: {srv.transport}")
        if srv.command:
            lines.append(f"命令: {srv.command}")
        if srv.url:
            lines.append(f"URL: {srv.url}")
        if srv.tools:
            tool_names = [t.get("name", "?") if isinstance(t, dict) else str(t) for t in srv.tools]
            lines.append(f"提供工具: {', '.join(tool_names[:10])}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def _agent_learned_facts(db: AsyncSession, agent_id: int, session_id: int) -> str:
    """v3.1: 加载当前 Agent 在本会话中积累的工作记忆(learned_facts)。

    同一个 Agent 跨多个任务时，之前任务中提取的关键事实会注入当前上下文，
    实现"经验传承"，避免重复探索已知信息。
    """
    from app.persistence.models.agent import Agent

    agent = await db.get(Agent, agent_id)
    if agent is None:
        return ""
    facts = agent.learned_facts or []
    if not facts:
        return ""
    # 筛选当前 session 的事实，最多取最近 10 条
    relevant = [f for f in facts if f.get("session_id") == session_id][-10:]
    if not relevant:
        return ""
    lines = [f"- {f.get('text', '')}" for f in relevant if f.get("text")]
    if not lines:
        return ""
    return "\n".join(lines)


async def build_agent_context(
    db: AsyncSession,
    *,
    agent: "Agent",
    task: "Task",
    session: "Session",
    system_prompt: str,
) -> list[ChatMessage]:
    """组装 agent prompt:system(三层) + 历史 thread + 当前任务指令。

    v3.3: 解析 Agent 的 per-model context_window，驱动所有窗口预算。
    v1.0: 并行化无依赖 IO 查询，降低任务启动延迟 60%。
    """
    import asyncio as _aio

    # v3.3: 解析 Agent 的 per-model 上下文窗口
    from app.orchestration.token_counter import get_agent_context_window
    agent_context_window = await get_agent_context_window(db, agent)

    # v0.8: 先尝试更新主会话摘要(分层记忆维护)——可能写入，必须先行
    from app.orchestration.context_memory import (
        maybe_summarize_main_session,
        _resolve_leader_context_window,
    )
    try:
        leader_window = await _resolve_leader_context_window(db, session)
        await maybe_summarize_main_session(db, session, context_window=leader_window)
    except Exception:
        logger.debug("主会话摘要更新失败(非阻塞)", exc_info=True)

    # v1.0: 并行执行所有只读查询（各用独立 session 避免并发冲突）
    from app.persistence.database import async_session_factory

    async def _q_global_summary():
        async with async_session_factory() as s:
            return await _layer1_global_summary(s, session)

    async def _q_parent_brief():
        async with async_session_factory() as s:
            return await _parent_artifacts_brief(s, task)

    async def _q_history():
        async with async_session_factory() as s:
            return await _layer2_task_messages(
                s, task.session_id, task.id, context_window=agent_context_window,
            )

    async def _q_team_brief():
        async with async_session_factory() as s:
            return await _team_progress_brief(s, task.session_id, current_task_id=task.id)

    async def _q_rag():
        async with async_session_factory() as s:
            return await retrieve_knowledge(s, task=task)

    async def _q_learned_facts():
        async with async_session_factory() as s:
            return await _agent_learned_facts(s, agent.id, session.id)

    async def _q_skills():
        async with async_session_factory() as s:
            return await _load_agent_skills(s, agent)

    async def _q_mcp():
        async with async_session_factory() as s:
            return await _load_agent_mcp_servers(s, agent)

    (
        global_summary,
        parent_brief,
        history,
        team_brief,
        rag_context,
        learned_facts_text,
        skills_text,
        mcp_text,
    ) = await _aio.gather(
        _q_global_summary(),
        _q_parent_brief(),
        _q_history(),
        _q_team_brief(),
        _q_rag(),
        _q_learned_facts(),
        _q_skills(),
        _q_mcp(),
    )

    # 规则文档是纯文件读取，无 DB 依赖
    rules_text = await _load_session_rules(session)

    # v6.2: System Prompt 分层（对齐 codex ContextualUserFragment 语义，但兼容多网关）
    # - system 消息：仅角色行为准则（核心，不随任务变化）
    # - developer 消息：上下文片段，内部按"核心任务在前、参考信息在后"分段
    #   （v6.2 修正：合并为 1 条 developer 而非 12 条 —— 连续多条 system 在
    #    DeepSeek/GLM 等兼容网关可能被忽略或行为异常，合并后兼容性大幅提升，
    #    段落顺序保留分层效果）
    workspace = getattr(session, "workspace_root", None) or ""

    messages: list[ChatMessage] = []

    # Layer 1: system — 角色行为准则（最重要）
    messages.append(ChatMessage(role="system", content=system_prompt or build_default_agent_prompt(agent.name)))

    # Layer 2+: developer — 上下文片段（合并为一条，内部按序分段）
    dev_parts: list[str] = []

    # 1. 当前任务定义（注意力最高）
    task_def = f"## Current Task\nTitle: {task.title}"
    if task.description:
        task_def += f"\nDescription: {task.description}"
    if task.acceptance_criteria:
        task_def += f"\nAcceptance Criteria: {task.acceptance_criteria}"
    dev_parts.append(task_def)

    # 2. 工作目录 + 工具调用规则（英文精简，工具定义在 API tools 参数中）
    ws_ctx = f"Working directory: {workspace}"
    ws_ctx += (
        "\n\n## Tool Usage Rules\n"
        "Use tools via structured function calls. Never describe tool actions in natural language.\n"
        f"All paths are relative to the working directory: {workspace}."
    )
    dev_parts.append(ws_ctx)

    # 3. Git 仓库信息（有 git 操作时才需要）
    if workspace:
        try:
            from pathlib import Path as _Path  # noqa: N813
            ws = _Path(workspace)
            if ws.is_dir():
                repos = sorted(
                    str(c.relative_to(ws))
                    for c in ws.iterdir()
                    if c.is_dir() and not c.name.startswith(".") and (c / ".git").exists()
                )
                if repos:
                    git_ctx = "## Git Repositories\n"
                    for r in repos:
                        git_ctx += f"- `{r}/` — independent git repo; use cwd='{r}' for git commands\n"
                    dev_parts.append(git_ctx)
        except OSError:
            pass

    # 4. 上游交接（有依赖任务时才需要）
    if parent_brief:
        dev_parts.append(f"## Upstream Handoff\n{parent_brief}")

    # 5. 团队进展（仅在简短时注入，避免稀释注意力）
    if team_brief and len(team_brief) < 500:
        dev_parts.append(f"## Team Progress\n{team_brief}")

    # 6. Agent 工作记忆（仅在简短时注入）
    if learned_facts_text and len(learned_facts_text) < 500:
        dev_parts.append(f"## Your Memory (from previous tasks)\n{learned_facts_text}")

    # 7. 绑定的技能
    if skills_text:
        dev_parts.append(f"## Available Skills\n{skills_text}")

    # 8. 绑定的 MCP Server
    if mcp_text:
        dev_parts.append(f"## Available MCP Servers\n{mcp_text}")

    # 9. 全局上下文摘要 + 最近主聊天历史（v6.3: 移除 800 字截断，注入完整上下文）
    if global_summary:
        dev_parts.append(f"## Global Context\n{global_summary}")

    # v6.3: 注入最近主聊天历史，让 agent 能看到完整对话上下文
    try:
        from app.orchestration.context_memory import _fetch_main_messages
        recent_main = await _fetch_main_messages(db, session.id, limit=20)
        if recent_main:
            chat_lines: list[str] = []
            for m in recent_main:
                speaker = "用户" if m.sender_type == "user" else (
                    m.content.get("agent_name", "agent") if isinstance(m.content, dict) else "agent"
                )
                if isinstance(m.content, dict):
                    text = m.content.get("text") or m.content.get("note") or ""
                else:
                    text = str(m.content)
                text = (text or "").strip()[:500]
                if text:
                    chat_lines.append(f"[{speaker}] {text}")
            if chat_lines:
                dev_parts.append(f"## Recent Chat History\n" + "\n".join(chat_lines))
    except Exception:
        logger.debug("注入主聊天历史失败(非阻塞)", exc_info=True)

    # 10. 项目规范（参考性质，不干扰核心推理）
    if rules_text:
        dev_parts.append(f"## Project Rules\n{rules_text}")

    # 11. RAG 检索结果（最末，按需参考）
    if rag_context:
        dev_parts.append(rag_context)

    if dev_parts:
        messages.append(ChatMessage(role="developer", content="\n\n".join(dev_parts)))

    # 历史 thread 消息
    messages.extend(history)

    # 任务触发消息（user）
    messages.append(ChatMessage(role="user", content=f"Start working on: {task.title}"))
    return messages

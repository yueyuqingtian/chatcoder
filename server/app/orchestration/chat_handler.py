"""群聊统一消息处理。

v2.5: 重构为单步编排 —— Leader 一次性决定:闲聊回复 or 拆解任务派活。
不再分三步(意图判断→拆解→派活),减少 LLM 调用次数,提升响应速度和准确度。

核心流程:
1. 用户消息(@某人 → 该人直接执行;无@ → Leader 编排)
2. Leader 返回 JSON: {action, message, tasks}
3. action=reply → 纯回复; action=execute → 持久化任务 + 广播 + 自动调度
"""
import json
import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.models.registry import get_model_registry
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.orchestrator import create_tasks_from_plan, _match_agent
from app.orchestration.prompts import (
    build_leader_system_prompt,
    build_orchestrate_user_prompt,
    parse_orchestrate_response,
)
from app.persistence.models.agent import Agent
from app.services import session_service, task_service, team_service

# v3 兼容别名：团队时代代码引用 AgentTemplate / TeamAgent，
# v2 已统一为 Agent；保留别名让旧编排逻辑可导入，语义上 Agent 取代二者。
AgentTemplate = Agent
TeamAgent = Agent

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.persistence.models.message import Session


async def _get_team_members(db: AsyncSession, team_id: int) -> tuple[list, dict[int, str]]:
    """返回 (team_members 列表, agent_id->name 映射)。"""
    agents = await team_service.list_team_agents(db, team_id)
    template_ids = {a.template_id for a in agents if a.template_id}
    tmpl_map: dict[int, AgentTemplate] = {}
    if template_ids:
        res = await db.execute(select(AgentTemplate).where(AgentTemplate.id.in_(template_ids)))
        tmpl_map = {t.id: t for t in res.scalars()}
    members = []
    name_map: dict[int, str] = {}
    for a in agents:
        role = (
            tmpl_map[a.template_id].role
            if a.template_id and tmpl_map.get(a.template_id)
            else "engineer"
        )
        members.append({"id": a.id, "name": a.name, "role": role})
        name_map[a.id] = a.name
    return members, name_map


async def _get_leader_agent(db: AsyncSession, team_id: int):
    """v3：团队概念已移除，改为按 session 查主代理（kind='main'）。

    保留 team_id 参数仅为兼容调用方签名；实际忽略。
    """
    from sqlalchemy import select
    res = await db.execute(select(Agent).where(Agent.kind == "main").order_by(Agent.id.desc()))
    agents = list(res.scalars().all())
    return agents[0] if agents else None


async def handle_chat_message(
    db: AsyncSession,
    *,
    session_id: int,
    content: str,
    mentions: list[int] | None = None,
    attachments: list[dict] | None = None,
    mode: str = "team",
) -> dict:
    """群聊消息统一处理入口。

    v2.5: 单步编排,Leader 一次性决定 action + message + tasks。
    v1.0: 支持附件(图片/文档)注入多模态上下文。
    v1.0: 支持 mode="quick" 单 Agent 快速模式（跳过拆解/调度/审查）。
    """
    session = await session_service.get_session(db, session_id)
    if not session or not session.team_id:
        raise ValueError("session or team not found")

    # v1.0: 单 Agent 快速模式
    if mode == "quick":
        return await _handle_quick_mode(
            db, session_id=session_id, content=content, session=session,
        )

    members, name_map = await _get_team_members(db, session.team_id)

    # ── 1. 被 @ 了某人 → 该 agent 直接处理 ──
    if mentions:
        return await _route_to_mentioned(
            db, session_id=session_id, content=content,
            mentions=mentions, members=members, name_map=name_map,
            session=session,
        )

    # ── 2. 无 @ → Leader 统一编排 ──
    leader = await _get_leader_agent(db, session.team_id)

    # v2.0: 优先使用 Leader 绑定的模型,而非全局默认
    provider = None
    if leader:
        from app.models.registry import get_model_registry
        provider, _reason = await get_model_registry().get_provider_for_agent(db, leader)
    if not provider:
        from app.models.registry import get_model_registry
        provider = get_model_registry().get_default_provider()
    if not provider:
        await session_service.create_message(
            db, session_id=session_id,
            sender_type=SenderType.AGENT, sender_id=leader.id if leader else None,
            msg_type=MsgType.TEXT,
            content={"text": "(当前未配置模型,无法回复)\n\n请在「设置 → 模型」中为 Leader 绑定模型并配置 API Key。", "agent_name": leader.name if leader else "Leader"},
            thread_id=None,
        )
        return {"intent": "chat", "reply": "(未配置模型)", "tasks": None}

    # v3.3: 统一使用 build_leader_context_lines（token 预算动态窗口 + 摘要）
    # 替代原来手动的 limit=20 + 无摘要逻辑，消除记忆黑洞
    from app.orchestration.context_memory import build_leader_context_lines
    summary_text, context_lines = await build_leader_context_lines(db, session)

    # v3.1: 将历史摘要注入 Leader 上下文
    extra_parts = []
    if summary_text:
        extra_parts.append("## 历史摘要(更早的群聊已压缩)\n" + summary_text)
    workspace = getattr(session, "workspace_root", None) or ""
    if workspace:
        try:
            from pathlib import Path
            ws = Path(workspace)
            if ws.is_dir():
                repos = sorted(
                    str(c.relative_to(ws))
                    for c in ws.iterdir()
                    if c.is_dir() and not c.name.startswith(".") and (c / ".git").exists()
                )
                if repos:
                    extra_parts.append("工作目录下有独立 git 仓库: " + ", ".join(repos))
        except OSError:
            pass
    rules_info = list(getattr(session, "rules_docs", None) or [])
    if rules_info:
        extra_parts.append("已配置规范文档: " + ", ".join(rules_info))

    # v1.0: 注入全局规则
    try:
        from app.gateway.routers.settings import _load_config
        global_cfg = _load_config()
        global_rules = global_cfg.get("global_rules", "")
        if global_rules:
            extra_parts.append("\n## 全局规则\n" + global_rules)
    except Exception:
        pass

    system_prompt = build_leader_system_prompt(
        members, workspace=workspace, extra_context="\n".join(extra_parts),
    )
    user_prompt = build_orchestrate_user_prompt(content, members, context_lines)

    # v1.0: 附件注入到 user message
    content_blocks = None
    if attachments:
        user_text = user_prompt
        doc_parts = []
        content_blocks = []
        for att in attachments:
            if att.get("type") == "image" and att.get("data_url"):
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": att["data_url"]},
                })
            elif att.get("content"):
                doc_parts.append(att["content"])
        if doc_parts:
            user_text = user_text + "\n\n## 用户上传的附件\n" + "\n\n".join(doc_parts)
            user_prompt = user_text

    # v1.0: 显式设置 max_tokens，避免网关默认输出限制导致 Leader JSON 被截断
    user_msg = ChatMessage(role="user", content=user_prompt, content_blocks=content_blocks)
    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content=system_prompt),
            user_msg,
        ],
        model="",
        max_tokens=8192,  # Leader 的 JSON 响应需要足够输出空间
    )
    resp = await provider.chat(request)
    # v1.0: 详细日志——记录 Leader 响应的关键诊断信息
    _raw = resp.content or ""
    logger.info(
        "[chat] Leader 响应诊断: finish_reason=%s, raw_len=%d, usage=%s, raw_tail='%s'",
        resp.finish_reason,
        len(_raw),
        f"p={resp.usage.prompt_tokens}/c={resp.usage.completion_tokens}/t={resp.usage.total_tokens}" if resp.usage else "none",
        _raw[-100:] if len(_raw) > 100 else _raw,
    )
    decision = parse_orchestrate_response(resp.content)
    logger.info("[chat] Leader 决策: action=%s tasks=%d msg_len=%d", decision["action"], len(decision["tasks"]), len(decision.get("message", "")))

    # v4.4: 广播 Leader 的 token 用量，使用 API 精确数据 + per-model 上下文窗口
    leader = await _get_leader_agent(db, session.team_id)
    from app.gateway.ws import manager as ws_manager
    from app.orchestration.context_memory import _resolve_leader_context_window
    _api_prompt = resp.usage.prompt_tokens if resp.usage else 0
    _api_completion = resp.usage.completion_tokens if resp.usage else 0
    _leader_window = await _resolve_leader_context_window(db, session)
    await ws_manager.broadcast(session_id, {
        "event": "usage.update",
        "payload": {
            "agent_name": leader.name if leader else "Leader",
            "prompt_tokens": _api_prompt,
            "completion_tokens": _api_completion,
            "total_tokens": _api_prompt + _api_completion,
            "total_context_used": _api_prompt + _api_completion,
            "system_tokens": 0,  # v4.4: Leader 编排无工具定义
            "budget_used": _api_prompt + _api_completion,
            "finish_reason": resp.finish_reason,
            "context_window": _leader_window,
            # v6.2: 当前会话上下文真实占用（本次请求送入模型的输入量）
            "context_tokens_current": _api_prompt,
        },
    })

    if decision["action"] == "reply":
        # 纯回复
        reply = decision["message"]
        await session_service.create_message(
            db, session_id=session_id,
            sender_type=SenderType.AGENT, sender_id=leader.id if leader else None,
            msg_type=MsgType.TEXT,
            content={"text": reply, "agent_name": leader.name if leader else "Leader"},
            thread_id=None,
        )
        return {"intent": "chat", "reply": reply, "tasks": None}

    if decision["action"] == "direct":
        # v6.0: Leader 直连工具模式 -- 主聊天窗口直接对话也能调用工具/MCP
        if leader:
            leader_role = ""
            if getattr(leader, "template_id", None):
                tpl = await db.get(AgentTemplate, leader.template_id)
                if tpl:
                    leader_role = tpl.role or ""
            # 先把 Leader 的话术发到群里
            if decision.get("message"):
                await session_service.create_message(
                    db, session_id=session_id,
                    sender_type=SenderType.AGENT, sender_id=leader.id,
                    msg_type=MsgType.TEXT,
                    content={"text": decision["message"], "agent_name": leader.name},
                    thread_id=None,
                )
                await db.commit()
            return await _handle_direct_tool_loop(
                db, session_id=session_id, content=content,
                session=session, leader=leader, leader_role=leader_role,
            )
        # leader 不存在 -> 降级 reply
        await session_service.create_message(
            db, session_id=session_id,
            sender_type=SenderType.AGENT, sender_id=None,
            msg_type=MsgType.TEXT,
            content={"text": decision.get("message") or "(无法处理)", "agent_name": "Leader"},
            thread_id=None,
        )
        return {"intent": "chat", "reply": decision.get("message", ""), "tasks": None}

    # action == "execute" -> 持久化任务
    agents_list = await team_service.list_team_agents(db, session.team_id)
    # role 映射
    agent_roles = {}
    if agents_list:
        tids = {a.template_id for a in agents_list if a.template_id}
        if tids:
            res = await db.execute(select(AgentTemplate).where(AgentTemplate.id.in_(tids)))
            tmpl = {t.id: t for t in res.scalars()}
            for a in agents_list:
                if a.template_id and tmpl.get(a.template_id):
                    agent_roles[a.id] = tmpl[a.template_id].role

    tasks_created = await create_tasks_from_plan(
        db, session_id=session_id, plan_tasks=decision["tasks"],
        agents=agents_list, agent_roles=agent_roles,
    )

    # 发需求理解卡 + Leader 派活话术
    understanding = decision["message"]
    await session_service.create_message(
        db, session_id=session_id,
        sender_type=SenderType.AGENT, sender_id=leader.id if leader else None,
        msg_type=MsgType.TASK_CARD,
        content={
            "understanding": understanding,
            "tasks": [{"task_id": t["task_id"], "title": t["title"]} for t in tasks_created],
        },
        thread_id=None,
    )
    await session_service.create_message(
        db, session_id=session_id,
        sender_type=SenderType.AGENT, sender_id=leader.id if leader else None,
        msg_type=MsgType.TEXT,
        content={"text": decision["message"], "agent_name": leader.name if leader else "Leader"},
        thread_id=None,
    )

    # 自动确认并调度
    session.plan_confirmed = True

    # v3.1: 新任务创建时重置完成标记，允许新一轮任务完成后再次发送总结
    ctx = session.shared_context or {}
    if isinstance(ctx, dict) and ctx.get("completion_announced"):
        ctx["completion_announced"] = False
        session.shared_context = ctx

    return {
        "intent": "task",
        "reply": decision["message"],
        "tasks": tasks_created,
        "auto_schedule": True,
    }


async def _route_to_mentioned(
    db: AsyncSession, *, session_id: int, content: str,
    mentions: list[int], members: list[dict], name_map: dict[int, str],
    session: "Session",
) -> dict:
    """被 @ 的人:若涉及实际工作则创建单任务执行,否则闲聊回复。"""
    target_id = mentions[0]
    target_name = name_map.get(target_id, f"Agent#{target_id}")
    target_role = next((m["role"] for m in members if m["id"] == target_id), "engineer")

    _WORK_VERBS = ("执行", "创建", "写入", "修改", "审查", "构建", "生成", "修复", "重构",
                   "开发", "实现", "删除", "运行", "查看", "读取", "分析", "部署", "打包",
                   "测试", "优化", "排查", "检查", "搜索", "安装", "跑", "写", "改", "看",
                   "校验", "验证", "确认", "连通", "读", "查", "连", "试试", "试一下")
    needs_action = any(v in content for v in _WORK_VERBS)

    if not needs_action:
        # v2.0: 优先使用被 @ 的 agent 绑定的模型
        agent_obj = await db.get(Agent, target_id)
        provider = None
        if agent_obj:
            provider, _reason = await get_model_registry().get_provider_for_agent(db, agent_obj)
        if not provider:
            provider = get_model_registry().get_default_provider()
        if not provider:
            await session_service.create_message(
                db, session_id=session_id,
                sender_type=SenderType.AGENT, sender_id=target_id,
                msg_type=MsgType.TEXT,
                content={"text": "(当前未配置模型)\n\n请在「设置 → 模型」中绑定模型并配置 API Key。", "agent_name": target_name},
                thread_id=None,
            )
            return {"intent": "chat", "reply": "(未配置模型)", "tasks": None}

        from app.orchestration.context_memory import build_main_chat_context
        history_msgs = await build_main_chat_context(db, session, content)

        from pathlib import Path as _Path
        ws = getattr(session, "workspace_root", None) or ""
        ws_parts = [
            f"你是 chatcoder 团队的 {target_name}（角色：{target_role}）。",
            "用户在群里 @ 你,请以你的身份简洁友好地回复。",
        ]
        if ws:
            ws_parts.append(f"\n## 当前工作目录\n{ws}")
            try:
                wp = _Path(ws)
                if wp.is_dir():
                    subdirs = [c.name for c in wp.iterdir() if c.is_dir() and not c.name.startswith(".")][:15]
                    files = [c.name for c in wp.iterdir() if c.is_file() and not c.name.startswith(".")][:10]
                    if subdirs:
                        ws_parts.append("子目录: " + ", ".join(subdirs))
                    if files:
                        ws_parts.append("根文件: " + ", ".join(files))
                    repos = [c.name for c in wp.iterdir() if c.is_dir() and (c / ".git").exists()]
                    if repos:
                        ws_parts.append("Git 仓库: " + ", ".join(repos))
            except OSError:
                pass
            rules_info = list(getattr(session, "rules_docs", None) or [])
            if rules_info:
                ws_parts.append("规范文档: " + ", ".join(rules_info))

        sys_prompt = "\n".join(ws_parts)

        # v3.5: 不再限制参数，全面释放模型能力
        request = ChatRequest(
            messages=[
                ChatMessage(role="system", content=sys_prompt),
                *history_msgs,
            ],
            model="",
        )
        resp = await provider.chat(request)
        reply = resp.content or "(没有回复内容)"
        await session_service.create_message(
            db, session_id=session_id,
            sender_type=SenderType.AGENT, sender_id=target_id,
            msg_type=MsgType.TEXT,
            content={"text": reply, "agent_name": target_name},
            thread_id=None,
        )
        return {"intent": "chat", "reply": reply, "tasks": None}

    # 涉及实际工作 → 创建单任务走 agent loop
    logger.info("[chat] @%s 涉及工作,创建单任务走 agent loop", target_name)
    import re as _re
    clean_content = _re.sub(r"^@\S+\s*", "", content).strip() or content
    task = await task_service.create_task(
        db, session_id=session_id, title=clean_content[:80],
        description=clean_content, assigned_agent_id=target_id,
        priority=1,
    )
    await session_service.create_message(
        db, session_id=session_id,
        sender_type=SenderType.AGENT, sender_id=target_id,
        msg_type=MsgType.TASK_CARD,
        content={
            "task_id": task.id, "title": clean_content[:80],
            "status": "pending", "assignee": target_name,
            "agent_name": target_name, "note": f"@{target_name} 已接收任务",
        },
        thread_id=None,
    )
    session.plan_confirmed = True
    # v3.1: 重置完成标记
    ctx = session.shared_context or {}
    if isinstance(ctx, dict) and ctx.get("completion_announced"):
        ctx["completion_announced"] = False
        session.shared_context = ctx
    return {
        "intent": "task", "reply": None,
        "tasks": [{"id": task.id, "title": clean_content[:80]}],
        "auto_schedule": True,
    }


async def _generate_completion_summary(
    db: AsyncSession, *, tasks: list, artifacts: list,
) -> str:
    """全部任务完成后,Leader 生成简短总结。v2.5: 更自然,不像模板。"""
    provider = get_model_registry().get_default_provider()
    if not provider:
        return "全部任务已完成。"

    task_lines = []
    agent_names: dict[int, str] = {}
    for t in tasks:
        name = agent_names.get(t.assigned_agent_id, "成员") if t.assigned_agent_id else "成员"
        mark = "✓" if t.status == "done" else "○"
        task_lines.append(f"{mark} @{name} — {t.title}")

    file_lines = []
    for a in artifacts[:20]:
        if a.summary:
            file_lines.append(f"- {a.summary}")
        elif a.title:
            file_lines.append(f"- {a.title}")

    from app.orchestration.prompts import build_completion_prompt
    prompt = build_completion_prompt(
        "\n".join(task_lines),
        "\n".join(file_lines) if file_lines else "",
    )
    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content="You are the team Leader. All tasks just completed. Briefly notify the team in the group chat."),
            ChatMessage(role="user", content=prompt),
        ],
        model="",
    )
    resp = await provider.chat(request)
    return resp.content or "全部任务已完成。"


# ---------------------------------------------------------------------------
# v1.0: 单 Agent 快速模式
# ---------------------------------------------------------------------------

async def _handle_quick_mode(
    db: AsyncSession,
    *,
    session_id: int,
    content: str,
    session: "Session",
) -> dict:
    """v1.0: 单 Agent 快速模式。

    跳过 Leader 拆解 / DAG 调度 / 审查，直接创建单任务 → 单 agent loop → 返回结果。
    适用于简单编码任务，对标 Claude Code 模式。
    """
    from app.orchestration.agent_runtime import run_agent_loop
    from app.core.config import resolve_workspace_root

    # 获取第一个可用 agent
    leader = await _get_leader_agent(db, session.team_id)
    if not leader:
        raise ValueError("团队中无可用 agent")

    # 创建单任务
    task = await task_service.create_task(
        db,
        session_id=session_id,
        title=content[:80],
        description=content,
        assigned_agent_id=leader.id,
        status="pending",
        priority=0,
    )
    await db.commit()

    # 广播任务创建
    from app.gateway.ws import manager as ws_manager
    await ws_manager.broadcast(session_id, {
        "event": "task.updated",
        "payload": {"task_id": task.id, "status": "in_progress"},
    })

    # 直接执行 agent loop
    workspace = resolve_workspace_root(getattr(session, "workspace_root", None))
    # v3：团队/模板概念已移除，leader_role 不再适用
    leader_role = ""
    result = await run_agent_loop(
        db,
        session_id=session_id,
        task_id=task.id,
        agent_id=leader.id,
        agent_name=leader.name,
        agent_role=leader_role,
        system_prompt=getattr(leader, "system_prompt", "") or "",
        template_whitelist=None,
        cancel_event=None,
    )
    await db.commit()

    return {
        "intent": "quick",
        "task_id": task.id,
        "status": "done" if result.kind == "message" else "blocked",
        "response": result.text or result.error or "",
    }


async def _handle_direct_tool_loop(
    db: AsyncSession,
    *,
    session_id: int,
    content: str,
    session: "Session",
    leader: TeamAgent,
    leader_role: str = "",
) -> dict:
    """v6.0: Leader 直连工具模式 -- 主聊天窗口直接对话也能调用工具/MCP。

    创建临时单任务 -> run_agent_loop（带工具 + MCP 注入）-> 产出回流主聊天窗口。
    适用于校验 MCP、读文件、单步查询等无需团队拆活的场景。
    复用 run_agent_loop 的工具执行、流式广播、MCP 注入逻辑，跳过 DAG 调度/审查。
    """
    from app.orchestration.agent_runtime import run_agent_loop

    task = await task_service.create_task(
        db, session_id=session_id, title=content[:80],
        description=content, assigned_agent_id=leader.id,
        status="pending", priority=0,
    )
    await db.commit()

    from app.gateway.ws import manager as ws_manager
    await ws_manager.broadcast(session_id, {
        "event": "task.updated",
        "payload": {"task_id": task.id, "status": "in_progress"},
    })

    result = await run_agent_loop(
        db, session_id=session_id, task_id=task.id,
        agent_id=leader.id, agent_name=leader.name,
        agent_role=leader_role,
        system_prompt="",  # 留空 -> build_agent_context 用默认兜底工作方法论
        template_whitelist=None, cancel_event=None,
    )
    await db.commit()

    return {
        "intent": "direct",
        "task_id": task.id,
        "status": "done" if result.kind == "message" else "blocked",
        "response": result.text or result.error or "",
    }

"""编排服务：任务持久化 + DAG 构建。

v2.5: 拆分为 create_tasks_from_plan（可复用）+ 旧 decompose_requirement（保留兼容）。
chat_handler 不再调用 decompose_requirement，改为直接传入 Leader 决策结果。
v1.0: 增加 DAG 环检测与自动修复。
"""
import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.models.registry import get_model_registry
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.dag import TaskDAG, TaskNode
from app.orchestration.prompts import LEADER_SYSTEM_PROMPT, build_decompose_prompt
from app.persistence.models.agent import Agent
from app.services import session_service, task_service, team_service

logger = logging.getLogger(__name__)

# v3 兼容别名：团队时代代码引用 AgentTemplate，v2 已统一为 Agent。
AgentTemplate = Agent


def _repair_dag_cycle(dag: TaskDAG) -> None:
    """v1.0: 尝试修复 DAG 中的环。

    策略: 找到入度最大的节点，移除其一条入边，重复直到无环或达到最大尝试次数。
    """
    max_attempts = 10
    for _ in range(max_attempts):
        if not dag.has_cycle():
            return
        # 找入度最大的节点
        max_in_node = max(dag.in_degree.items(), key=lambda x: x[1], default=None)
        if not max_in_node or max_in_node[1] == 0:
            return
        node_id = max_in_node[0]
        # 找到该节点的一条入边并移除
        for src, targets in dag.adjacency.items():
            if node_id in targets:
                targets.remove(node_id)
                dag.in_degree[node_id] = max(0, dag.in_degree.get(node_id, 1) - 1)
                if node_id in dag.nodes:
                    dag.nodes[node_id].depends_on = [
                        d for d in dag.nodes[node_id].depends_on if d != src
                    ]
                logger.info("[DAG修复] 移除边 %s -> %s", src, node_id)
                break
        else:
            return


async def create_tasks_from_plan(
    db: AsyncSession, *,
    session_id: int,
    plan_tasks: list[dict],
    agents: list,
    agent_roles: dict[int, str] | None = None,
) -> list[dict]:
    """v2.5: 从 Leader 决策的任务列表创建 DB 任务 + DAG 边。

    返回 [{task_id, title, assigned_agent_id, ...}]
    """
    task_id_map: dict[int, int] = {}  # 序号 -> DB id

    for idx, t in enumerate(plan_tasks, start=1):
        assignee_role = t.get("assignee_role") or t.get("role") or t.get("assignee") or ""
        assignee = _match_agent(agents, assignee_role, agent_roles)
        task = await task_service.create_task(
            db,
            session_id=session_id,
            title=_to_str(t.get("title"), f"任务{idx}"),
            description=_to_str(t.get("description")),
            acceptance_criteria=_to_str(t.get("acceptance_criteria")),
            assigned_agent_id=assignee.id if assignee else None,
            status="pending",
            priority=t.get("priority", 0),
            needs_review=bool(t.get("needs_review", False)),
        )
        task_id_map[idx] = task.id

    # 建依赖边
    for idx, t in enumerate(plan_tasks, start=1):
        for dep_idx in (t.get("depends_on") or t.get("dependencies") or t.get("deps") or []):
            if dep_idx in task_id_map and idx in task_id_map:
                await task_service.add_edge(
                    db,
                    session_id=session_id,
                    from_id=task_id_map[dep_idx],
                    to_id=task_id_map[idx],
                )

    # 返回带 assigned_agent_id 的列表
    out: list[dict] = []
    for i, t in enumerate(plan_tasks):
        role = t.get("assignee_role") or t.get("role") or t.get("assignee") or ""
        matched = _match_agent(agents, role, agent_roles)
        out.append({
            "task_id": task_id_map[i + 1],
            "assigned_agent_id": matched.id if matched else None,
            **t,
        })
    return out


async def decompose_requirement(
    db: AsyncSession, *, session_id: int, requirement: str,
    context_messages: list[str] | None = None,
) -> dict:
    """Leader 调用 LLM 拆解需求，构建任务 DAG 并持久化。

    v2.3: context_messages 传入最近群聊动态,避免 LLM 对"重新跟进"等短消息凭空拆解。

    返回: {"understanding": str, "tasks": [...], "dag": {...}}
    """
    # 1. 取会话与团队成员
    session = await session_service.get_session(db, session_id)
    if not session or not session.team_id:
        raise ValueError("session or team not found")

    agents = await team_service.list_team_agents(db, session.team_id)
    template_ids = {a.template_id for a in agents if a.template_id}
    tmpl_map: dict[int, AgentTemplate] = {}
    if template_ids:
        res = await db.execute(select(AgentTemplate).where(AgentTemplate.id.in_(template_ids)))
        tmpl_map = {t.id: t for t in res.scalars()}
    agent_roles = {
        a.id: (tmpl_map[a.template_id].role if a.template_id and tmpl_map.get(a.template_id) else "engineer")
        for a in agents
    }
    team_members = [{"id": a.id, "name": a.name, "role": agent_roles[a.id]} for a in agents]

    # 2. 调用 LLM 拆解
    provider = get_model_registry().get_default_provider()
    if not provider:
        # 无默认模型时返回占位（MVP 降级：手动拆解）
        plan = _fallback_plan(requirement, team_members)
    else:
        # v3.5: 不再限制参数，全面释放模型能力
        prompt = build_decompose_prompt(requirement, team_members, context_messages=context_messages)
        request = ChatRequest(
            messages=[
                ChatMessage(role="system", content=LEADER_SYSTEM_PROMPT),
                ChatMessage(role="user", content=prompt),
            ],
            model="",
        )
        response = await provider.chat(request)
        plan = _parse_plan(response.content)

    # 3. 持久化任务与 DAG 边
    task_id_map: dict[int, int] = {}  # 序号 -> DB id
    dag = TaskDAG(session_id=session_id)

    for idx, t in enumerate(plan.get("tasks", []), start=1):
        # 按 role 匹配 agent
        assignee_role = t.get("assignee_role") or t.get("role") or t.get("assignee") or ""
        assignee = _match_agent(agents, assignee_role, agent_roles)
        task = await task_service.create_task(
            db,
            session_id=session_id,
            title=_to_str(t.get("title"), f"任务{idx}"),
            description=_to_str(t.get("description")),
            acceptance_criteria=_to_str(t.get("acceptance_criteria")),
            assigned_agent_id=assignee.id if assignee else None,
            status="pending",
            priority=t.get("priority", 0),
        )
        task_id_map[idx] = task.id
        dag.add_node(
            TaskNode(
                task_id=task.id,
                title=task.title,
                assignee_id=task.assigned_agent_id,
                depends_on=[],  # 边单独建
                status="pending",
            )
        )

    # 建依赖边
    for idx, t in enumerate(plan.get("tasks", []), start=1):
        for dep_idx in (t.get("depends_on") or t.get("dependencies") or t.get("deps") or []):
            if dep_idx in task_id_map and idx in task_id_map:
                await task_service.add_edge(
                    db,
                    session_id=session_id,
                    from_id=task_id_map[dep_idx],
                    to_id=task_id_map[idx],
                )
                dag.add_edge(task_id_map[dep_idx], task_id_map[idx])

    # v1.0: DAG 环检测 + 自动修复
    if dag.has_cycle():
        logger.warning("会话 %s DAG 检测到环，尝试自动修复", session_id)
        # 修复策略: 移除环中入度最大的节点的入边
        _repair_dag_cycle(dag)
        if dag.has_cycle():
            logger.error("会话 %s DAG 环修复失败，任务可能永久 pending", session_id)

    # 4. 在群聊发一条任务卡片消息
    await session_service.create_message(
        db,
        session_id=session_id,
        sender_type=SenderType.AGENT,
        sender_id=None,
        msg_type=MsgType.TASK_CARD,
        content={
            "understanding": plan.get("understanding", ""),
            "tasks": [
                {"task_id": task_id_map[i + 1], **t}
                for i, t in enumerate(plan.get("tasks", []))
            ],
            "awaiting_confirmation": True,
        },
    )

    # v0.3: 计划确认门 — 默认硬门,除非全局配置 auto_confirm_plan
    from app.core.config import settings
    session.plan_confirmed = settings.auto_confirm_plan

    # v0.9: 返回的 task 携带 assigned_agent_id,供派活话术准确 @ 真实成员
    out_tasks: list[dict] = []
    for i, t in enumerate(plan.get("tasks", [])):
        role = t.get("assignee_role") or t.get("role") or t.get("assignee") or ""
        matched = _match_agent(agents, role, agent_roles)
        out_tasks.append({
            "task_id": task_id_map[i + 1],
            "assigned_agent_id": matched.id if matched else None,
            **t,
        })

    return {
        "understanding": plan.get("understanding", ""),
        "tasks": out_tasks,
        "parallel_layers": dag.parallel_layers(),
        "plan_confirmed": session.plan_confirmed,
        "awaiting_confirmation": not session.plan_confirmed,
    }


def _to_str(val: object, default: str = "") -> str:
    """把 LLM 可能返回的 list/dict 统一转成字符串。"""
    if val is None:
        return default
    if isinstance(val, (list, dict)):
        try:
            return json.dumps(val, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(val)
    return str(val)


def _parse_plan(content: str) -> dict:
    """从 LLM 输出中解析 JSON（兼容 markdown 代码块包裹）。"""
    # 尝试提取 ```json ... ```
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    raw = match.group(1) if match else content
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"understanding": content, "tasks": []}


_ROLE_KEYWORDS: dict[str, list[str]] = {
    "pm": ["产品", "leader", "pm", "经理"],
    "leader": ["产品", "leader", "pm", "经理"],
    "architect": ["架构", "architect"],
    "frontend": ["前端", "frontend", "front-end"],
    "backend": ["后端", "backend", "back-end"],
    "qa": ["测试", "qa", "test"],
    "reviewer": ["审查", "review"],
    "ui_designer": ["设计", "ui", "ux", "design"],
    "fullstack": ["全栈", "fullstack", "full-stack"],
    "devops": ["devops", "运维"],
    "data_scientist": ["数据", "data"],
}


def _match_agent(agents, role: str, agent_roles: dict[int, str] | None = None):
    """按 role 匹配 agent：先按 template role 精确匹配，再按关键词模糊匹配 name。"""
    if not agents:
        return None
    if not role:
        return agents[0]
    role_lower = role.lower().strip()

    # 1. 按 template role 精确匹配
    if agent_roles:
        for a in agents:
            if (agent_roles.get(a.id) or "").lower() == role_lower:
                return a

    # 2. 按 role 别名关键词匹配 agent name
    keywords = _ROLE_KEYWORDS.get(role_lower) or [role_lower]
    for kw in keywords:
        for a in agents:
            if kw in a.name.lower():
                return a

    # 3. role 原文直接匹配 name
    for a in agents:
        if role_lower in a.name.lower():
            return a

    return agents[0]


def _fallback_plan(requirement: str, members: list[dict]) -> dict:
    """无 LLM 时的降级拆解。"""
    return {
        "understanding": f"降级模式：已收到需求「{requirement}」，请用户手动确认任务拆解。",
        "tasks": [
            {
                "title": "需求分析与设计",
                "description": requirement,
                "acceptance_criteria": "产出需求文档",
                "assignee_role": "pm",
                "depends_on": [],
                "priority": 1,
            },
            {
                "title": "实现功能",
                "description": requirement,
                "acceptance_criteria": "代码可运行",
                "assignee_role": "backend",
                "depends_on": [1],
                "priority": 2,
            },
            {
                "title": "测试验证",
                "description": "验证功能正确性",
                "acceptance_criteria": "测试通过",
                "assignee_role": "qa",
                "depends_on": [2],
                "priority": 3,
            },
        ],
    }

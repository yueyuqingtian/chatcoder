"""v3.6: Skills & MCP Service —— 技能和 MCP Server 的 CRUD + 扫描同步。

功能：
- 创建/查询/更新/删除 Skill
- 创建/查询/更新/删除 McpServer
- 扫描外部工具（Codex/CodeBuddy/Qoder/Trae）的技能和 MCP 配置，同步入库
- Agent 绑定/解绑 Skill 和 MCP Server
- 查询 Agent 可用的 Skill 和 MCP Server 列表
"""
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orchestration.skill_scanner import (
    ScannedMcpServer,
    ScannedSkill,
    fetch_mcp_tools,
    scan_all_mcp_servers,
    scan_all_skills,
)
from app.persistence.models.agent import Agent
from app.persistence.models.skill import McpServer, Skill

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────
# Skill CRUD
# ───────────────────────────────────────────────────────────────────

async def create_skill(
    db: AsyncSession, *, name: str, display_name: str | None = None,
    description: str | None = None, content: str | None = None,
    source: str = "custom", path: str | None = None,
    trigger: str | None = None, tools: list[str] | None = None,
    tags: list[str] | None = None, is_active: bool = True,
    auto_load: bool = True, meta: dict | None = None,
) -> Skill:
    """创建技能。"""
    skill = Skill(
        name=name,
        display_name=display_name or name,
        description=description,
        content=content,
        source=source,
        path=path,
        trigger=trigger,
        tools=tools,
        tags=tags,
        is_active=is_active,
        auto_load=auto_load,
        meta=meta,
    )
    db.add(skill)
    await db.flush()
    return skill


async def list_skills(db: AsyncSession, source: str | None = None) -> list[Skill]:
    """列出所有技能，可按来源筛选。"""
    stmt = select(Skill).order_by(Skill.source, Skill.name)
    if source:
        stmt = stmt.where(Skill.source == source)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_skill(db: AsyncSession, skill_id: int) -> Skill | None:
    return await db.get(Skill, skill_id)


async def get_skill_by_name(db: AsyncSession, name: str) -> Skill | None:
    res = await db.execute(select(Skill).where(Skill.name == name))
    return res.scalars().first()


async def update_skill(db: AsyncSession, skill_id: int, **kwargs: Any) -> Skill | None:
    """更新技能字段。"""
    skill = await db.get(Skill, skill_id)
    if skill is None:
        return None
    for key, val in kwargs.items():
        if val is not None and hasattr(skill, key):
            setattr(skill, key, val)
    await db.flush()
    return skill


async def delete_skill(db: AsyncSession, skill_id: int) -> bool:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        return False
    await db.delete(skill)
    await db.flush()
    return True


# ───────────────────────────────────────────────────────────────────
# MCP Server CRUD
# ───────────────────────────────────────────────────────────────────

async def create_mcp_server(
    db: AsyncSession, *, name: str, display_name: str | None = None,
    description: str | None = None, source: str = "custom",
    transport: str = "stdio", command: str | None = None,
    args: list | None = None, env: dict | None = None,
    url: str | None = None, tools: list | None = None,
    is_active: bool = True, path: str | None = None,
    meta: dict | None = None,
) -> McpServer:
    """创建 MCP Server 配置。"""
    srv = McpServer(
        name=name,
        display_name=display_name or name,
        description=description,
        source=source,
        transport=transport,
        command=command,
        args=args,
        env=env,
        url=url,
        tools=tools,
        is_active=is_active,
        path=path,
        meta=meta,
    )
    db.add(srv)
    await db.flush()
    return srv


async def list_mcp_servers(db: AsyncSession, source: str | None = None) -> list[McpServer]:
    """列出所有 MCP Server，可按来源筛选。"""
    stmt = select(McpServer).order_by(McpServer.source, McpServer.name)
    if source:
        stmt = stmt.where(McpServer.source == source)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_mcp_server(db: AsyncSession, server_id: int) -> McpServer | None:
    return await db.get(McpServer, server_id)


async def update_mcp_server(db: AsyncSession, server_id: int, **kwargs: Any) -> McpServer | None:
    srv = await db.get(McpServer, server_id)
    if srv is None:
        return None
    for key, val in kwargs.items():
        if val is not None and hasattr(srv, key):
            setattr(srv, key, val)
    await db.flush()
    return srv


async def delete_mcp_server(db: AsyncSession, server_id: int) -> bool:
    srv = await db.get(McpServer, server_id)
    if srv is None:
        return False
    await db.delete(srv)
    await db.flush()
    return True


# ───────────────────────────────────────────────────────────────────
# 扫描同步
# ───────────────────────────────────────────────────────────────────

async def sync_scanned_skills(
    db: AsyncSession, workspace_root: str | None = None,
) -> dict:
    """扫描外部工具的技能文件，同步到数据库。

    - 新发现的技能：创建记录
    - 已存在的技能（按 name 匹配）：更新 content/path
    - 数据库中手动创建的技能（source=custom）：不会被覆盖

    Returns:
        {"added": int, "updated": int, "unchanged": int, "total_scanned": int}
    """
    scanned = scan_all_skills(workspace_root)
    added = 0
    updated = 0
    unchanged = 0

    for item in scanned:
        existing = await get_skill_by_name(db, item.name)
        if existing is None:
            # 新增
            await create_skill(
                db,
                name=item.name,
                display_name=item.display_name,
                description=item.description,
                content=item.content,
                source=item.source,
                path=item.path,
                trigger=item.trigger,
                tools=item.tools,
                tags=item.tags,
                meta=item.meta,
            )
            added += 1
        elif existing.source != "custom":
            # 更新（非 custom 来源才覆盖）
            changed = False
            if existing.content != item.content:
                existing.content = item.content
                changed = True
            if existing.path != item.path:
                existing.path = item.path
                changed = True
            if existing.display_name != item.display_name:
                existing.display_name = item.display_name
                changed = True
            if changed:
                updated += 1
            else:
                unchanged += 1
        else:
            unchanged += 1

    await db.flush()
    logger.info(
        "技能扫描同步完成: 扫描=%d 新增=%d 更新=%d 未变=%d",
        len(scanned), added, updated, unchanged,
    )
    return {
        "added": added, "updated": updated,
        "unchanged": unchanged, "total_scanned": len(scanned),
    }


async def sync_scanned_mcp_servers(
    db: AsyncSession, workspace_root: str | None = None,
) -> dict:
    """扫描外部工具的 MCP 配置，同步到数据库。

    Returns:
        {"added": int, "updated": int, "unchanged": int, "total_scanned": int}
    """
    scanned = scan_all_mcp_servers(workspace_root)
    added = 0
    updated = 0
    unchanged = 0

    for item in scanned:
        # 按 name 查找已有记录
        res = await db.execute(select(McpServer).where(McpServer.name == item.name))
        existing = res.scalars().first()

        if existing is None:
            # v4.8: 扫描时获取 tools/list 填充到数据库
            tools = await fetch_mcp_tools(item.command, item.args, item.env) if item.command else []
            await create_mcp_server(
                db,
                name=item.name,
                display_name=item.display_name,
                description=item.description,
                source=item.source,
                transport=item.transport,
                command=item.command,
                args=item.args,
                env=item.env,
                url=item.url,
                tools=tools,
                path=item.path,
                meta=item.meta,
            )
            added += 1
        elif existing.source != "custom":
            changed = False
            if existing.command != item.command:
                existing.command = item.command
                changed = True
            if existing.args != item.args:
                existing.args = item.args
                changed = True
            if existing.env != item.env:
                existing.env = item.env
                changed = True
            if existing.url != item.url:
                existing.url = item.url
                changed = True
            if changed:
                updated += 1
            else:
                unchanged += 1
        else:
            unchanged += 1

    await db.flush()
    logger.info(
        "MCP 扫描同步完成: 扫描=%d 新增=%d 更新=%d 未变=%d",
        len(scanned), added, updated, unchanged,
    )
    return {
        "added": added, "updated": updated,
        "unchanged": unchanged, "total_scanned": len(scanned),
    }


# ───────────────────────────────────────────────────────────────────
# Agent 绑定
# ───────────────────────────────────────────────────────────────────

async def bind_agent_skills(
    db: AsyncSession, agent_id: int, skill_ids: list[int],
) -> Agent | None:
    """设置 Agent 绑定的技能列表。"""
    agent = await db.get(Agent, agent_id)
    if agent is None:
        return None
    agent.skill_ids = skill_ids
    await db.flush()
    return agent


async def bind_agent_mcp_servers(
    db: AsyncSession, agent_id: int, mcp_server_ids: list[int],
) -> Agent | None:
    """设置 Agent 绑定的 MCP Server 列表。"""
    agent = await db.get(Agent, agent_id)
    if agent is None:
        return None
    agent.mcp_server_ids = mcp_server_ids
    await db.flush()
    return agent


async def get_agent_skills(db: AsyncSession, agent: Agent) -> list[Skill]:
    """获取 Agent 绑定的（且激活的）技能列表。"""
    skill_ids = agent.skill_ids or []
    if not skill_ids:
        return []
    res = await db.execute(
        select(Skill).where(Skill.id.in_(skill_ids), Skill.is_active == True)  # noqa: E712
    )
    return list(res.scalars().all())


async def get_agent_mcp_servers(db: AsyncSession, agent: Agent) -> list[McpServer]:
    """获取 Agent 生效的 MCP Server 列表。"""
    bound_ids = set(agent.mcp_server_ids or [])
    res = await db.execute(
        select(McpServer).where(
            (McpServer.is_active == True) | (McpServer.id.in_(bound_ids))  # noqa: E712
        )
    )
    servers = list(res.scalars().all())
    seen: set[int] = set()
    out: list[McpServer] = []
    for s in servers:
        if s.id in seen:
            continue
        seen.add(s.id)
        out.append(s)
    return out


# v2: 全局技能/MCP（context_manager 注入用）
async def get_global_skills(db: AsyncSession) -> list[Skill]:
    """全局激活技能（auto_load 或 is_active）。"""
    res = await db.execute(
        select(Skill).where(Skill.is_active == True, Skill.auto_load == True)  # noqa: E712
    )
    return list(res.scalars().all())


async def get_global_mcp_servers(db: AsyncSession) -> list[McpServer]:
    """全局激活 MCP Server。"""
    res = await db.execute(
        select(McpServer).where(McpServer.is_active == True)  # noqa: E712
    )
    return list(res.scalars().all())

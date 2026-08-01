"""v3.6: Skills & MCP Server 管理 API。

支持：
- 技能 CRUD + 扫描同步
- MCP Server CRUD + 扫描同步
- Agent 绑定技能和 MCP Server
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.gateway.schemas import (
    AgentBindingsUpdate,
    McpServerCreate,
    McpServerOut,
    McpServerUpdate,
    ScanResult,
    SkillCreate,
    SkillOut,
    SkillUpdate,
)
from app.persistence.database import get_db
from app.services import skill_service

router = APIRouter()


def _skill_to_out(s) -> SkillOut:
    return SkillOut(
        id=s.id,
        name=s.name,
        display_name=s.display_name,
        description=s.description,
        source=s.source,
        path=s.path,
        content=s.content,
        trigger=s.trigger,
        tools=s.tools,
        tags=s.tags,
        is_active=s.is_active,
        auto_load=s.auto_load,
    )


def _mcp_to_out(m) -> McpServerOut:
    return McpServerOut(
        id=m.id,
        name=m.name,
        display_name=m.display_name,
        description=m.description,
        source=m.source,
        transport=m.transport,
        command=m.command,
        args=m.args,
        env=m.env,
        url=m.url,
        tools=m.tools,
        is_active=m.is_active,
    )


# ───────────────────────────────────────────────────────────────────
# Skills API
# ───────────────────────────────────────────────────────────────────

@router.post("/skills", response_model=SkillOut)
async def create_skill(body: SkillCreate, db: AsyncSession = Depends(get_db)):
    skill = await skill_service.create_skill(
        db,
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        content=body.content,
        source=body.source,
        trigger=body.trigger,
        tools=body.tools,
        tags=body.tags,
        auto_load=body.auto_load,
    )
    await db.commit()
    return _skill_to_out(skill)


@router.get("/skills", response_model=list[SkillOut])
async def list_skills(
    source: str | None = Query(None, description="按来源筛选"),
    db: AsyncSession = Depends(get_db),
):
    skills = await skill_service.list_skills(db, source=source)
    return [_skill_to_out(s) for s in skills]


@router.get("/skills/{skill_id}", response_model=SkillOut)
async def get_skill(skill_id: int, db: AsyncSession = Depends(get_db)):
    skill = await skill_service.get_skill(db, skill_id)
    if skill is None:
        raise HTTPException(404, "skill not found")
    return _skill_to_out(skill)


@router.put("/skills/{skill_id}", response_model=SkillOut)
async def update_skill(skill_id: int, body: SkillUpdate, db: AsyncSession = Depends(get_db)):
    skill = await skill_service.update_skill(
        db, skill_id,
        display_name=body.display_name,
        description=body.description,
        content=body.content,
        trigger=body.trigger,
        tools=body.tools,
        tags=body.tags,
        is_active=body.is_active,
        auto_load=body.auto_load,
    )
    if skill is None:
        raise HTTPException(404, "skill not found")
    await db.commit()
    return _skill_to_out(skill)


@router.delete("/skills/{skill_id}")
async def delete_skill(skill_id: int, db: AsyncSession = Depends(get_db)):
    ok = await skill_service.delete_skill(db, skill_id)
    if not ok:
        raise HTTPException(404, "skill not found")
    await db.commit()
    return {"ok": True}


@router.post("/skills/scan", response_model=ScanResult)
async def scan_skills(db: AsyncSession = Depends(get_db)):
    """扫描外部工具（Codex/CodeBuddy/Qoder/Trae）的技能文件，同步入库。"""
    result = await skill_service.sync_scanned_skills(db, settings.workspace_root)
    await db.commit()
    return ScanResult(**result)


# ───────────────────────────────────────────────────────────────────
# MCP Server API
# ───────────────────────────────────────────────────────────────────

@router.post("/mcp-servers", response_model=McpServerOut)
async def create_mcp_server(body: McpServerCreate, db: AsyncSession = Depends(get_db)):
    srv = await skill_service.create_mcp_server(
        db,
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        source=body.source,
        transport=body.transport,
        command=body.command,
        args=body.args,
        env=body.env,
        url=body.url,
        is_active=body.is_active,
    )
    await db.commit()
    return _mcp_to_out(srv)


@router.get("/mcp-servers", response_model=list[McpServerOut])
async def list_mcp_servers(
    source: str | None = Query(None, description="按来源筛选"),
    db: AsyncSession = Depends(get_db),
):
    servers = await skill_service.list_mcp_servers(db, source=source)
    return [_mcp_to_out(m) for m in servers]


@router.get("/mcp-servers/{server_id}", response_model=McpServerOut)
async def get_mcp_server(server_id: int, db: AsyncSession = Depends(get_db)):
    srv = await skill_service.get_mcp_server(db, server_id)
    if srv is None:
        raise HTTPException(404, "mcp server not found")
    return _mcp_to_out(srv)


@router.put("/mcp-servers/{server_id}", response_model=McpServerOut)
async def update_mcp_server(server_id: int, body: McpServerUpdate, db: AsyncSession = Depends(get_db)):
    srv = await skill_service.update_mcp_server(
        db, server_id,
        display_name=body.display_name,
        description=body.description,
        transport=body.transport,
        command=body.command,
        args=body.args,
        env=body.env,
        url=body.url,
        is_active=body.is_active,
    )
    if srv is None:
        raise HTTPException(404, "mcp server not found")
    await db.commit()
    return _mcp_to_out(srv)


@router.delete("/mcp-servers/{server_id}")
async def delete_mcp_server(server_id: int, db: AsyncSession = Depends(get_db)):
    ok = await skill_service.delete_mcp_server(db, server_id)
    if not ok:
        raise HTTPException(404, "mcp server not found")
    await db.commit()
    return {"ok": True}


@router.post("/mcp-servers/scan", response_model=ScanResult)
async def scan_mcp_servers(db: AsyncSession = Depends(get_db)):
    """扫描外部工具的 MCP 配置文件，同步入库。"""
    result = await skill_service.sync_scanned_mcp_servers(db, settings.workspace_root)
    await db.commit()
    return ScanResult(**result)


# ───────────────────────────────────────────────────────────────────
# Agent 绑定 API
# ───────────────────────────────────────────────────────────────────

@router.get("/agents/{agent_id}/skills", response_model=list[SkillOut])
async def get_agent_skills_api(agent_id: int, db: AsyncSession = Depends(get_db)):
    """获取 Agent 绑定的技能列表。"""
    from app.persistence.models.agent import Agent
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    skills = await skill_service.get_agent_skills(db, agent)
    return [_skill_to_out(s) for s in skills]


@router.get("/agents/{agent_id}/mcp-servers", response_model=list[McpServerOut])
async def get_agent_mcp_servers_api(agent_id: int, db: AsyncSession = Depends(get_db)):
    """获取 Agent 绑定的 MCP Server 列表。"""
    from app.persistence.models.agent import Agent
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    servers = await skill_service.get_agent_mcp_servers(db, agent)
    return [_mcp_to_out(m) for m in servers]


@router.put("/agents/{agent_id}/bindings")
async def update_agent_bindings(
    agent_id: int, body: AgentBindingsUpdate, db: AsyncSession = Depends(get_db),
):
    """更新 Agent 绑定的技能和 MCP Server。

    - skill_ids / mcp_server_ids 为完整列表（覆盖式更新）
    - 传 None 或不传时表示不更新该项
    """
    from app.persistence.models.agent import Agent
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")

    if body.skill_ids is not None:
        agent = await skill_service.bind_agent_skills(db, agent_id, body.skill_ids)
    if body.mcp_server_ids is not None:
        agent = await skill_service.bind_agent_mcp_servers(db, agent_id, body.mcp_server_ids)

    await db.commit()
    return {
        "ok": True,
        "skill_ids": agent.skill_ids or [],
        "mcp_server_ids": agent.mcp_server_ids or [],
    }

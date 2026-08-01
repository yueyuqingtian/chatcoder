"""技能（Skills）与 MCP Server 路由（v2）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import skill_service

router = APIRouter()
from app.services.mcp_scan import scan_local_mcp


def skill_to_dict(s) -> dict:
    return {
        "id": s.id, "name": s.name, "display_name": s.display_name,
        "description": s.description, "source": s.source, "path": s.path,
        "content": s.content, "trigger": s.trigger, "tools": s.tools,
        "tags": s.tags, "is_active": s.is_active, "auto_load": s.auto_load,
    }


def mcp_to_dict(m) -> dict:
    return {
        "id": m.id, "name": m.name, "display_name": m.display_name,
        "description": m.description, "source": m.source, "transport": m.transport,
        "command": m.command, "args": m.args, "env": m.env, "url": m.url,
        "tools": m.tools, "is_active": m.is_active,
    }


class SkillCreate(BaseModel):
    name: str
    display_name: str | None = None
    description: str | None = None
    content: str | None = None
    source: str = "manual"
    trigger: str | None = None
    tools: list[str] | None = None
    tags: list[str] | None = None
    auto_load: bool = False


class SkillUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    content: str | None = None
    trigger: str | None = None
    tools: list[str] | None = None
    tags: list[str] | None = None
    is_active: bool | None = None
    auto_load: bool | None = None


class McpCreate(BaseModel):
    name: str
    display_name: str | None = None
    description: str | None = None
    source: str = "manual"
    transport: str = "stdio"
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    is_active: bool = True


class McpUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    is_active: bool | None = None


@router.get("/skills", response_model=list[dict])
async def list_skills(source: str | None = None, db: AsyncSession = Depends(get_db)):
    return [skill_to_dict(s) for s in await skill_service.list_skills(db, source)]


@router.post("/skills", response_model=dict)
async def create_skill(body: SkillCreate, db: AsyncSession = Depends(get_db)):
    skill = await skill_service.create_skill(
        db, name=body.name, display_name=body.display_name,
        description=body.description, content=body.content, source=body.source,
        trigger=body.trigger, tools=body.tools, tags=body.tags, auto_load=body.auto_load,
    )
    await db.commit()
    return skill_to_dict(skill)


@router.patch("/skills/{skill_id}", response_model=dict)
async def update_skill(skill_id: int, body: SkillUpdate, db: AsyncSession = Depends(get_db)):
    skill = await skill_service.update_skill(
        db, skill_id,
        display_name=body.display_name, description=body.description,
        content=body.content, trigger=body.trigger, tools=body.tools,
        tags=body.tags, is_active=body.is_active, auto_load=body.auto_load,
    )
    if skill is None:
        raise HTTPException(404, "skill not found")
    await db.commit()
    return skill_to_dict(skill)


@router.delete("/skills/{skill_id}", response_model=dict)
async def delete_skill(skill_id: int, db: AsyncSession = Depends(get_db)):
    ok = await skill_service.delete_skill(db, skill_id)
    if not ok:
        raise HTTPException(404, "skill not found")
    await db.commit()
    return {"ok": True}


@router.get("/mcp-servers", response_model=list[dict])
async def list_mcp(source: str | None = None, db: AsyncSession = Depends(get_db)):
    return [mcp_to_dict(m) for m in await skill_service.list_mcp_servers(db, source)]


@router.post("/mcp-servers", response_model=dict)
async def create_mcp(body: McpCreate, db: AsyncSession = Depends(get_db)):
    server = await skill_service.create_mcp_server(
        db, name=body.name, display_name=body.display_name,
        description=body.description, source=body.source, transport=body.transport,
        command=body.command, args=body.args, env=body.env, url=body.url,
        is_active=body.is_active,
    )
    await db.commit()
    return mcp_to_dict(server)


@router.patch("/mcp-servers/{server_id}", response_model=dict)
async def update_mcp(server_id: int, body: McpUpdate, db: AsyncSession = Depends(get_db)):
    server = await skill_service.update_mcp_server(
        db, server_id,
        display_name=body.display_name, description=body.description,
        transport=body.transport, command=body.command, args=body.args,
        env=body.env, url=body.url, is_active=body.is_active,
    )
    if server is None:
        raise HTTPException(404, "mcp server not found")
    await db.commit()
    return mcp_to_dict(server)


@router.delete("/mcp-servers/{server_id}", response_model=dict)
async def delete_mcp(server_id: int, db: AsyncSession = Depends(get_db)):
    ok = await skill_service.delete_mcp_server(db, server_id)
    if not ok:
        raise HTTPException(404, "mcp server not found")
    await db.commit()
    return {"ok": True}


@router.post("/mcp-servers/scan", response_model=list[dict])
async def scan_mcp_servers():
    """扫描本机常见 MCP 客户端配置，返回候选 server 列表（不落库）。"""
    return await scan_local_mcp()

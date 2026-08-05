"""技能（Skills）、MCP Server 与技能仓库路由。"""
import json
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import skill_service

router = APIRouter()
from app.services.mcp_scan import scan_local_mcp

# 技能仓库配置持久化到用户 config.json
_REPO_CFG_PATH = Path(
    os.environ.get("CHATCODER_USER_CONFIG", str(Path.home() / ".chatcoder" / "config.json"))
)
_REPO_ROOT = Path(os.environ.get("CHATCODER_SKILL_REPO_DIR", str(Path.home() / ".chatcoder" / "skill-repos")))


def _load_repos() -> list[dict]:
    try:
        if _REPO_CFG_PATH.exists():
            data = json.loads(_REPO_CFG_PATH.read_text(encoding="utf-8"))
            repos = data.get("skill_repos", [])
            return repos if isinstance(repos, list) else []
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _save_repos(repos: list[dict]) -> None:
    try:
        _REPO_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(_REPO_CFG_PATH.read_text(encoding="utf-8")) if _REPO_CFG_PATH.exists() else {}
        data["skill_repos"] = repos
        _REPO_CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


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


# ── 技能仓库（第15点：云端 git url 技能仓库）──

class SkillRepoCreate(BaseModel):
    url: str
    name: str | None = None


class SkillRepoImport(BaseModel):
    repo_id: str
    skill_name: str


@router.get("/skills/repos", response_model=list[dict])
async def list_skill_repos():
    """列出已配置的技能仓库。"""
    repos = _load_repos()
    for r in repos:
        local = _REPO_ROOT / str(r["id"])
        r["synced"] = local.is_dir()
        r["skill_count"] = len(list_repo_skills(str(local))) if local.is_dir() else 0
    return repos


@router.post("/skills/repos", response_model=dict)
async def create_skill_repo(body: SkillRepoCreate):
    """添加技能仓库（git url）。"""
    repos = _load_repos()
    for r in repos:
        if r.get("url") == body.url:
            raise HTTPException(400, "该仓库已添加")
    repo = {
        "id": uuid.uuid4().hex[:12],
        "name": body.name or body.url.rstrip("/").split("/")[-1],
        "url": body.url,
    }
    repos.append(repo)
    _save_repos(repos)
    return repo


@router.post("/skills/repos/{repo_id}/sync", response_model=dict)
async def sync_skill_repo(repo_id: str):
    """拉取仓库并列出其中的技能。"""
    repos = _load_repos()
    repo = next((r for r in repos if r.get("id") == repo_id), None)
    if repo is None:
        raise HTTPException(404, "仓库不存在")
    local = _REPO_ROOT / repo_id
    res = await clone_skill_repo(repo["url"], str(local))
    if not res["ok"]:
        raise HTTPException(400, res["error"])
    skills = list_repo_skills(str(local))
    return {"repo": {**repo, "synced": True, "skill_count": len(skills)}, "skills": skills}


@router.post("/skills/repos/import", response_model=dict)
async def import_repo_skill(body: SkillRepoImport, db: AsyncSession = Depends(get_db)):
    """导入仓库中的某个技能到本地技能库并启用。"""
    local = _REPO_ROOT / body.repo_id
    if not local.is_dir():
        raise HTTPException(404, "仓库未同步")
    skills = list_repo_skills(str(local))
    target = next((s for s in skills if s["name"] == body.skill_name), None)
    if target is None:
        raise HTTPException(404, "技能不存在")
    existing = await skill_service.get_skill_by_name(db, target["name"])
    if existing is not None:
        raise HTTPException(400, f"技能 {target['name']} 已存在")
    skill = await skill_service.create_skill(
        db, name=target["name"], display_name=target["display_name"],
        description=target["description"], content=target["content"],
        source="repo", path=target["path"], trigger=target["trigger"],
        tools=target["tools"], is_active=True, auto_load=True,
    )
    await db.commit()
    return {"ok": True, "id": skill.id}


@router.delete("/skills/repos/{repo_id}", response_model=dict)
async def delete_skill_repo(repo_id: str):
    """删除技能仓库配置及本地缓存。"""
    repos = _load_repos()
    _save_repos([r for r in repos if r.get("id") != repo_id])
    local = _REPO_ROOT / repo_id
    try:
        import shutil
        if local.is_dir():
            shutil.rmtree(str(local), ignore_errors=True)
    except Exception:
        pass
    return {"ok": True}

"""记忆路由（D8；plan-230-1144 M4.1 三层化）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import MemoryEntryOut
from app.persistence.database import get_db
from app.services import memory_service

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("", response_model=list[MemoryEntryOut])
async def list_memories(
    session_id: int | None = None,
    scope: str | None = None,
    project_id: int | None = None,
    include_candidate: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """列出记忆。可按 scope（session/project/global）与 project_id 过滤；
    include_candidate=false 时隐藏候选区（低置信未注入项）。"""
    return await memory_service.list_memories(
        db, session_id, scope=scope, project_id=project_id,
        include_candidate=include_candidate,
    )


class MemoryPromote(BaseModel):
    target_scope: str  # session / project / global
    project_id: int | None = None


@router.post("/{memory_id}/promote", response_model=dict)
async def promote_memory(memory_id: int, body: MemoryPromote, db: AsyncSession = Depends(get_db)):
    """提升/降级记忆作用域（会话 ↔ 项目 ↔ 全局）。"""
    try:
        ok = await memory_service.promote_memory(
            db, memory_id, target_scope=body.target_scope, project_id=body.project_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "记忆不存在")
    return {"ok": True}


@router.delete("/{memory_id}", response_model=dict)
async def delete_memory(memory_id: int, db: AsyncSession = Depends(get_db)):
    ok = await memory_service.delete_memory(db, memory_id)
    if not ok:
        raise HTTPException(404, "记忆不存在")
    return {"ok": True}


class MemoryUpdate(BaseModel):
    text: str | None = None
    kind: str | None = None


@router.patch("/{memory_id}", response_model=dict)
async def update_memory(memory_id: int, body: MemoryUpdate, db: AsyncSession = Depends(get_db)):
    """编辑记忆文本/类型（S8 / plan-41-197）。编辑即人工确认，候选标记随之清除。"""
    try:
        ok = await memory_service.update_memory(db, memory_id, text=body.text, kind=body.kind)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "记忆不存在")
    return {"ok": True}


@router.post("/consolidate", response_model=dict)
async def consolidate(session_id: int, project_id: int, db: AsyncSession = Depends(get_db)):
    """整合记忆 → 写 MEMORY.md。"""
    content = await memory_service.consolidate(db, session_id)
    path = ""
    if content:
        from app.services import project_service
        project = await project_service.get_project(db, project_id)
        if project:
            path = memory_service.write_memory_file(project.path, content)
    return {"ok": True, "path": path, "entries": len(content.splitlines()) if content else 0}

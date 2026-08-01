"""记忆路由（D8）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import MemoryEntryOut
from app.persistence.database import get_db
from app.services import memory_service

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("", response_model=list[MemoryEntryOut])
async def list_memories(session_id: int | None = None, db: AsyncSession = Depends(get_db)):
    return await memory_service.list_memories(db, session_id)


@router.delete("/{memory_id}", response_model=dict)
async def delete_memory(memory_id: int, db: AsyncSession = Depends(get_db)):
    ok = await memory_service.delete_memory(db, memory_id)
    if not ok:
        raise HTTPException(404, "记忆不存在")
    await db.commit()
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

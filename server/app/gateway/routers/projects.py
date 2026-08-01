"""项目（工作目录）路由。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import ProjectCreate, ProjectOut, ProjectUpdate
from app.orchestration.rules_loader import scan_rules_docs
from app.persistence.database import get_db
from app.services import project_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectOut)
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db)):
    try:
        project = await project_service.create_project(
            db, path=body.path, name=body.name,
            rules_docs=body.rules_docs, auto_scan_rules=body.auto_scan_rules,
        )
        await db.commit()
        return project
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("", response_model=list[ProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db)):
    return await project_service.list_projects(db)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, db: AsyncSession = Depends(get_db)):
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(project_id: int, body: ProjectUpdate, db: AsyncSession = Depends(get_db)):
    project = await project_service.update_project(
        db, project_id,
        name=body.name, rules_docs=body.rules_docs, auto_scan_rules=body.auto_scan_rules,
        pinned=body.pinned, archived=body.archived,
    )
    if project is None:
        raise HTTPException(404, "项目不存在")
    await db.commit()
    return project


@router.delete("/{project_id}", response_model=dict)
async def delete_project(project_id: int, db: AsyncSession = Depends(get_db)):
    ok = await project_service.delete_project(db, project_id)
    if not ok:
        raise HTTPException(404, "项目不存在")
    await db.commit()
    return {"ok": True}


@router.get("/{project_id}/scan-rules", response_model=list[str])
async def project_scan_rules(project_id: int, db: AsyncSession = Depends(get_db)):
    """扫描项目目录下的规范文档候选。"""
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return await scan_rules_docs(project.path)


@router.get("/{project_id}/read-file", response_model=dict)
async def project_read_file(project_id: int, path: str,
                            db: AsyncSession = Depends(get_db)):
    """读取项目内文件（供右侧文件预览面板）。仅允许项目路径内。"""
    import os
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    root = os.path.abspath(project.path)
    target = os.path.abspath(os.path.join(root, path.lstrip("/\\")))
    if not target.startswith(root + os.sep) and target != root:
        raise HTTPException(400, "路径越界")
    if not os.path.isfile(target):
        raise HTTPException(404, "文件不存在")
    size = os.path.getsize(target)
    MAX_BYTES = 512 * 1024
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(MAX_BYTES) if size > MAX_BYTES else f.read()
    except OSError as e:
        raise HTTPException(500, f"读取失败: {e}")
    ext = os.path.splitext(target)[1].lstrip(".").lower()
    return {"path": target.replace("\\", "/"), "content": content,
            "size": size, "truncated": size > MAX_BYTES, "language": ext or None}


@router.get("/{project_id}/tree", response_model=dict)
async def project_tree(project_id: int, depth: int = Query(2, ge=1, le=4),
                       db: AsyncSession = Depends(get_db)):
    """项目目录树（供右侧文件管理面板）。"""
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return _build_tree(project.path, max_depth=depth)


def _build_tree(root: str, max_depth: int) -> dict:
    import os

    def walk(path: str, depth: int) -> list[dict]:
        if depth > max_depth:
            return []
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return []
        out = []
        for e in entries[:200]:
            if e.name.startswith(".") or e.name == "node_modules":
                continue
            node = {"name": e.name, "type": "dir" if e.is_dir() else "file",
                    "path": e.path.replace("\\", "/")}
            if e.is_dir():
                node["children"] = walk(e.path, depth + 1)
            out.append(node)
        return out

    return {"path": root.replace("\\", "/"), "children": walk(root, 0)}

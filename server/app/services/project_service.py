"""项目（工作目录）CRUD 服务。"""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.project import Project


async def create_project(db: AsyncSession, *, path: str, name: str | None = None,
                         rules_docs: list[str] | None = None, auto_scan_rules: bool = True) -> Project:
    """创建项目。path 必须是存在的目录；name 默认取路径末段。"""
    p = Path(path)
    if not p.is_dir():
        raise ValueError(f"工作目录不存在: {path}")
    norm_path = str(p.resolve())
    if not name:
        name = p.name or norm_path
    project = Project(name=name, path=norm_path, rules_docs=rules_docs, auto_scan_rules=auto_scan_rules)
    db.add(project)
    await db.flush()
    return project


async def get_project(db: AsyncSession, project_id: int) -> Project | None:
    return await db.get(Project, project_id)


async def list_projects(db: AsyncSession, include_archived: bool = False) -> list[Project]:
    stmt = select(Project)
    if not include_archived:
        stmt = stmt.where(Project.archived == False)  # noqa: E712
    res = await db.execute(stmt.order_by(Project.pinned.desc(), Project.updated_at.desc()))
    return list(res.scalars().all())


async def update_project(db: AsyncSession, project_id: int, **kwargs) -> Project | None:
    project = await db.get(Project, project_id)
    if project is None:
        return None
    for k, v in kwargs.items():
        if v is not None:
            setattr(project, k, v)
    await db.flush()
    return project


async def delete_project(db: AsyncSession, project_id: int) -> bool:
    project = await db.get(Project, project_id)
    if project is None:
        return False
    await db.delete(project)
    await db.flush()
    return True


async def resolve_project_path(db: AsyncSession, project_id: int) -> str:
    """解析项目工作目录绝对路径。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise ValueError("project not found")
    return project.path

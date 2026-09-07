"""项目（工作目录）CRUD 服务。"""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.project import Project


async def create_project(db: AsyncSession, *, path: str, name: str | None = None,
                         rules_docs: list[str] | None = None, auto_scan_rules: bool = True) -> int:
    """创建项目（写引擎单写线程）。path 必须是存在的目录；name 默认取路径末段。
    返回项目 id。"""
    from app.persistence.database import run_write_locked

    p = Path(path)
    if not p.is_dir():
        raise ValueError(f"工作目录不存在: {path}")
    norm_path = str(p.resolve())
    if not name:
        name = p.name or norm_path

    def patch(s):
        project = Project(name=name, path=norm_path, rules_docs=rules_docs, auto_scan_rules=auto_scan_rules)
        s.add(project)
        s.flush()
        pid = project.id
        s.commit()
        return pid

    return await run_write_locked(patch, label="project.create")


async def get_project(db: AsyncSession, project_id: int) -> Project | None:
    return await db.get(Project, project_id)


async def list_projects(db: AsyncSession, include_archived: bool = False) -> list[Project]:
    stmt = select(Project)
    if not include_archived:
        stmt = stmt.where(Project.archived == False)  # noqa: E712
    res = await db.execute(stmt.order_by(Project.pinned.desc(), Project.updated_at.desc()))
    return list(res.scalars().all())


async def update_project(db: AsyncSession, project_id: int, **kwargs) -> str | None:
    """更新项目（写引擎单写线程）。返回 status/None。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        project = s.get(Project, project_id)
        if project is None:
            return None
        for k, v in kwargs.items():
            if v is not None:
                setattr(project, k, v)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"project.update.{project_id}")


async def delete_project(db: AsyncSession, project_id: int) -> bool:
    """删除项目（写引擎单写线程）。返回是否存在。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        project = s.get(Project, project_id)
        if project is None:
            return False
        s.delete(project)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"project.delete.{project_id}")


async def resolve_project_path(db: AsyncSession, project_id: int) -> str:
    """解析项目工作目录绝对路径。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise ValueError("project not found")
    return project.path

"""项目（工作目录）CRUD 服务。"""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.project import Project


class ProjectArchivedError(Exception):
    """plan-278-1391: 同路径项目已存在且处于归档状态。

    调用方（路由层）据此返回 409 + 结构化信息，前端提示用户
    「该项目已归档，是否恢复并打开」，而不是抛 500。
    """

    def __init__(self, project_id: int, name: str, path: str):
        super().__init__(f"项目已归档: {name}")
        self.project_id = project_id
        self.name = name
        self.path = path


async def find_by_path(db: AsyncSession, path: str) -> Project | None:
    """按规范化绝对路径查项目（不做 resolve 之外的变形）。"""
    p = Path(path)
    if not p.is_dir():
        return None
    norm_path = str(p.resolve())
    res = await db.execute(select(Project).where(Project.path == norm_path))
    return res.scalars().first()


async def create_project(db: AsyncSession, *, path: str, name: str | None = None,
                         rules_docs: list[str] | None = None, auto_scan_rules: bool = True) -> int:
    """创建项目（写引擎单写线程）。path 必须是存在的目录；name 默认取路径末段。
    返回项目 id。

    plan-278-1391: 幂等化——path 为 UNIQUE 列，直接 INSERT 在同路径第二次创建时
    会抛 IntegrityError（500）。现改为：
    - 已存在且未归档 → 直接返回既有项目 id（幂等，前端选中它）；
    - 已存在但已归档 → 抛 ProjectArchivedError（前端提示恢复归档项目）。
    """
    from app.persistence.database import run_write_locked

    p = Path(path)
    if not p.is_dir():
        raise ValueError(f"工作目录不存在: {path}")
    norm_path = str(p.resolve())
    if not name:
        name = p.name or norm_path

    # 查重（async 只读）——正常路径下命中率高，避免无谓写事务
    existing = await find_by_path(db, norm_path)
    if existing is not None:
        if getattr(existing, "archived", False):
            raise ProjectArchivedError(int(existing.id), str(existing.name or name), norm_path)
        return int(existing.id)

    def patch(s):
        # 双保险：写线程内再查一次，避免并发下两个请求同时通过上面的检查
        row = s.execute(select(Project).where(Project.path == norm_path)).scalars().first()
        if row is not None:
            if getattr(row, "archived", False):
                raise ProjectArchivedError(int(row.id), str(row.name or name), norm_path)
            return int(row.id)
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

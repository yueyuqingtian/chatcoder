"""任务服务（v2：子代理承载的工作项）。

plan-206-975：写操作（创建/状态/产物）经 WriteEngine 单写线程执行（对齐
deepseek-harness 单写者，无锁）；读操作保留 async 会话。`db` 参数保留兼容
（调用方仍传 async 会话），写函数内部不再使用 db 直接写入，权威读-改-写
在写线程内基于最新 DB 状态完成；返回值改为标量（id/status/count）。
"""
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.task import Artifact, Task


async def create_task(db: AsyncSession, *, session_id: int, title: str,
                      description: str | None = None, acceptance_criteria: str | None = None,
                      turn_id: int | None = None, agent_id: int | None = None,
                      parent_task_id: int | None = None, priority: int = 0,
                      kind: str = "request", depends_on: list[int] | None = None,
                      estimate: int | None = None, is_hidden: bool = False,
                      status: str = "pending") -> int:
    """创建任务（写线程内自给自足），返回 task id。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        task = Task(
            session_id=session_id, title=title, description=description,
            acceptance_criteria=acceptance_criteria, turn_id=turn_id,
            agent_id=agent_id, parent_task_id=parent_task_id, priority=priority,
            kind=kind, depends_on=depends_on, estimate=estimate, is_hidden=is_hidden,
            status=status,
        )
        s.add(task)
        s.flush()
        tid = task.id
        s.commit()
        return tid

    return await run_write_locked(patch, label="task.create")


async def get_task(db: AsyncSession, task_id: int) -> Task | None:
    return await db.get(Task, task_id)


async def list_tasks(db: AsyncSession, session_id: int, turn_id: int | None = None) -> list[Task]:
    stmt = select(Task).where(Task.session_id == session_id)
    if turn_id is not None:
        stmt = stmt.where(Task.turn_id == turn_id)
    res = await db.execute(stmt.order_by(Task.id.asc()))
    return list(res.scalars().all())


async def update_task_status(db: AsyncSession, task_id: int, status: str,
                             note: str | None = None) -> str | None:
    """更新任务状态（写线程内读-改-写，权威）。返回 status（None=未找到）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        task = s.get(Task, task_id)
        if task is None:
            return None
        task.status = status
        if note is not None:
            task.note = note
        s.commit()
        return task.status

    return await run_write_locked(patch, label=f"task.status.{task_id}")


async def attach_artifacts(db: AsyncSession, task_id: int, artifact_ids: list[int]) -> None:
    """将产物 id 列表挂到任务上（去重保序）。task 不存在时静默忽略（非阻塞）。"""
    if not artifact_ids:
        return
    from app.persistence.database import run_write_locked

    def patch(s):
        task = s.get(Task, task_id)
        if task is None:
            return
        merged = list(dict.fromkeys((task.artifact_ids or []) + list(artifact_ids)))
        task.artifact_ids = merged
        s.commit()

    await run_write_locked(patch, label=f"task.attach.{task_id}")


async def cancel_turn_tasks(db: AsyncSession, session_id: int, turn_id: int | None) -> int:
    """批量取消未完成任务（写线程内批量 UPDATE），返回影响行数。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        stmt = (
            update(Task)
            .where(
                Task.session_id == session_id,
                Task.status.in_(["proposed", "pending", "running", "in_progress"]),
            )
            .values(status="cancelled")
        )
        if turn_id is not None:
            stmt = stmt.where(Task.turn_id >= turn_id)
        result = s.execute(stmt)
        s.commit()
        return int(result.rowcount or 0)

    return await run_write_locked(patch, label="task.cancel_turn")


async def create_artifact(db: AsyncSession, *, task_id: int | None = None,
                          type: str | None = None, title: str | None = None,
                          storage_ref: str | None = None, summary: str | None = None,
                          files: list[str] | None = None,
                          git_baseline: str | None = None) -> int:
    """创建产物（写线程内自给自足），返回 artifact id。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        art = Artifact(
            task_id=task_id, type=type, title=title, storage_ref=storage_ref,
            summary=summary, files=files, git_baseline=git_baseline,
        )
        s.add(art)
        s.flush()
        aid = art.id
        s.commit()
        return aid

    return await run_write_locked(patch, label="artifact.create")


async def patch_task(task_id: int, **fields) -> None:
    """写引擎单写线程提交 Task 字段补丁（无锁单写者；status 之外 None 跳过）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        task = s.get(Task, task_id)
        if task is None:
            return
        for k, v in fields.items():
            if v is not None or k == "status":
                setattr(task, k, v)
        s.commit()

    await run_write_locked(patch, label=f"task.patch.{task_id}")


async def list_artifacts(db: AsyncSession, session_id: int) -> list[Artifact]:
    from app.persistence.models.task import Task
    res = await db.execute(
        select(Artifact).join(Task, Artifact.task_id == Task.id)
        .where(Task.session_id == session_id).order_by(Artifact.id.desc())
    )
    return list(res.scalars().all())

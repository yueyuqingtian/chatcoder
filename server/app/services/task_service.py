"""任务服务（v2：子代理承载的工作项）。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.task import Artifact, Task


async def create_task(db: AsyncSession, *, session_id: int, title: str,
                      description: str | None = None, acceptance_criteria: str | None = None,
                      turn_id: int | None = None, agent_id: int | None = None,
                      parent_task_id: int | None = None, priority: int = 0,
                      kind: str = "request", depends_on: list[int] | None = None,
                      estimate: int | None = None, is_hidden: bool = False,
                      status: str = "pending") -> Task:
    task = Task(
        session_id=session_id, title=title, description=description,
        acceptance_criteria=acceptance_criteria, turn_id=turn_id,
        agent_id=agent_id, parent_task_id=parent_task_id, priority=priority,
        kind=kind, depends_on=depends_on, estimate=estimate, is_hidden=is_hidden,
        status=status,
    )
    db.add(task)
    await db.flush()
    return task


async def get_task(db: AsyncSession, task_id: int) -> Task | None:
    return await db.get(Task, task_id)


async def list_tasks(db: AsyncSession, session_id: int, turn_id: int | None = None) -> list[Task]:
    stmt = select(Task).where(Task.session_id == session_id)
    if turn_id is not None:
        stmt = stmt.where(Task.turn_id == turn_id)
    res = await db.execute(stmt.order_by(Task.id.asc()))
    return list(res.scalars().all())


async def update_task_status(db: AsyncSession, task_id: int, status: str,
                             note: str | None = None) -> Task | None:
    task = await db.get(Task, task_id)
    if task is None:
        return None
    task.status = status
    if note is not None:
        task.note = note
    await db.flush()
    return task


async def attach_artifacts(db: AsyncSession, task_id: int, artifact_ids: list[int]) -> None:
    """将产物 id 列表挂到任务上（去重保序）。task 不存在时静默忽略（非阻塞）。"""
    if not artifact_ids:
        return
    task = await db.get(Task, task_id)
    if task is None:
        return
    merged = list(dict.fromkeys((task.artifact_ids or []) + list(artifact_ids)))
    task.artifact_ids = merged
    await db.flush()


async def cancel_turn_tasks(db: AsyncSession, session_id: int, turn_id: int) -> int:
    """回滚/取消时，将该 turn 之后运行中的任务置 cancelled。"""
    res = await db.execute(
        select(Task).where(
            Task.session_id == session_id,
            Task.status.in_(["proposed", "pending", "running"]),
        )
    )
    count = 0
    for t in res.scalars().all():
        if turn_id is None or (t.turn_id or 0) >= turn_id:
            t.status = "cancelled"
            count += 1
    return count


async def create_artifact(db: AsyncSession, *, task_id: int | None = None,
                          type: str | None = None, title: str | None = None,
                          storage_ref: str | None = None, summary: str | None = None,
                          files: list[str] | None = None,
                          git_baseline: str | None = None) -> Artifact:
    art = Artifact(
        task_id=task_id, type=type, title=title, storage_ref=storage_ref,
        summary=summary, files=files, git_baseline=git_baseline,
    )
    db.add(art)
    await db.flush()
    return art


async def list_artifacts(db: AsyncSession, session_id: int) -> list[Artifact]:
    from app.persistence.models.task import Task
    res = await db.execute(
        select(Artifact).join(Task, Artifact.task_id == Task.id)
        .where(Task.session_id == session_id).order_by(Artifact.id.desc())
    )
    return list(res.scalars().all())

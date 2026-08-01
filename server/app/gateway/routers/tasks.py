"""任务 API。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.gateway.schemas import ArtifactOut, TaskOut
from app.orchestration.tools.safe_path import safe_resolve
from app.persistence.database import get_db
from app.persistence.models.task import Artifact, Task
from app.services import task_service

router = APIRouter()


@router.get("/sessions/{session_id}/tasks", response_model=list[TaskOut])
async def list_tasks(session_id: int, db: AsyncSession = Depends(get_db)):
    tasks = await task_service.list_tasks(db, session_id)
    return [
        TaskOut(
            id=t.id,
            session_id=t.session_id,
            title=t.title,
            description=t.description,
            assigned_agent_id=t.assigned_agent_id,
            status=t.status,
            parent_task_id=t.parent_task_id,
            artifact_ids=t.artifact_ids,
            note=t.note,
        )
        for t in tasks
    ]


@router.get("/sessions/{session_id}/artifacts", response_model=list[ArtifactOut])
async def list_session_artifacts(session_id: int, db: AsyncSession = Depends(get_db)):
    """查询会话所有任务产出的成品(文件/代码块)。"""
    tasks = await task_service.list_tasks(db, session_id)
    artifact_id_set: set[int] = set()
    for t in tasks:
        if t.artifact_ids:
            artifact_id_set.update(t.artifact_ids)
    if not artifact_id_set:
        return []
    res = await db.execute(select(Artifact).where(Artifact.id.in_(artifact_id_set)).order_by(Artifact.id))
    return [
        ArtifactOut(
            id=a.id, task_id=a.task_id, type=a.type,
            title=a.title, storage_ref=a.storage_ref, summary=a.summary,
        )
        for a in res.scalars()
    ]


@router.put("/tasks/{task_id}/status")
async def update_status(task_id: int, status: str, db: AsyncSession = Depends(get_db)):
    task = await task_service.update_task_status(db, task_id, status)
    if not task:
        raise HTTPException(404, "task not found")
    await db.commit()
    return {"ok": True, "task_id": task_id, "status": status}


@router.delete("/sessions/{session_id}/tasks/completed")
async def clear_completed_tasks(session_id: int, db: AsyncSession = Depends(get_db)):
    """清理会话中所有已完成/已取消的任务及其关联的边和产物。"""
    count = await task_service.clear_completed_tasks(db, session_id)
    await db.commit()
    return {"ok": True, "cleared_count": count}


@router.delete("/sessions/{session_id}/tasks")
async def clear_all_tasks(session_id: int, db: AsyncSession = Depends(get_db)):
    """清空会话中所有任务及其关联的边和产物。"""
    count = await task_service.clear_all_tasks(db, session_id)
    await db.commit()
    return {"ok": True, "cleared_count": count}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """删除单个任务及其关联的边和产物。"""
    ok = await task_service.delete_task(db, task_id)
    if not ok:
        raise HTTPException(404, "task not found")
    await db.commit()
    return {"ok": True, "task_id": task_id}


@router.post("/tasks/{task_id}/execute")
async def execute_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """v0.3: 单任务手动执行(override)。

    正常流程应由会话级调度器(SessionScheduler.run_ready)并行驱动,
    本端点保留作为单任务重试/补跑入口。
    """
    task = await task_service.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if not task.assigned_agent_id:
        raise HTTPException(400, "task has no assigned agent")

    from app.persistence.models.agent import Agent
    agent = await db.get(Agent, task.assigned_agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")

    # v3：团队/模板概念已移除，系统提示词由 prompts 分层统一构建
    system_prompt = ""
    whitelist: list[str] | None = None
    agent_role = ""

    from app.orchestration.agent_runtime import run_agent_loop
    result = await run_agent_loop(
        db,
        session_id=task.session_id,
        task_id=task_id,
        agent_id=agent.id,
        agent_name=agent.name,
        agent_role=agent_role,
        system_prompt=system_prompt,
        template_whitelist=whitelist,
    )
    await db.commit()
    return {
        "kind": result.kind,
        "text": result.text,
        "error": result.error,
        "artifact_ids": result.artifact_ids,
    }


# ───────────────────────── v0.8 产物查看 ─────────────────────────


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: int, db: AsyncSession = Depends(get_db)):
    """查询单个产物元信息。"""
    a = await db.get(Artifact, artifact_id)
    if not a:
        raise HTTPException(404, "artifact not found")
    return {
        "id": a.id, "task_id": a.task_id, "type": a.type,
        "title": a.title, "storage_ref": a.storage_ref, "summary": a.summary,
    }


@router.get("/artifacts/{artifact_id}/content")
async def get_artifact_content(artifact_id: int, db: AsyncSession = Depends(get_db)):
    """读取产物文件内容(工作区内相对路径)。

    返回 {path, content, size, truncated, language}。
    二进制文件返回提示而非内容。
    """
    a = await db.get(Artifact, artifact_id)
    if not a:
        raise HTTPException(404, "artifact not found")
    if not a.storage_ref:
        return {"path": None, "content": "(无文件路径)", "size": 0, "truncated": False, "language": None}

    target = safe_resolve(settings.workspace_root, a.storage_ref)
    if target is None:
        raise HTTPException(400, "路径越界或非法")
    if not target.exists() or not target.is_file():
        return {"path": a.storage_ref, "content": "(文件不存在)", "size": 0, "truncated": False, "language": None, "missing": True}

    size = target.stat().st_size
    # 二进制检测：尝试 utf-8 解码
    try:
        raw = target.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"path": a.storage_ref, "content": "(二进制文件，无法预览)", "size": size, "truncated": False, "language": None, "binary": True}

    max_chars = 50000
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n\n...(已截断，完整内容请打开文件)"

    # 简单语言推断
    suffix = target.suffix.lower().lstrip(".")
    lang_map = {
        "py": "python", "js": "javascript", "ts": "typescript", "tsx": "tsx",
        "jsx": "jsx", "html": "html", "css": "css", "json": "json",
        "md": "markdown", "sh": "bash", "go": "go", "rs": "rust",
        "java": "java", "c": "c", "cpp": "cpp", "yml": "yaml", "yaml": "yaml",
    }
    language = lang_map.get(suffix, suffix or "text")

    return {
        "path": a.storage_ref, "content": text, "size": size,
        "truncated": truncated, "language": language,
    }


# ───────────────────────── v0.8 Agent 历史会话 ─────────────────────────


@router.get("/agents/{agent_id}/tasks")
async def list_agent_tasks(agent_id: int, db: AsyncSession = Depends(get_db)):
    """查询某个 agent 参与过的所有任务(跨会话)。"""
    res = await db.execute(
        select(Task)
        .where(Task.assigned_agent_id == agent_id)
        .order_by(Task.id.desc())
        .limit(100)
    )
    tasks = res.scalars().all()
    return [
        {
            "id": t.id, "session_id": t.session_id, "title": t.title,
            "status": t.status, "assigned_agent_id": t.assigned_agent_id,
        }
        for t in tasks
    ]


@router.get("/agents/{agent_id}/messages")
async def list_agent_messages(
    agent_id: int, db: AsyncSession = Depends(get_db), limit: int = 50,
):
    """查询某个 agent 发出的所有消息(跨会话,按时间倒序)。"""
    from app.persistence.models.message import Message

    res = await db.execute(
        select(Message)
        .where(Message.sender_type == "agent")
        .where(Message.sender_id == agent_id)
        .order_by(Message.created_at.desc())
        .limit(min(limit, 200))
    )
    msgs = res.scalars().all()
    return [
        {
            "id": m.id, "session_id": m.session_id, "thread_id": m.thread_id,
            "sender_type": m.sender_type, "sender_id": m.sender_id,
            "msg_type": m.msg_type, "content": m.content,
            "created_at": m.created_at,
        }
        for m in msgs
    ]

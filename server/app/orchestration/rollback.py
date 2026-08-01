"""v0.9: 任务中断与回滚服务。

回滚策略(文件+消息双回滚):
1. 文件:用 git 恢复(git checkout -- . && git clean -fd),要求工作目录是 git 仓库。
   非 git 仓库降级为“仅消息回滚”,并提示用户。
2. 消息:标记 deleted=True(软删,保留可追溯),任务状态置 cancelled。

快照:create_snapshot 在任务执行前调用,记录 git HEAD + 关联消息起点。
回滚:rollback_session 取快照 git_head 恢复文件 + 软删消息。

v1.0: 增加文件级 checkpoint——每次 fs_write 前自动快照，支持一键回退到任意步骤。
"""
import asyncio
import logging
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import resolve_workspace_root
from app.persistence.models.message import Message
from app.persistence.models.rollback import RollbackSnapshot
from app.persistence.models.task import Task

logger = logging.getLogger(__name__)

_MAX_GIT_TIMEOUT = 30


async def _run_git(workspace: str, *args: str) -> tuple[bool, str, str]:
    """在工作目录执行 git 命令,返回 (ok, stdout, stderr)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_MAX_GIT_TIMEOUT)
        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        return proc.returncode == 0, out, err
    except asyncio.TimeoutError:
        return False, "", "git 命令超时(>30s)"
    except FileNotFoundError:
        return False, "", "系统未安装 git"
    except OSError as e:
        return False, "", f"git 执行失败: {e}"


async def _is_git_repo(workspace: str) -> bool:
    ok, _, err = await _run_git(workspace, "rev-parse", "--is-inside-work-tree")
    return ok and "true" in err.lower() is False


async def _get_git_head(workspace: str) -> str | None:
    ok, out, _ = await _run_git(workspace, "rev-parse", "HEAD")
    if ok:
        return out.strip() or None
    return None


async def create_snapshot(
    db: AsyncSession, *, session_id: int, task_id: int | None = None,
    workspace: str | None = None,
) -> RollbackSnapshot | None:
    """任务执行前创建快照。返回快照记录(None=非 git 仓库,降级)。"""
    ws = workspace or resolve_workspace_root()
    git_head = await _get_git_head(ws)
    if git_head is None:
        logger.info("session=%s 工作目录非 git 仓库,跳过文件快照(仅支持消息回滚)", session_id)
    snapshot = RollbackSnapshot(
        session_id=session_id,
        task_id=task_id,
        git_head=git_head,
        message_ids=[],
        file_list=[],
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


async def rollback_files(workspace: str, git_head: str | None) -> dict:
    """用 git 恢复工作目录到指定 HEAD。

    git_head 为 None 时表示非 git 仓库,返回 skipped。
    """
    if not git_head:
        return {"ok": False, "skipped": True, "reason": "工作目录非 git 仓库,无法恢复文件"}

    # 1. 还原已跟踪文件到 HEAD
    ok1, out1, err1 = await _run_git(workspace, "checkout", "--", ".")
    # 2. 删除未跟踪文件和目录(-fd 强制),但保留 .git
    ok2, out2, err2 = await _run_git(workspace, "clean", "-fd")
    # 3. 如果有未提交的暂存,也重置
    ok3, out3, err3 = await _run_git(workspace, "reset", "--hard", git_head)

    all_ok = ok1 and ok2 and ok3
    detail = f"checkout:{out1}{err1} clean:{out2}{err2} reset:{out3}{err3}"
    return {"ok": all_ok, "skipped": False, "detail": detail[:500]}


async def rollback_session(
    db: AsyncSession, *, session_id: int, to_snapshot_id: int | None = None,
) -> dict:
    """回滚整个会话:文件恢复 + 消息软删 + 任务取消。

    to_snapshot_id:指定快照(默认取该会话最早未回滚的快照)。
    """
    # 取快照
    if to_snapshot_id:
        snap = await db.get(RollbackSnapshot, to_snapshot_id)
    else:
        res = await db.execute(
            select(RollbackSnapshot)
            .where(RollbackSnapshot.session_id == session_id)
            .where(RollbackSnapshot.rolled_back == 0)
            .order_by(RollbackSnapshot.id.asc())
            .limit(1)
        )
        snap = res.scalars().first()

    if snap is None:
        return {"ok": False, "reason": "无可回滚的快照"}

    # 1. 解析工作目录
    from app.persistence.models.message import Session
    session = await db.get(Session, session_id)
    workspace = resolve_workspace_root(getattr(session, "workspace_root", None) if session else None)

    # 2. 文件回滚
    file_result = await rollback_files(workspace, snap.git_head)

    # 3. 消息软删:该会话该快照之后的消息标记 deleted=True
    msg_res = await db.execute(
        select(Message).where(Message.session_id == session_id)
        .where(Message.created_at >= snap.created_at)
    )
    msgs = msg_res.scalars().all()
    for m in msgs:
        m.deleted = True
    msg_ids = [m.id for m in msgs]

    # 4. 任务取消
    task_res = await db.execute(
        select(Task).where(Task.session_id == session_id)
        .where(Task.created_at >= snap.created_at)
    )
    tasks = task_res.scalars().all()
    for t in tasks:
        if t.status in ("pending", "in_progress", "in_review"):
            t.status = "cancelled"

    # 5. 标记快照已回滚
    snap.rolled_back = 1
    snap.message_ids = msg_ids

    await db.flush()
    return {
        "ok": True,
        "snapshot_id": snap.id,
        "rolled_back_messages": len(msg_ids),
        "rolled_back_tasks": len(tasks),
        "file_recovery": file_result,
    }


async def list_snapshots(db: AsyncSession, session_id: int) -> list[RollbackSnapshot]:
    res = await db.execute(
        select(RollbackSnapshot).where(RollbackSnapshot.session_id == session_id)
        .order_by(RollbackSnapshot.id.desc())
    )
    return list(res.scalars().all())


# ---------------------------------------------------------------------------
# v1.0: 文件级 Checkpoint 快照
# ---------------------------------------------------------------------------

import shutil
import time as _time

_CHECKPOINT_DIR = ".chatcoder/checkpoints"


def checkpoint_file(workspace_root: str, file_path: str, step_id: str = "") -> str | None:
    """v1.0: 在 fs_write 前创建文件级快照。

    将文件复制到 .chatcoder/checkpoints/{timestamp}_{filename}。
    返回快照路径，失败返回 None。
    """
    try:
        src = Path(file_path)
        if not src.is_absolute():
            src = Path(workspace_root) / file_path
        if not src.exists():
            return None  # 新文件无需快照

        ckpt_dir = Path(workspace_root) / _CHECKPOINT_DIR
        ckpt_dir.mkdir(parents=True, exist_ok=True)

        ts = _time.strftime("%Y%m%d_%H%M%S")
        step_suffix = f"_{step_id}" if step_id else ""
        ckpt_name = f"{ts}{step_suffix}_{src.name}"
        ckpt_path = ckpt_dir / ckpt_name

        shutil.copy2(str(src), str(ckpt_path))
        logger.debug("[checkpoint] %s -> %s", src.name, ckpt_path)
        return str(ckpt_path)
    except OSError as e:
        logger.debug("[checkpoint] 快照失败: %s", e)
        return None


def restore_checkpoint(workspace_root: str, checkpoint_path: str, target_path: str) -> bool:
    """v1.0: 从 checkpoint 恢复文件。"""
    try:
        src = Path(checkpoint_path)
        if not src.is_absolute():
            src = Path(workspace_root) / checkpoint_path
        if not src.exists():
            return False

        dst = Path(target_path)
        if not dst.is_absolute():
            dst = Path(workspace_root) / target_path
        dst.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(str(src), str(dst))
        logger.info("[checkpoint] 恢复: %s -> %s", src, dst)
        return True
    except OSError as e:
        logger.warning("[checkpoint] 恢复失败: %s", e)
        return False


def list_checkpoints(workspace_root: str) -> list[dict]:
    """v1.0: 列出所有 checkpoint。"""
    ckpt_dir = Path(workspace_root) / _CHECKPOINT_DIR
    if not ckpt_dir.exists():
        return []
    results = []
    for f in sorted(ckpt_dir.iterdir(), reverse=True):
        if f.is_file():
            results.append({
                "name": f.name,
                "path": str(f),
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
            })
    return results[:50]  # 最多返回 50 个

"""Git 工作树服务（§4.16）。"""
import asyncio
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import session_service

logger = logging.getLogger(__name__)

_TIMEOUT = 30


async def _git(cwd: str, *args: str) -> tuple[bool, str, str]:
    """在工作目录执行 git，返回 (ok, stdout, stderr)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        return proc.returncode == 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return False, "", "git 命令超时"
    except FileNotFoundError:
        return False, "", "系统未安装 git"
    except OSError as e:
        return False, "", f"git 执行失败: {e}"


async def create_worktree(db: AsyncSession, session_id: int, *, branch: str | None = None) -> dict:
    """为会话在项目仓库下创建独立工作树。"""
    session = await session_service.get_session(db, session_id)
    if session is None or session.project_id is None:
        raise ValueError("会话不存在或未关联项目")
    from app.services.project_service import get_project
    project = await get_project(db, session.project_id)
    if project is None:
        raise ValueError("项目不存在")
    repo = project.path

    ok, out, _ = await _git(repo, "rev-parse", "--is-inside-work-tree")
    if not ok or out.strip() != "true":
        raise ValueError("项目目录不是 git 仓库，无法创建工作树")
    if session.worktree_path:
        raise ValueError("会话已存在工作树")

    base = Path(repo) / ".chatcoder" / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    wt_path = base / f"session_{session_id}"
    branch_name = branch or f"chatcoder/session-{session_id}"

    ok, out, err = await _git(repo, "worktree", "add", str(wt_path), "-b", branch_name)
    if not ok:
        raise ValueError(f"创建工作树失败: {(err or out)[:200]}")

    session.worktree_path = str(wt_path)
    await db.flush()
    logger.info("会话 %s 创建 worktree: %s (branch=%s)", session_id, wt_path, branch_name)
    return {"ok": True, "path": str(wt_path), "branch": branch_name}


async def remove_worktree(db: AsyncSession, session_id: int) -> dict:
    session = await session_service.get_session(db, session_id)
    if session is None or not session.worktree_path:
        raise ValueError("会话无工作树")
    wt = session.worktree_path

    # 先检查工作树是否干净
    ok, out, _ = await _git(wt, "status", "--porcelain")
    if ok and out.strip():
        raise ValueError("工作树存在未提交变更，请先 commit 或 stash")

    from app.services.project_service import get_project
    project = await get_project(db, session.project_id) if session.project_id else None
    repo = project.path if project else wt

    ok, out, err = await _git(repo, "worktree", "remove", wt, "--force")
    if not ok:
        raise ValueError(f"移除工作树失败: {(err or out)[:200]}")
    session.worktree_path = None
    await db.flush()
    logger.info("会话 %s 移除 worktree: %s", session_id, wt)
    return {"ok": True}

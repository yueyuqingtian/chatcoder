"""Git 常规操作路由（plan-20261017-goal12 项 5）。所有接口以 project_id 解析工作目录（含工作树），
具体逻辑见 services/git_service.py；可预期的错误统一以 400 + 中文说明返回。

plan-20261019-git-multirepo：同一项目目录下可能有多个独立仓库（如前端 + 后端），接口统一增加可选的
`repo`（相对项目根的子目录，空 = 根仓库）；`session_id` 指定时以该会话的工作树目录为准。"""
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import git_service
from app.services.git_service import GitError
from app.services.project_service import get_project

router = APIRouter(prefix="/git", tags=["git"])


async def _workspace(db: AsyncSession, project_id: int, session_id: int | None = None) -> str:
    """项目工作目录；会话在自己的工作树里工作时（「在工作树中继续」）以工作树目录为准。"""
    project = await get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    if session_id:
        from app.services import session_service

        session = await session_service.get_session(db, session_id)
        wt = getattr(session, "worktree_path", None) if session is not None else None
        if wt and getattr(session, "project_id", None) == project_id and Path(wt).is_dir():
            return wt
    return project.path


async def _project_path(db: AsyncSession, project_id: int, repo: str | None = None,
                        session_id: int | None = None) -> str:
    """操作目标仓库的目录（repo 为空时为工作目录本身）。"""
    workspace = await _workspace(db, project_id, session_id)
    try:
        return git_service.repo_dir(workspace, repo)
    except GitError as e:
        raise HTTPException(400, str(e)) from e


async def _call(coro):
    try:
        return await coro
    except GitError as e:
        raise HTTPException(400, str(e)) from e


class _Target(BaseModel):
    project_id: int
    # plan-20261019-git-multirepo：目标仓库（相对项目根）与会话（会话工作树优先）
    repo: str | None = Field(default=None, max_length=500)
    session_id: int | None = None


class PathsBody(_Target):
    paths: list[str] = Field(default_factory=list, max_length=500)


class DiscardBody(PathsBody):
    untracked: list[str] = Field(default_factory=list, max_length=500)


class CommitBody(_Target):
    message: str = Field(default="", max_length=20000)
    amend: bool = False


class SyncBody(_Target):
    action: Literal["fetch", "pull", "push"]


class BranchBody(_Target):
    action: Literal["checkout", "create", "delete"]
    name: str = Field(min_length=1, max_length=200)
    base: str | None = Field(default=None, max_length=200)
    force: bool = False
    # 切换分支时如何处理未提交改动：stash=自动暂存后切换；discard=放弃改动后切换
    on_dirty: Literal["stash", "discard"] | None = None


class ResolveBody(_Target):
    path: str = Field(min_length=1, max_length=1000)
    side: Literal["ours", "theirs"]


class ProjectBody(_Target):
    pass


_REPO_Q = Query(None, max_length=500)


@router.get("/repos")
async def repos(project_id: int = Query(...), session_id: int | None = Query(None),
                db: AsyncSession = Depends(get_db)):
    """项目内的全部仓库（根仓库在前），带分支与改动数，供面板切换仓库。"""
    return await _call(git_service.discover_repos(await _workspace(db, project_id, session_id)))


@router.get("/status-all")
async def status_all(project_id: int = Query(...), session_id: int | None = Query(None),
                     db: AsyncSession = Depends(get_db)):
    """汇总全部仓库的状态（路径带仓库前缀），供工具卡按项目展示改动。"""
    return await _call(git_service.status_all(await _workspace(db, project_id, session_id)))


@router.get("/diff-stat")
async def diff_stat(project_id: int = Query(...), session_id: int | None = Query(None),
                    db: AsyncSession = Depends(get_db)):
    """未提交改动的增删行数合计（已暂存 + 未暂存 + 未跟踪文本文件），供工具卡 Git 栏的「更改 +N −M」。"""
    return await _call(git_service.diff_stat(await _workspace(db, project_id, session_id)))


@router.get("/status")
async def status(project_id: int = Query(...), repo: str | None = _REPO_Q, session_id: int | None = Query(None),
                 db: AsyncSession = Depends(get_db)):
    return await _call(git_service.status(await _project_path(db, project_id, repo, session_id)))


@router.get("/diff")
async def diff(project_id: int = Query(...), path: str = Query(..., max_length=1000),
               staged: bool = Query(False), repo: str | None = _REPO_Q, session_id: int | None = Query(None),
               db: AsyncSession = Depends(get_db)):
    return await _call(git_service.diff(await _project_path(db, project_id, repo, session_id), path, staged))


@router.get("/log")
async def log(project_id: int = Query(...), limit: int = Query(40, ge=1, le=200),
              skip: int = Query(0, ge=0, le=100_000), repo: str | None = _REPO_Q,
              session_id: int | None = Query(None), db: AsyncSession = Depends(get_db)):
    return await _call(git_service.log(await _project_path(db, project_id, repo, session_id), limit, skip))


@router.get("/commit")
async def commit_detail(project_id: int = Query(...), sha: str = Query(..., min_length=4, max_length=64),
                        repo: str | None = _REPO_Q, session_id: int | None = Query(None),
                        db: AsyncSession = Depends(get_db)):
    return await _call(git_service.commit_detail(await _project_path(db, project_id, repo, session_id), sha))


@router.get("/commit/diff")
async def commit_diff(project_id: int = Query(...), sha: str = Query(..., min_length=4, max_length=64),
                      path: str = Query(..., max_length=1000), repo: str | None = _REPO_Q,
                      session_id: int | None = Query(None), db: AsyncSession = Depends(get_db)):
    return await _call(git_service.commit_diff(await _project_path(db, project_id, repo, session_id), sha, path))


@router.get("/branches")
async def branches(project_id: int = Query(...), repo: str | None = _REPO_Q, session_id: int | None = Query(None),
                   db: AsyncSession = Depends(get_db)):
    return await _call(git_service.branches(await _project_path(db, project_id, repo, session_id)))


async def _body_path(db: AsyncSession, body: _Target) -> str:
    return await _project_path(db, body.project_id, body.repo, body.session_id)


@router.post("/stage")
async def stage(body: PathsBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.stage(await _body_path(db, body), body.paths))
    return {"ok": True}


@router.post("/unstage")
async def unstage(body: PathsBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.unstage(await _body_path(db, body), body.paths))
    return {"ok": True}


@router.post("/discard")
async def discard(body: DiscardBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.discard(await _body_path(db, body), body.paths, body.untracked))
    return {"ok": True}


@router.post("/commit")
async def commit(body: CommitBody, db: AsyncSession = Depends(get_db)):
    sha = await _call(git_service.commit(await _body_path(db, body), body.message, body.amend))
    return {"ok": True, "sha": sha}


@router.post("/sync")
async def sync(body: SyncBody, db: AsyncSession = Depends(get_db)):
    # 拉取冲突不是错误：结果带着 conflict / conflicts / state，界面直接进入冲突处理
    return {"ok": True, **(await _call(git_service.sync(await _body_path(db, body), body.action)))}


@router.post("/branch")
async def branch(body: BranchBody, db: AsyncSession = Depends(get_db)):
    return {"ok": True, **(await _call(git_service.branch_action(
        await _body_path(db, body), body.action, body.name, body.base, body.force,
        body.on_dirty,
    )))}


@router.post("/resolve")
async def resolve(body: ResolveBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.resolve_conflict(await _body_path(db, body), body.path, body.side))
    return {"ok": True}


@router.post("/merge/abort")
async def merge_abort(body: ProjectBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.abort_merge(await _body_path(db, body)))
    return {"ok": True}

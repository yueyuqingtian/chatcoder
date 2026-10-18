"""Git 常规操作路由（plan-20261017-goal12 项 5）。所有接口以 project_id 解析工作目录（含工作树），
具体逻辑见 services/git_service.py；可预期的错误统一以 400 + 中文说明返回。"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import git_service
from app.services.git_service import GitError
from app.services.project_service import get_project

router = APIRouter(prefix="/git", tags=["git"])


async def _project_path(db: AsyncSession, project_id: int) -> str:
    project = await get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project.path


async def _call(coro):
    try:
        return await coro
    except GitError as e:
        raise HTTPException(400, str(e)) from e


class PathsBody(BaseModel):
    project_id: int
    paths: list[str] = Field(default_factory=list, max_length=500)


class DiscardBody(PathsBody):
    untracked: list[str] = Field(default_factory=list, max_length=500)


class CommitBody(BaseModel):
    project_id: int
    message: str = Field(default="", max_length=20000)
    amend: bool = False


class SyncBody(BaseModel):
    project_id: int
    action: Literal["fetch", "pull", "push"]


class BranchBody(BaseModel):
    project_id: int
    action: Literal["checkout", "create", "delete"]
    name: str = Field(min_length=1, max_length=200)
    base: str | None = Field(default=None, max_length=200)
    force: bool = False
    # 切换分支时如何处理未提交改动：stash=自动暂存后切换；discard=放弃改动后切换
    on_dirty: Literal["stash", "discard"] | None = None


class ResolveBody(BaseModel):
    project_id: int
    path: str = Field(min_length=1, max_length=1000)
    side: Literal["ours", "theirs"]


class ProjectBody(BaseModel):
    project_id: int


@router.get("/status")
async def status(project_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    return await _call(git_service.status(await _project_path(db, project_id)))


@router.get("/diff")
async def diff(project_id: int = Query(...), path: str = Query(..., max_length=1000),
               staged: bool = Query(False), db: AsyncSession = Depends(get_db)):
    return await _call(git_service.diff(await _project_path(db, project_id), path, staged))


@router.get("/log")
async def log(project_id: int = Query(...), limit: int = Query(40, ge=1, le=200),
              skip: int = Query(0, ge=0, le=100_000), db: AsyncSession = Depends(get_db)):
    return await _call(git_service.log(await _project_path(db, project_id), limit, skip))


@router.get("/commit")
async def commit_detail(project_id: int = Query(...), sha: str = Query(..., min_length=4, max_length=64),
                        db: AsyncSession = Depends(get_db)):
    return await _call(git_service.commit_detail(await _project_path(db, project_id), sha))


@router.get("/commit/diff")
async def commit_diff(project_id: int = Query(...), sha: str = Query(..., min_length=4, max_length=64),
                      path: str = Query(..., max_length=1000), db: AsyncSession = Depends(get_db)):
    return await _call(git_service.commit_diff(await _project_path(db, project_id), sha, path))


@router.get("/branches")
async def branches(project_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    return await _call(git_service.branches(await _project_path(db, project_id)))


@router.post("/stage")
async def stage(body: PathsBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.stage(await _project_path(db, body.project_id), body.paths))
    return {"ok": True}


@router.post("/unstage")
async def unstage(body: PathsBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.unstage(await _project_path(db, body.project_id), body.paths))
    return {"ok": True}


@router.post("/discard")
async def discard(body: DiscardBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.discard(await _project_path(db, body.project_id), body.paths, body.untracked))
    return {"ok": True}


@router.post("/commit")
async def commit(body: CommitBody, db: AsyncSession = Depends(get_db)):
    sha = await _call(git_service.commit(await _project_path(db, body.project_id), body.message, body.amend))
    return {"ok": True, "sha": sha}


@router.post("/sync")
async def sync(body: SyncBody, db: AsyncSession = Depends(get_db)):
    # 拉取冲突不是错误：结果带着 conflict / conflicts / state，界面直接进入冲突处理
    return {"ok": True, **(await _call(git_service.sync(await _project_path(db, body.project_id), body.action)))}


@router.post("/branch")
async def branch(body: BranchBody, db: AsyncSession = Depends(get_db)):
    return {"ok": True, **(await _call(git_service.branch_action(
        await _project_path(db, body.project_id), body.action, body.name, body.base, body.force,
        body.on_dirty,
    )))}


@router.post("/resolve")
async def resolve(body: ResolveBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.resolve_conflict(await _project_path(db, body.project_id), body.path, body.side))
    return {"ok": True}


@router.post("/merge/abort")
async def merge_abort(body: ProjectBody, db: AsyncSession = Depends(get_db)):
    await _call(git_service.abort_merge(await _project_path(db, body.project_id)))
    return {"ok": True}

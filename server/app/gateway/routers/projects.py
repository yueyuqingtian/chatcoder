"""项目（工作目录）路由；plan-282-1441（#5）追加工作树管理端点。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import ProjectCreate, ProjectOut, ProjectUpdate
from app.orchestration.rules_loader import scan_rules_docs
from app.persistence.database import get_db
from app.services import project_service, worktree_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectOut)
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db)):
    try:
        pid = await project_service.create_project(
            db, path=body.path, name=body.name,
            rules_docs=body.rules_docs, auto_scan_rules=body.auto_scan_rules,
        )
        project = await project_service.get_project(db, pid)  # async 只读
        return project
    except ValueError as e:
        raise HTTPException(400, str(e))
    except project_service.ProjectArchivedError as e:
        # plan-278-1391: 同路径项目已归档 → 409 + 结构化信息，前端提示「恢复并打开」，
        # 不再让 UNIQUE 约束冲突冒泡成 500。
        raise HTTPException(409, detail={
            "code": "project_archived",
            "message": "该项目已归档，是否恢复并打开？",
            "project_id": e.project_id,
            "name": e.name,
            "path": e.path,
        })


@router.get("", response_model=list[ProjectOut])
async def list_projects(include_archived: bool = False, db: AsyncSession = Depends(get_db)):
    """项目列表；include_archived=true 时返回含归档项目（归档恢复面板用）。"""
    return await project_service.list_projects(db, include_archived=include_archived)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, db: AsyncSession = Depends(get_db)):
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(project_id: int, body: ProjectUpdate, db: AsyncSession = Depends(get_db)):
    ok = await project_service.update_project(
        db, project_id,
        name=body.name, rules_docs=body.rules_docs, auto_scan_rules=body.auto_scan_rules,
        pinned=body.pinned, archived=body.archived,
    )
    if ok is None:
        raise HTTPException(404, "项目不存在")
    project = await project_service.get_project(db, project_id)
    return project


@router.delete("/{project_id}", response_model=dict)
async def delete_project(project_id: int, db: AsyncSession = Depends(get_db)):
    ok = await project_service.delete_project(db, project_id)
    if not ok:
        raise HTTPException(404, "项目不存在")
    return {"ok": True}


@router.get("/{project_id}/scan-rules", response_model=list[str])
async def project_scan_rules(project_id: int, db: AsyncSession = Depends(get_db)):
    """扫描项目目录下的规范文档候选。"""
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return await scan_rules_docs(project.path)


@router.get("/{project_id}/stat", response_model=dict)
async def project_stat(project_id: int, path: str,
                       db: AsyncSession = Depends(get_db)):
    """问题2: 轻量文件存在性校验（不读内容），供消息内文件链接判断是否可点击。"""
    import os
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    root = os.path.abspath(project.path)
    target = os.path.abspath(os.path.join(root, path.lstrip("/\\")))
    if not target.startswith(root + os.sep) and target != root:
        return {"exists": False, "is_dir": False}
    is_dir = os.path.isdir(target)
    return {"exists": os.path.exists(target), "is_dir": is_dir}


@router.get("/{project_id}/read-file", response_model=dict)
async def project_read_file(project_id: int, path: str,
                            db: AsyncSession = Depends(get_db)):
    """读取项目内文件（供右侧文件预览面板）。仅允许项目路径内。"""
    import os
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    root = os.path.abspath(project.path)
    target = os.path.abspath(os.path.join(root, path.lstrip("/\\")))
    if not target.startswith(root + os.sep) and target != root:
        raise HTTPException(400, "路径越界")
    if not os.path.isfile(target):
        raise HTTPException(404, "文件不存在")
    size = os.path.getsize(target)
    MAX_BYTES = 512 * 1024
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(MAX_BYTES) if size > MAX_BYTES else f.read()
    except OSError as e:
        raise HTTPException(500, f"读取失败: {e}")
    ext = os.path.splitext(target)[1].lstrip(".").lower()
    return {"path": target.replace("\\", "/"), "content": content,
            "size": size, "truncated": size > MAX_BYTES, "language": ext or None}


@router.get("/{project_id}/tree", response_model=dict)
async def project_tree(project_id: int, depth: int = Query(2, ge=1, le=8),
                       db: AsyncSession = Depends(get_db)):
    """项目目录树（供右侧文件管理面板）。"""
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return _build_tree(project.path, max_depth=depth)


# 问题13: 文件搜索排除的常见忽略目录（与消息流工具一致）
_SEARCH_EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", "target", ".idea", ".vscode", "logs", ".cache",
    "coverage", ".pytest_cache", ".ruff_cache",
}


@router.get("/{project_id}/files/search", response_model=list[str])
async def project_file_search(project_id: int, q: str = "", limit: int = 100,
                              db: AsyncSession = Depends(get_db)):
    """问题13: 按文件名/路径子串搜索项目内全部文件（不设深度限制，排除常见忽略目录）。

    供输入框 @ 引用文件补全使用；空查询返回空列表。
    """
    import os
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    needle = q.strip().lower()
    if not needle:
        return []
    root = project.path
    results: list[str] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地过滤忽略目录（含隐藏目录）
        dirnames[:] = [
            d for d in dirnames
            if d not in _SEARCH_EXCLUDE_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            scanned += 1
            if scanned > 200_000:  # 防御性上限：超大仓库避免无限扫描
                return results
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/")
            if needle in rel.lower():
                results.append(rel)
                if len(results) >= limit:
                    return results
    return results


def _build_tree(root: str, max_depth: int) -> dict:
    import os

    def walk(path: str, depth: int) -> list[dict]:
        if depth > max_depth:
            return []
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return []
        out = []
        # 先排除不可引用的目录，再限制数量，避免无关条目占满前 200 项。
        visible = [e for e in entries if not e.name.startswith(".") and e.name != "node_modules"]
        for e in visible[:200]:
            node = {"name": e.name, "type": "dir" if e.is_dir() else "file",
                    "path": e.path.replace("\\", "/")}
            if e.is_dir():
                node["children"] = walk(e.path, depth + 1)
            out.append(node)
        return out

    return {"path": root.replace("\\", "/"), "children": walk(root, 0)}


# ═══════════════════════════════════════════════════════════════════════
# plan-282-1441（#5）：工作树（worktree）——按项目管理，合并走三栏冲突解决
# ═══════════════════════════════════════════════════════════════════════


class WorktreeCreateBody(BaseModel):
    name: str | None = None
    branch: str | None = None
    # plan-282-1441：要建工作树的仓库（项目根或子仓库的绝对路径，可多选）；
    # 为空时后端自动探测（优先有提交的子仓库）
    repos: list[str] | None = None


class MergeFileBody(BaseModel):
    path: str
    # 合并方向：to_main=工作树→主工作区（默认）；from_main=主工作区→工作树
    direction: str = "to_main"


class MergeApplyBody(BaseModel):
    # [{path, content, deleted?}]：content 为最终文本；deleted=True 表示删除该文件
    files: list[dict]
    direction: str = "to_main"


class MergeAiBody(BaseModel):
    path: str
    # 可选：仅针对某个冲突块（未传则对整文件给建议）
    hunk: dict | None = None
    direction: str = "to_main"
    # 用户自选的模型 id（任意供应商下的模型）；不传则由服务端默认解析
    model_id: int | None = None


class MergePreviewBody(BaseModel):
    direction: str = "to_main"


class MergeAiAllBody(BaseModel):
    """plan-308-1542 需求3-A：一键 AI 智能合并（进度经 WS merge.progress 广播）。"""
    direction: str = "to_main"
    model_id: int | None = None
    # 会话 id：进度事件按会话归属推送（与 debug.paused 同口径）
    session_id: int | None = None


class WorktreeCommitBody(BaseModel):
    """提交某一侧的未提交改动（合并前的自动提交）。side: worktree | main"""
    side: str = "worktree"
    message: str | None = None


@router.get("/{project_id}/repo-candidates", response_model=list[dict])
async def project_repo_candidates(project_id: int, db: AsyncSession = Depends(get_db)):
    """列出该项目可用于创建工作树的仓库（项目根 + 直接的子仓库）。

    plan-282-1441：#5 修复——支持"根目录是空仓库、真实代码在子仓库"的布局，
    前端据此弹出勾选（如 clinic 后端 + clinicFrontEnd 前端）。
    """
    try:
        return await worktree_service.list_repo_candidates(db, project_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{project_id}/worktrees", response_model=dict)
async def create_project_worktree(project_id: int, body: WorktreeCreateBody,
                                  db: AsyncSession = Depends(get_db)):
    """为项目（或其选中的子仓库）创建工作树并登记为独立工作区。"""
    try:
        return await worktree_service.create_worktree_for_project(
            db, project_id, name=body.name, branch=body.branch, repos=body.repos)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{project_id}/worktrees", response_model=list[dict])
async def list_project_worktrees(project_id: int, db: AsyncSession = Depends(get_db)):
    """列出该项目下的工作树（含 git 状态摘要）。"""
    return await worktree_service.list_worktrees(db, project_id=project_id)


@router.post("/worktrees/cleanup-stale", response_model=dict)
async def cleanup_stale_worktrees(db: AsyncSession = Depends(get_db)):
    """清理"git 侧已不存在、数据库仍登记"的失效工作树（左面板删不掉的僵尸项）。

    plan-308-1542 修复：目录/分支被外部删除（或早前版本删除时因外键报错中断）后，
    左面板会一直显示这个工作树且删不掉。设置页与启动自愈都走本接口。
    """
    return await worktree_service.cleanup_stale_worktrees(db)


@router.delete("/worktrees/{worktree_project_id}", response_model=dict)
async def delete_project_worktree(worktree_project_id: int, force: bool = False,
                                  db: AsyncSession = Depends(get_db)):
    """删除工作树（force=true 时忽略未提交变更）。"""
    try:
        return await worktree_service.remove_worktree_project(
            db, worktree_project_id, force=force)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/worktrees/{worktree_project_id}/merge/preview", response_model=dict)
async def worktree_merge_preview(worktree_project_id: int, body: MergePreviewBody | None = None,
                                 db: AsyncSession = Depends(get_db)):
    """合并预览：差异文件列表（含未提交改动、自动冲突解决结果）。不改动任何工作区。"""
    try:
        return await worktree_service.merge_preview(
            db, worktree_project_id, direction=(body.direction if body else "to_main"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/worktrees/{worktree_project_id}/merge/file", response_model=dict)
async def worktree_merge_file(worktree_project_id: int, body: MergeFileBody,
                              db: AsyncSession = Depends(get_db)):
    """三路内容：base（共同祖先）/ ours（目标侧）/ theirs（来源侧），均取工作区当前内容。"""
    try:
        return await worktree_service.merge_file_blobs(
            db, worktree_project_id, body.path, direction=body.direction)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/worktrees/{worktree_project_id}/merge/apply", response_model=dict)
async def worktree_merge_apply(worktree_project_id: int, body: MergeApplyBody,
                               db: AsyncSession = Depends(get_db)):
    """应用合并结果并提交到目标侧工作区。"""
    try:
        return await worktree_service.merge_apply(
            db, worktree_project_id, body.files, direction=body.direction)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/worktrees/{worktree_project_id}/merge/ai", response_model=dict)
async def worktree_merge_ai(worktree_project_id: int, body: MergeAiBody,
                            db: AsyncSession = Depends(get_db)):
    """某文件 / 某冲突块的 AI 合并建议（可指定模型；失败不阻塞，返回 ok=false + 原因）。"""
    return await worktree_service.ai_merge_suggest(
        db, worktree_project_id, body.path, body.hunk,
        direction=body.direction, model_id=body.model_id)


@router.post("/worktrees/{worktree_project_id}/merge/ai-all", response_model=dict)
async def worktree_merge_ai_all(worktree_project_id: int, body: MergeAiAllBody,
                                db: AsyncSession = Depends(get_db)):
    """一键 AI 智能合并（plan-308-1542 需求3-A）。

    执行期间经 WS 广播 `merge.progress`（当前文件/阶段/工具调用/耗时），
    前端在合并弹窗内像消息流一样实时追加行；完成后返回汇总报告。
    """
    try:
        return await worktree_service.ai_merge_all(
            db, worktree_project_id, direction=body.direction,
            model_id=body.model_id, session_id=body.session_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/worktrees/{worktree_project_id}/commit", response_model=dict)
async def worktree_commit(worktree_project_id: int, body: WorktreeCommitBody,
                          db: AsyncSession = Depends(get_db)):
    """提交某一侧的未提交改动（side=worktree 提交工作树，side=main 提交主工作区）。"""
    try:
        return await worktree_service.commit_worktree(
            db, worktree_project_id, side=body.side, message=body.message)
    except ValueError as e:
        raise HTTPException(400, str(e))

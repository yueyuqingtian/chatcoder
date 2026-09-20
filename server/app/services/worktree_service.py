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


# 本工具自身的工作目录：工作树就建在这里。它不该被当成"用户的未提交变更"。
_TOOL_DIR = ".chatcoder"


def _status_lines(raw: str) -> list[str]:
    """过滤掉本工具目录产生的状态行（见 _ensure_tool_dir_ignored 的说明）。"""
    out: list[str] = []
    for line in (raw or "").splitlines():
        # porcelain 格式：XY <path>；重命名形如 "R  old -> new"
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path.startswith(_TOOL_DIR + "/") or path == _TOOL_DIR:
            continue
        out.append(line)
    return out


async def _is_repo_dirty(repo: str) -> bool:
    """主工作区是否有**用户**未提交变更（忽略本工具自己的 .chatcoder 目录）。"""
    ok, raw, _ = await _git(repo, "status", "--porcelain")
    if not ok:
        return False
    return bool(_status_lines(raw))


async def _ensure_tool_dir_ignored(repo: str) -> None:
    """把 `.chatcoder/` 写进 `.git/info/exclude`（本地忽略，不改用户 .gitignore）。

    必要性：工作树建在 `<repo>/.chatcoder/worktrees/` 下，若不忽略，主仓库会因这个
    未跟踪目录而恒被判为"有未提交变更"——合并与删除会被自己的目录死死挡住。
    用 `info/exclude` 而非 `.gitignore`：不污染用户的版本控制内容，且立即生效。
    """
    try:
        ok, git_dir, _ = await _git(repo, "rev-parse", "--git-dir")
        if not ok or not git_dir.strip():
            return
        gd = Path(git_dir.strip())
        if not gd.is_absolute():
            gd = Path(repo) / gd
        exclude = gd / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
        if f"{_TOOL_DIR}/" in existing:
            return
        line = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(existing + line + f"{_TOOL_DIR}/\n", encoding="utf-8")
    except OSError:
        logger.debug("[worktree] 写入 .git/info/exclude 失败(非阻塞)", exc_info=True)


def _force_rmtree(path: Path) -> bool:
    """强制递归删除目录，返回是否已彻底删除。

    背景：`git worktree remove` 在这些情形会失败——
      - 工作树内存在被**忽略的文件**（git ≥2.31 需要连续两次 `--force` 才肯删）；
      - Windows 上文件带只读位、或被 IDE / 杀软占用。
    此前失败后只做 `worktree prune`（仅清理 git 登记），DB 记录被删而目录留在磁盘，
    用户看到"工作树删了、文件夹还在"。这里兜底：去掉只读位后逐个删除。
    """
    import os
    import shutil
    import stat as _stat

    if not path.exists():
        return True

    def _rm_file(f: Path) -> None:
        try:
            f.unlink()
        except OSError:
            try:
                f.chmod(_stat.S_IWRITE | _stat.S_IREAD)
            except OSError:
                pass
            try:
                f.unlink()
            except OSError:
                logger.debug("[worktree] 删除文件失败(可能被占用): %s", f, exc_info=True)

    for root, _dirs, files in os.walk(path, topdown=False):
        for name in files:
            _rm_file(Path(root) / name)
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


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

    # 分支已存在时给出可读提示（否则 git 直接抛 "a branch named ... already exists"）
    if await _branch_exists(repo, branch_name):
        raise ValueError(f"仓库已存在分支 {branch_name}，请另填分支名或先删除该分支")

    ok, out, err = await _git(repo, "worktree", "add", str(wt_path), "-b", branch_name)
    if not ok:
        raise ValueError(f"创建工作树失败: {(err or out)[:200]}")

    from app.persistence.database import run_write_locked

    def _persist_path(s, value: str | None):
        from app.persistence.models.message import Session as _Sess
        row = s.get(_Sess, session_id)
        if row is not None:
            row.worktree_path = value
        s.commit()

    await run_write_locked(lambda s: _persist_path(s, str(wt_path)), label=f"worktree.create.{session_id}")
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

    # 分支名必须在摘除工作树之前读取（摘除后工作树目录已不存在）
    branch = await _branch_of_worktree(wt)

    from app.services.project_service import get_project
    project = await get_project(db, session.project_id) if session.project_id else None
    repo = project.path if project else wt

    ok, out, err = await _git(repo, "worktree", "remove", "--force", wt)
    if not ok:
        # git 拒绝（含被忽略文件 / 文件被占用 / 登记与目录不一致）时兜底：
        # 先清掉 git 登记，再强删目录——否则会留下"记录删了、目录还在"的残留。
        await _git(repo, "worktree", "prune")
        if not _force_rmtree(Path(wt)):
            raise ValueError(f"移除工作树失败，目录仍被占用: {wt}")
        logger.warning("worktree remove 失败，已强制清理目录 %s: %s", wt, (err or out)[:200])

    # 连带删除该工作树的本地分支，避免仓库残留 chatcoder/session-xxx
    branch_deleted = await _delete_local_branch(repo, branch)
    await _git(repo, "worktree", "prune")
    from app.persistence.database import run_write_locked

    def _persist_path(s, value: str | None):
        from app.persistence.models.message import Session as _Sess
        row = s.get(_Sess, session_id)
        if row is not None:
            row.worktree_path = value
        s.commit()

    await run_write_locked(lambda s: _persist_path(s, None), label=f"worktree.remove.{session_id}")
    logger.info("会话 %s 移除 worktree: %s (branch=%s, 删除=%s)", session_id, wt, branch, branch_deleted)
    return {"ok": True, "branch": branch, "branch_deleted": branch_deleted}


# ═══════════════════════════════════════════════════════════════════════
# plan-282-1441（#5）：工作树按**项目**管理（独立于会话的工作树）
#
# 设计：工作树复用 Project 表（is_worktree=True / parent_project_id / worktree_branch）——
# 它"就是一个工作区"，session.project_id 指向它即可正常建会话、跑引擎，
# 左侧面板也天然能作为独立工作区渲染。本段提供项目级 CRUD 与合并能力。
# ═══════════════════════════════════════════════════════════════════════

_WT_DIR = ".chatcoder/worktrees"


async def _project_of(db: AsyncSession, project_id: int):
    from app.services.project_service import get_project
    return await get_project(db, project_id)


async def _require_repo(project) -> None:
    """校验项目目录是 git 仓库。"""
    ok, out, _ = await _git(project.path, "rev-parse", "--is-inside-work-tree")
    if not ok or out.strip() != "true":
        raise ValueError("项目目录不是 git 仓库，无法使用工作树")


def _wt_dir_of(repo: str) -> Path:
    return Path(repo) / _WT_DIR


async def _default_branch(repo: str) -> str:
    """探测仓库的**当前分支**作为合并/工作树的基点。

    plan-282-1441（#5 修复）：此前探测失败时盲目回退 `"main"` —— 若仓库实际没有
    main 分支（空仓库尚未产生首个提交、或默认分支叫 master/其他名字），
    后续 `git worktree add ... main` 会直接失败并报
    `fatal: invalid reference: main`（用户实际遇到的报错）。
    现在：逐级回退到**真实存在**的引用，都不存在则返回空串，由调用方给出可读提示。
    """
    # 1) 当前 HEAD 指向的分支（正常有提交的仓库）
    ok, out, _ = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    name = out.strip() if ok else ""
    if name and name != "HEAD":
        return name
    # 2) HEAD 游离但存在提交：用 HEAD 本身作为基点（git 接受任意 rev）
    ok, out, _ = await _git(repo, "rev-parse", "--verify", "HEAD")
    if ok and out.strip():
        return "HEAD"
    # 3) 常见默认分支名，仅在确实存在时采用
    for candidate in ("main", "master"):
        ok, out, _ = await _git(repo, "rev-parse", "--verify", candidate)
        if ok and out.strip():
            return candidate
    # 4) 空仓库：没有任何可用基点
    return ""


async def _repo_root_of(path: str) -> str | None:
    """返回 path 所属 git 仓库的**根目录**（不是仓库或不存在则返回 None）。"""
    ok, out, _ = await _git(path, "rev-parse", "--show-toplevel")
    if not ok or not out.strip():
        return None
    return out.strip().replace("\\", "/")


async def _has_commits(repo: str) -> bool:
    """仓库是否已有提交（空仓库无法作为工作树起点）。"""
    ok, out, _ = await _git(repo, "rev-parse", "--verify", "HEAD")
    return bool(ok and out.strip())


async def _branch_exists(repo: str, branch: str) -> bool:
    """本地是否存在同名分支（refs/heads）。"""
    if not branch:
        return False
    ok, out, _ = await _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
    return bool(ok and out.strip())


async def _branch_of_worktree(wt_path: str) -> str | None:
    """读取工作树当前检出的分支名；游离 HEAD 或失败时返回 None。"""
    ok, out, _ = await _git(wt_path, "rev-parse", "--abbrev-ref", "HEAD")
    name = out.strip() if ok else ""
    if not name or name == "HEAD":
        return None
    return name


async def _delete_local_branch(repo: str, branch: str | None) -> bool:
    """删除工作树对应的本地分支（工作树摘除后调用）。

    - 跳过主分支（当前 HEAD 所在分支），避免把用户正在开发的主线删掉；
    - 分支仍被其他工作树占用时 `git branch -D` 会自行拒绝，这里只记录告警；
    - 删除分支失败不影响"工作树已删除"这一主结果，返回是否真正删掉。
    """
    if not branch:
        return False
    default = await _default_branch(repo)
    if branch == default or branch in ("main", "master"):
        logger.info("[worktree] 跳过删除主分支: %s", branch)
        return False
    ok, out, err = await _git(repo, "branch", "-D", branch)
    if not ok:
        logger.warning("[worktree] 删除分支失败 %s: %s", branch, (err or out)[:200])
        return False
    logger.info("[worktree] 已删除本地分支: %s", branch)
    return True


# 扫描子仓库时跳过的目录（构建产物/依赖/工具目录，避免误判与耗时）
_SKIP_DIRS = {
    "node_modules", "dist", "build", "target", "out", ".git",
    "venv", ".venv", "env", "__pycache__", "vendor", "bin", "obj",
}


async def _require_initial_commit(repo: str) -> None:
    """工作树必须有一个提交作为基点；空仓库给出可操作的提示。

    空仓库上 `git worktree add -b <新分支> <路径> <基点>` 无论基点写什么都不成立，
    与其让它抛 git 的原始错误（fatal: invalid reference），不如直接告诉用户该做什么。
    """
    ok, out, _ = await _git(repo, "rev-parse", "--verify", "HEAD")
    if ok and out.strip():
        return
    raise ValueError(
        "该项目仓库还没有任何提交，无法创建工作树。请先在项目目录完成一次提交"
        "（git add . && git commit -m \"init\"），或改用「新建会话」直接在该项目工作。"
    )


async def list_repo_candidates(db: AsyncSession, project_id: int) -> list[dict]:
    """列出可用于创建工作树的仓库（项目根 + 直接的子仓库）。

    plan-282-1441（#5）：支持"根目录是空仓库、真实代码在子仓库"的布局
    （如 F:\\project\\work 下有 clinic 与 clinicFrontEnd 两个独立仓库）。
    前端据此弹出勾选对话框，让用户选择在哪个（些）仓库建工作树。
    """
    from pathlib import Path as _Path

    project = await _project_of(db, project_id)
    if project is None:
        raise ValueError("项目不存在")
    root = _Path(project.path)
    if not root.is_dir():
        raise ValueError("项目目录不存在")

    out: list[dict] = []

    async def _describe(repo_path: str, is_root: bool) -> dict | None:
        top = await _repo_root_of(repo_path)
        if top is None:
            return None
        # 只认"仓库根"本身，避免把某仓库内部的子目录当成独立候选
        if _Path(top).resolve() != _Path(repo_path).resolve():
            return None
        has = await _has_commits(repo_path)
        branch = await _default_branch(repo_path) if has else ""
        return {
            "path": str(repo_path).replace("\\", "/"),
            "name": _Path(repo_path).name,
            "is_root": is_root,
            "has_commits": has,
            "branch": branch,
            "dirty": await _is_repo_dirty(repo_path) if has else False,
        }

    root_info = await _describe(project.path, True)
    if root_info is not None:
        out.append(root_info)

    try:
        children = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
    except OSError:
        children = []

    for child in children:
        if child.name in _SKIP_DIRS or child.name.startswith("."):
            continue
        info = await _describe(str(child), False)
        if info is not None:
            out.append(info)

    return out


async def create_worktree_for_project(
    db: AsyncSession, project_id: int, *, name: str | None = None, branch: str | None = None,
    repos: list[str] | None = None,
) -> dict:
    """为**项目**的选中仓库创建工作树，每个仓库登记为一个独立工作区（Project 行）。

    - 仓库选择：`repos` 为要建工作树的仓库绝对路径列表；为空时**自动探测**——
      优先使用有提交的子仓库（"根是空仓库、代码在子仓库"的布局），
      若没有子仓库则用项目根。
    - 目录：`<repo>/.chatcoder/worktrees/<name>`
    - 分支：默认 `chatcoder/<name>`，起点为该仓库当前分支
    - 登记：Project(is_worktree=True, parent_project_id=原项目)；多仓库时名称带仓库名
      （如 `dev-copy · clinic`）以便在侧栏区分。
    """
    import time

    from app.persistence.database import run_write_locked

    project = await _project_of(db, project_id)
    if project is None:
        raise ValueError("项目不存在")
    if getattr(project, "is_worktree", False):
        raise ValueError("工作树之下不能再创建工作树")

    wt_name = (name or f"wt-{int(time.time())}").strip()
    if not wt_name or any(c in wt_name for c in '\\/:*?"<>|'):
        raise ValueError("工作树名称非法（不能包含路径分隔符或特殊字符）")

    # ── 决定在哪些仓库上建 ──
    if repos:
        targets = [str(r).replace("\\", "/") for r in repos]
    else:
        candidates = await list_repo_candidates(db, project_id)
        usable = [c for c in candidates if c["has_commits"] and not c["is_root"]]
        if usable:
            targets = [c["path"] for c in usable]
        else:
            targets = [project.path]
    if not targets:
        raise ValueError("没有可用的仓库")

    multi = len(targets) > 1
    results: list[dict] = []

    for repo in targets:
        await _require_repo_path(repo)
        await _require_initial_commit(repo)

        base = _wt_dir_of(repo)
        base.mkdir(parents=True, exist_ok=True)
        # 把 .chatcoder/ 加入本地 exclude——否则主仓库会因工作树目录而恒被判为"有未提交变更"
        await _ensure_tool_dir_ignored(repo)

        wt_path = base / wt_name
        if wt_path.exists():
            raise ValueError(f"工作树目录已存在: {wt_path}")

        branch_name = branch or f"chatcoder/{wt_name}"
        base_branch = await _default_branch(repo)
        if not base_branch:
            raise ValueError(f"{repo} 没有可用的起始分支，无法创建工作树")

        # 分支已存在时直接拒绝：此前会退化为"检出该既有分支"，导致删除工作树时
        # 无法安全地连带删除分支（那可能是用户自己的分支）。宁可让用户换个名字。
        if await _branch_exists(repo, branch_name):
            raise ValueError(
                f"仓库 {_Path_name(repo)} 已存在分支 {branch_name}，请另填一个分支名后再创建工作树"
            )

        ok, out, err = await _git(repo, "worktree", "add", "-b", branch_name, str(wt_path), base_branch)
        if not ok:
            raise ValueError(f"在 {repo} 创建工作树失败: {(err or out)[:200]}")

        display_name = f"{wt_name} · {_Path_name(repo)}" if multi else wt_name

        def _create(s, _path=str(wt_path), _disp=display_name, _br=branch_name):
            from app.persistence.models.project import Project as _P
            row = _P(
                name=_disp,
                path=_path,
                is_worktree=True,
                parent_project_id=project.id,
                worktree_branch=_br,
                auto_scan_rules=False,
            )
            s.add(row)
            s.flush()
            new_id = row.id
            s.commit()
            return new_id

        new_id = await run_write_locked(_create, label=f"worktree.create_project.{project_id}")
        logger.info("项目 %s 在 %s 创建工作树 %s (branch=%s, project=%s)",
                    project_id, repo, wt_path, branch_name, new_id)
        results.append({
            "project_id": new_id, "path": str(wt_path),
            "branch": branch_name, "name": display_name, "repo": repo,
        })

    return {
        "ok": True,
        "count": len(results),
        "worktrees": results,
        # 兼容单仓库调用方的旧字段
        "project_id": results[0]["project_id"],
        "path": results[0]["path"],
        "branch": results[0]["branch"],
        "name": results[0]["name"],
    }


def _Path_name(path: str) -> str:
    """取路径末段（兼容两种分隔符）。"""
    return path.replace("\\", "/").rstrip("/").split("/")[-1]


async def _require_repo_path(repo: str) -> None:
    """校验给定路径本身是一个 git 仓库根。"""
    top = await _repo_root_of(repo)
    if top is None:
        raise ValueError(f"{repo} 不是 git 仓库")
    if top.replace("\\", "/").rstrip("/").lower() != repo.replace("\\", "/").rstrip("/").lower():
        raise ValueError(f"{repo} 不是仓库根目录（它属于 {top}）")


async def list_worktrees(db: AsyncSession, project_id: int | None = None) -> list[dict]:
    """列出工作树（可按主项目筛选），带 git 状态摘要。"""
    from sqlalchemy import select

    from app.persistence.models.project import Project as _P

    stmt = select(_P).where(_P.is_worktree == True)  # noqa: E712
    if project_id is not None:
        stmt = stmt.where(_P.parent_project_id == project_id)
    res = await db.execute(stmt.order_by(_P.id.asc()))
    rows = list(res.scalars().all())

    out: list[dict] = []
    for wt in rows:
        parent = await _project_of(db, wt.parent_project_id) if wt.parent_project_id else None
        info: dict = {
            "id": wt.id,
            "name": wt.name,
            "path": wt.path,
            "branch": wt.worktree_branch,
            "parent_project_id": wt.parent_project_id,
            "parent_path": parent.path if parent else None,
            "dirty": False,
            "ahead": 0,
            "behind": 0,
        }
        # git 状态：未提交变更数 + 相对主分支的领先/落后提交数
        if parent is not None:
            ok, status_out, _ = await _git(wt.path, "status", "--porcelain")
            if ok:
                info["dirty"] = bool(_status_lines(status_out))
            base_branch = await _default_branch(parent.path)
            ok, counts, _ = await _git(wt.path, "rev-list", "--left-right", "--count",
                                       f"{base_branch}...{wt.worktree_branch or 'HEAD'}")
            if ok:
                parts = counts.split()
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    info["behind"] = int(parts[0])
                    info["ahead"] = int(parts[1])
        out.append(info)
    return out


async def remove_worktree_project(db: AsyncSession, worktree_project_id: int, *, force: bool = False) -> dict:
    """删除工作树：移除 git worktree、**删除对应的本地分支**，再删除登记的 Project 行。"""
    from app.persistence.database import run_write_locked

    wt = await _project_of(db, worktree_project_id)
    if wt is None or not getattr(wt, "is_worktree", False):
        raise ValueError("工作树不存在")
    parent = await _project_of(db, wt.parent_project_id) if wt.parent_project_id else None
    repo = parent.path if parent else wt.path
    # 分支名优先取登记值；老数据缺字段时从工作树现场读取（摘除前才读得到）
    branch = wt.worktree_branch or await _branch_of_worktree(wt.path)

    if not force:
        ok, out, _ = await _git(wt.path, "status", "--porcelain")
        if ok and _status_lines(out):
            raise ValueError("工作树存在未提交变更，请先提交、或选择强制删除")

    # 先摘除 git 登记。--force 给两次：git ≥2.31 中第一次只覆盖"修改/未跟踪文件"，
    # 含**被忽略文件**（如 node_modules）的工作树要第二次才肯删。
    if force:
        args = ["worktree", "remove", "--force", "--force", wt.path]
    else:
        args = ["worktree", "remove", wt.path]
    ok, out, err = await _git(repo, *args)
    if not ok:
        # git 拒绝（被忽略文件 / 文件被占用 / 登记与目录不一致）→ 兜底清理：
        # 先 prune 掉 git 登记，再强删目录，避免"DB 记录删了、磁盘目录还在"。
        await _git(repo, "worktree", "prune")
        if not _force_rmtree(Path(wt.path)):
            raise ValueError(f"删除工作树目录失败（可能被其他程序占用）：{wt.path}")
        logger.warning("worktree remove 失败，已强制清理目录 %s: %s", wt.path, (err or out)[:200])

    # 工作树摘除后连带删除其本地分支——否则仓库里会残留一堆 chatcoder/xxx 分支
    branch_deleted = await _delete_local_branch(repo, branch)
    await _git(repo, "worktree", "prune")

    def _drop(s):
        from app.persistence.models.project import Project as _P
        row = s.get(_P, worktree_project_id)
        if row is not None:
            s.delete(row)
        s.commit()

    await run_write_locked(_drop, label=f"worktree.remove_project.{worktree_project_id}")
    logger.info("工作树已删除: id=%s path=%s branch=%s(删除=%s)",
                worktree_project_id, wt.path, branch, branch_deleted)
    return {"ok": True, "branch": branch, "branch_deleted": branch_deleted}


# ── 合并（双向：工作树 ⇄ 主工作区，基于**工作区当前内容**，含未提交改动）──
#
# 关键设计（修复"工作树里有未提交改动却提示无需合并"）：
#   旧实现用 `git diff base...branch` 比较的是**提交**，工作树里未提交的改动根本不在
#   比较范围内；且一进 preview 就因主工作区 dirty 直接报错（用户看到的正是这两点）。
#   现在改为对**工作区文件内容**做三方比较：
#     base   = merge-base 提交的内容（共同祖先）
#     ours   = 目标侧工作区当前文件内容（含未提交改动）
#     theirs = 来源侧工作区当前文件内容（含未提交改动）
#   两侧都改且不同时，用 `git merge-file` 尝试自动合并；仍冲突才交给用户/AI。

_DIR_TO_MAIN = "to_main"      # 工作树 → 主工作区
_DIR_FROM_MAIN = "from_main"  # 主工作区 → 工作树
_DIRECTIONS = (_DIR_TO_MAIN, _DIR_FROM_MAIN)


async def _show_text(repo: str, rev: str, rel: str) -> str | None:
    """取某提交中某文件的内容（不存在返回 None）。"""
    ok, out, _ = await _git(repo, "show", f"{rev}:{rel}")
    return out if ok else None


async def _ls_tree_files(repo: str, rev: str) -> set[str]:
    """某提交下的全部文件路径（相对仓库根）。"""
    ok, out, _ = await _git(repo, "ls-tree", "-r", "--name-only", rev)
    if not ok:
        return set()
    return {ln.strip().replace("\\", "/") for ln in out.splitlines() if ln.strip()}


async def _working_files(work_dir: str) -> set[str]:
    """工作区中的文件：已跟踪 + 未跟踪（排除本工具目录 .chatcoder）。"""
    files: set[str] = set()
    for args in (("ls-files",), ("ls-files", "--others", "--exclude-standard")):
        ok, out, _ = await _git(work_dir, *args)
        if not ok:
            continue
        for ln in out.splitlines():
            p = ln.strip().replace("\\", "/")
            if p and not (p == _TOOL_DIR or p.startswith(_TOOL_DIR + "/")):
                files.add(p)
    return files


def _read_work_file(work_dir: str, rel: str) -> str | None:
    """读工作区中的文件内容（不存在 / 读失败返回 None）。

    统一按 LF 归一化（universal newlines），使 base/ours/theirs 三侧口径一致，
    避免 Windows 上 CRLF 文件被误判为"两侧都改"。
    """
    p = Path(work_dir) / rel
    try:
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _write_work_file(work_dir: str, rel: str, content: str) -> None:
    """写工作区文件，**保留原文件的换行风格**（Windows 下多为 CRLF）。

    内部内容一律为 LF；若目标是 CRLF 文件则转换回去，避免一次合并把整份文件的
    行尾全部改写、产生"整文件重写"的脏 diff。
    """
    p = Path(work_dir) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    nl = "\n"
    try:
        if p.is_file() and b"\r\n" in p.read_bytes():
            nl = "\r\n"
    except OSError:
        nl = "\n"
    data = content if nl == "\n" else content.replace("\n", "\r\n")
    p.write_bytes(data.encode("utf-8"))


async def _merge_file_text(ours: str, base: str, theirs: str) -> tuple[bool, str]:
    """`git merge-file` 三方合并单文件内容；返回 (是否无冲突, 合并结果)。

    merge-file 不需要仓库，用一个临时目录存放三份文本即可。返回码为冲突数量
    （0 表示干净合并），冲突时 stdout 已带 `<<<<<<< / ======= / >>>>>>>` 标记。
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="chatcoder-merge-") as td:
        fo = Path(td) / "ours.txt"
        fb = Path(td) / "base.txt"
        ft = Path(td) / "theirs.txt"
        # newline="\n"：不做换行转换，确保输入/输出都是 LF（Windows 下默认会转成
        # CRLF，导致合并结果被写回时行尾翻倍或整文件重写）。
        fo.write_text(ours, encoding="utf-8", newline="\n")
        fb.write_text(base, encoding="utf-8", newline="\n")
        ft.write_text(theirs, encoding="utf-8", newline="\n")
        ok, out, _err = await _git(
            td, "merge-file", "-p", "--diff3",
            "-L", "主工作区(ours)", "-L", "base", "-L", "工作树(theirs)",
            str(fo), str(fb), str(ft),
        )
    return ok, (out or "").replace("\r\n", "\n")


def _resolve_single(o: str | None, b: str | None, t: str | None,
                    merged_text: str | None) -> tuple[bool, str | None]:
    """两侧都改时的冲突判定与合并结果组装。"""
    if b is None and o is not None and t is not None:
        return True, t                     # 两侧各自新增，内容不同
    if o is None or t is None:
        return True, t                     # 一方删除、另一方修改
    return merged_text is None, (merged_text or t)


async def _merge_sides(db: AsyncSession, worktree_project_id: int, direction: str) -> dict:
    """解析合并双方目录与共同祖先基点。"""
    if direction not in _DIRECTIONS:
        raise ValueError(f"不支持的合并方向: {direction}")
    wt = await _project_of(db, worktree_project_id)
    if wt is None or not getattr(wt, "is_worktree", False):
        raise ValueError("工作树不存在")
    parent = await _project_of(db, wt.parent_project_id) if wt.parent_project_id else None
    if parent is None:
        raise ValueError("找不到主工作区")

    repo = parent.path
    base_branch = await _default_branch(repo)
    branch = wt.worktree_branch or "HEAD"
    ok, mb, _ = await _git(repo, "merge-base", base_branch, branch)
    base_rev = mb.strip() if ok and mb.strip() else base_branch

    if direction == _DIR_TO_MAIN:
        ours_dir, theirs_dir, target_dir = parent.path, wt.path, parent.path
    else:
        ours_dir, theirs_dir, target_dir = wt.path, parent.path, wt.path

    return {
        "wt": wt, "parent": parent, "repo": repo,
        "base_branch": base_branch, "branch": branch, "base_rev": base_rev,
        "ours_dir": ours_dir, "theirs_dir": theirs_dir, "target_dir": target_dir,
        "direction": direction,
    }


async def merge_preview(db: AsyncSession, worktree_project_id: int,
                        *, direction: str = _DIR_TO_MAIN) -> dict:
    """差异文件列表（含**未提交改动**与自动冲突解决结果）。不改动任何工作区。

    不再因主工作区 dirty 直接报错——主工作区的未提交改动会作为 ours 参与三方比较，
    能自动合的自动合，合不了的标记为冲突交前端处理。
    """
    ctx = await _merge_sides(db, worktree_project_id, direction)
    base_files = await _ls_tree_files(ctx["repo"], ctx["base_rev"])
    theirs_files = await _working_files(ctx["theirs_dir"])
    ours_files = await _working_files(ctx["ours_dir"])

    files: list[dict] = []
    for rel in sorted(base_files | theirs_files | ours_files):
        b = await _show_text(ctx["repo"], ctx["base_rev"], rel) if rel in base_files else None
        t = _read_work_file(ctx["theirs_dir"], rel) if rel in theirs_files else None
        if b == t:
            continue  # 来源侧相对 base 无改动 → 不在本次合并范围
        o = _read_work_file(ctx["ours_dir"], rel) if rel in ours_files else None
        if o == t:
            continue  # 两侧内容已一致 → 无需处理

        status = "deleted" if t is None else ("added" if b is None else "modified")
        if o == b:
            conflict, merged = False, t            # 仅来源侧改动：直接采用
        elif b is None or o is None or t is None:
            conflict, merged = _resolve_single(o, b, t, None)
        else:
            ok_m, merged_out = await _merge_file_text(o, b, t)
            conflict = not ok_m
            merged = merged_out or t

        files.append({
            "path": rel, "status": status, "conflict": conflict,
            "merged": merged, "has_auto_merge": not conflict,
        })

    return {
        "ok": True,
        "direction": direction,
        "base_branch": ctx["base_branch"],
        "branch": ctx["branch"],
        "source_dirty": await _is_repo_dirty(ctx["theirs_dir"]),
        "target_dirty": await _is_repo_dirty(ctx["target_dir"]),
        "files": files,
        "has_conflict": any(f["conflict"] for f in files),
    }


async def merge_file_blobs(db: AsyncSession, worktree_project_id: int, path: str,
                           *, direction: str = _DIR_TO_MAIN) -> dict:
    """三路内容：base（共同祖先提交）/ ours（目标侧工作区）/ theirs（来源侧工作区）。

    三方都取**工作区当前内容**（含未提交改动），与 preview/apply 口径一致——
    否则会出现"预览能看到的改动，打开文件却看不到"的错位。
    """
    ctx = await _merge_sides(db, worktree_project_id, direction)
    rel = path.replace("\\", "/")

    base_text = await _show_text(ctx["repo"], ctx["base_rev"], rel)
    ours_text = _read_work_file(ctx["ours_dir"], rel)
    theirs_text = _read_work_file(ctx["theirs_dir"], rel)
    return {
        "ok": True,
        "path": rel,
        "base": base_text,
        "ours": ours_text,
        "theirs": theirs_text,
        "base_rev": ctx["base_rev"],
        "ours_rev": ctx["base_branch"] if direction == _DIR_TO_MAIN else ctx["branch"],
        "theirs_rev": ctx["branch"] if direction == _DIR_TO_MAIN else ctx["base_branch"],
    }


async def merge_apply(db: AsyncSession, worktree_project_id: int, files: list[dict],
                      *, direction: str = _DIR_TO_MAIN) -> dict:
    """应用合并结果：把解决后的内容写入**目标侧**工作区并提交。

    files: [{path, content, deleted?}] —— content 为最终文本（deleted 表示删除）。
    目标侧原有的其他未提交改动不受影响（只 add 本次写入的文件，不做 `add -A`）。
    """
    ctx = await _merge_sides(db, worktree_project_id, direction)
    wt = ctx["wt"]
    target_dir = ctx["target_dir"]
    branch = ctx["branch"]
    label = "合并到主工作区" if direction == _DIR_TO_MAIN else "更新到工作树"

    written: list[str] = []
    for f in files:
        rel = str(f.get("path") or "").replace("\\", "/")
        if not rel:
            continue
        target = Path(target_dir) / rel
        # 路径越界防护：必须落在目标工作区内
        try:
            resolved = target.resolve()
            root = Path(target_dir).resolve()
            if resolved != root and root not in resolved.parents:
                raise ValueError(f"路径越界: {rel}")
        except OSError:
            raise ValueError(f"路径非法: {rel}")
        if f.get("deleted"):
            if target.exists():
                target.unlink()
            written.append(rel)
            continue
        content = f.get("content")
        if content is None:
            continue
        _write_work_file(target_dir, rel, str(content))
        written.append(rel)

    if not written:
        return {"ok": True, "committed": False, "written": []}

    # 只提交本次写入的文件（不用 add -A），避免把目标侧原有未提交改动一并卷进来
    await _git(target_dir, "add", "--", *written)
    dst = "主工作区" if direction == _DIR_TO_MAIN else f"工作树 {wt.name}"
    src = f"工作树 {wt.name}" if direction == _DIR_TO_MAIN else "主工作区"
    msg = f"merge(worktree): {src} → {dst}（{len(written)} 个文件）"
    ok, out, err = await _git(target_dir, "-c", "user.name=chatcoder", "-c", "user.email=chatcoder@local",
                              "commit", "-m", msg)
    if not ok and "nothing to commit" not in (out + err):
        raise ValueError(f"提交合并结果失败: {(err or out)[:200]}")

    logger.info("%s：工作树 %s(%s) 方向=%s，%d 个文件",
                label, wt.id, branch, direction, len(written))
    return {"ok": True, "committed": True, "written": written}


async def commit_worktree(db: AsyncSession, worktree_project_id: int, *,
                          side: str = "worktree", message: str | None = None) -> dict:
    """提交某一侧的未提交改动（side=worktree 提交工作树，side=main 提交主工作区）。

    合并前若来源侧有未提交改动，前端会先弹提示；用户确认后调用本接口自动提交，
    使这些改动成为正式提交后再走合并流程。
    """
    ctx = await _merge_sides(db, worktree_project_id, _DIR_TO_MAIN)
    target_dir = ctx["wt"].path if side == "worktree" else ctx["parent"].path
    if not await _is_repo_dirty(target_dir):
        return {"ok": True, "committed": False, "message": "没有需要提交的改动"}

    what = f"工作树 {ctx['wt'].name}" if side == "worktree" else "主工作区"
    msg = message or f"chore(worktree): 提交{what}的改动（合并前自动提交）"
    await _git(target_dir, "add", "-A")
    ok, out, err = await _git(target_dir, "-c", "user.name=chatcoder", "-c", "user.email=chatcoder@local",
                              "commit", "-m", msg)
    if not ok and "nothing to commit" not in (out + err):
        raise ValueError(f"提交失败: {(err or out)[:200]}")
    logger.info("工作树 %s：已提交 %s 的改动", ctx["wt"].id, what)
    return {"ok": True, "committed": True, "message": f"已提交{what}的改动"}


async def ai_merge_suggest(db: AsyncSession, worktree_project_id: int, path: str,
                           hunk: dict | None = None, *, direction: str = _DIR_TO_MAIN,
                           model_id: int | None = None) -> dict:
    """针对某文件（或某个冲突块）给出 AI 合并建议。

    - `model_id`：用户在合并弹窗中自选的模型（可为任意供应商下的模型）；不传时
      回退到服务端默认 provider，再回退到库中第一个启用的模型。
    - 失败时返回 ok=False 并带可读原因，前端据此提示"AI 建议不可用，请手动解决"。
    """
    try:
        blobs = await merge_file_blobs(db, worktree_project_id, path, direction=direction)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    ours = blobs.get("ours") or ""
    theirs = blobs.get("theirs") or ""
    base = blobs.get("base") or ""

    # 仅把冲突块（或整个文件）送模型，控制 token
    focus = ""
    if hunk and isinstance(hunk, dict):
        focus = (
            f"【仅处理此冲突块】\n"
            f"主工作区(ours)：\n{hunk.get('ours', '')}\n"
            f"工作树(theirs)：\n{hunk.get('theirs', '')}\n"
        )

    prompt = (
        "你是代码合并助手。下面是同一个文件的三个版本：共同祖先(base)、"
        "主工作区(ours)、工作树(theirs)。请给出**合并后的完整内容**，"
        "保留双方的有效改动、消除冲突标记，不要添加解释文字。\n\n"
        f"{focus}"
        f"--- base ---\n{base[:6000]}\n\n"
        f"--- ours ---\n{ours[:6000]}\n\n"
        f"--- theirs ---\n{theirs[:6000]}\n"
    )

    try:
        from app.models.registry import get_model_registry
        from app.models.schemas import ChatMessage, ChatRequest
        from sqlalchemy import select as _select

        from app.persistence.models.model_reg import Model

        registry = get_model_registry()
        provider = None
        model_name = ""
        # 1) 用户显式指定模型：按其供应商/凭据解析（这就是"自己选供应商的模型"）
        if model_id:
            row = await db.get(Model, model_id)
            if row is None:
                return {"ok": False, "error": f"模型不存在: #{model_id}"}
            provider, reason = await registry.get_provider_for_model(db, row)
            if provider is None:
                return {"ok": False, "error": f"所选模型不可用（{reason}），请在模型设置中检查该供应商"}
            model_name = getattr(row, "name", "") or ""
        # 2) 未指定：服务端默认 provider
        if provider is None:
            provider = registry.get_default_provider()
        # 3) 兜底：库中第一个启用且可用的模型
        if provider is None:
            res = await db.execute(
                _select(Model).where(Model.is_active == True).order_by(Model.id.asc())  # noqa: E712
            )
            for row in res.scalars().all():
                p, _reason = await registry.get_provider_for_model(db, row)
                if p is not None:
                    provider, model_name = p, (getattr(row, "name", "") or "")
                    break
        if provider is None:
            return {"ok": False, "error": "未配置可用模型，无法生成 AI 建议。请在「设置 → 模型」中添加模型后重试"}

        request = ChatRequest(
            messages=[ChatMessage(role="user", content=prompt)],
            model=model_name,
            temperature=0.0,
        )
        resp = await provider.chat(request)
        merged = (resp.content or "").strip()
        if not merged:
            return {"ok": False, "error": "AI 未返回内容"}
        # 去掉模型可能包裹的代码围栏，直接给可用的文件内容
        if merged.startswith("```"):
            lines = merged.splitlines()
            if len(lines) >= 2 and lines[-1].strip().startswith("```"):
                merged = "\n".join(lines[1:-1])
        return {"ok": True, "suggestion": merged, "model": model_name}
    except Exception as e:  # noqa: BLE001 —— 建议失败不应影响合并主流程
        logger.warning("[worktree] AI 合并建议失败 path=%s", path, exc_info=True)
        return {"ok": False, "error": f"AI 建议失败: {e}"}

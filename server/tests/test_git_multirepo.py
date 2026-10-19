"""Git 多仓库与多仓库工作树（plan-20261019-git-multirepo）的真实 git 行为测试。

在临时目录里真的初始化「根目录不是仓库、frontend/ 与 backend/ 各是一个仓库」的布局：
仓库识别、按 repo 参数定位、汇总状态，以及工作树组的创建 / 统计 / 合并 / 删除 / 回滚。
mock 掉 git 就测不到参数与输出解析是否正确，因此这里不 mock。
"""
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models import Project, Session  # noqa: F401 注册模型
from app.services import git_service, worktree_service
from app.services.git_service import GitError


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr or proc.stdout}")
    return proc.stdout


def _init_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@t.local")
    _git(path, "config", "user.name", "t")
    for name, content in (files or {"README.md": "init\n"}).items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """根目录不是仓库：frontend/、backend/ 两个仓库，apps/web 是第二层的仓库；依赖与隐藏目录里的仓库要跳过。"""
    root = tmp_path / "proj"
    root.mkdir()
    _init_repo(root / "frontend", {"src/app.ts": "export const a = 1;\n"})
    _init_repo(root / "backend", {"main.py": "print('hi')\n"})
    _init_repo(root / "apps" / "web", {"index.html": "<html></html>\n"})
    _init_repo(root / "node_modules" / "pkg")
    _init_repo(root / ".tools" / "x")
    return root


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/mr.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


async def _seed_project(db, path: Path) -> int:
    p = Project(name=path.name, path=str(path))
    db.add(p)
    await db.commit()
    return p.id


# ── 仓库识别与定位 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_repos_finds_children_and_skips_deps(workspace):
    repos = await git_service.discover_repos(str(workspace))
    assert [r["rel"] for r in repos] == ["apps/web", "backend", "frontend"]
    assert all(r["branch"] == "main" and r["changes"] == 0 and not r["is_root"] for r in repos)

    (workspace / "frontend" / "src" / "app.ts").write_text("export const a = 2;\n", encoding="utf-8")
    (workspace / "frontend" / "new.ts").write_text("x\n", encoding="utf-8")
    front = next(r for r in await git_service.discover_repos(str(workspace)) if r["rel"] == "frontend")
    assert front["changes"] == 2


@pytest.mark.asyncio
async def test_discover_repos_root_repo_with_nested_repo(tmp_path):
    root = _init_repo(tmp_path / "mono", {"a.txt": "a\n"})
    _init_repo(root / "sub")
    repos = await git_service.discover_repos(str(root))
    assert [r["rel"] for r in repos] == ["", "sub"]
    assert repos[0]["is_root"] is True


@pytest.mark.asyncio
async def test_project_inside_repo_is_single_repo(tmp_path):
    root = _init_repo(tmp_path / "big", {"pkg/a.txt": "a\n"})
    dirs = await git_service.repo_dirs(str(root / "pkg"))
    assert dirs == [("", str(root / "pkg"))]


def test_repo_dir_validates(workspace):
    assert git_service.repo_dir(str(workspace), None) == str(workspace)
    assert git_service.repo_dir(str(workspace), "") == str(workspace)
    assert Path(git_service.repo_dir(str(workspace), "frontend")) == workspace / "frontend"
    assert Path(git_service.repo_dir(str(workspace), "apps/web/")) == workspace / "apps" / "web"
    for bad in ("../x", "C:/Windows", "/etc", "apps"):
        with pytest.raises(GitError):
            git_service.repo_dir(str(workspace), bad)


@pytest.mark.asyncio
async def test_status_all_prefixes_paths_with_repo(workspace):
    (workspace / "backend" / "main.py").write_text("print('changed')\n", encoding="utf-8")
    (workspace / "frontend" / "b.ts").write_text("b\n", encoding="utf-8")
    st = await git_service.status_all(str(workspace))
    assert {e["path"] for e in st["unstaged"]} == {"backend/main.py"}
    assert {e["path"] for e in st["untracked"]} == {"frontend/b.ts"}
    assert st["clean"] is False and st["branch"] is None
    assert {r["rel"] for r in st["repos"]} == {"apps/web", "backend", "frontend"}


@pytest.mark.asyncio
async def test_status_all_single_repo_keeps_branch(tmp_path):
    root = _init_repo(tmp_path / "solo")
    st = await git_service.status_all(str(root))
    assert st["branch"] == "main" and st["clean"] is True


# ── 工作树组 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_worktree_lifecycle(db, workspace):
    pid = await _seed_project(db, workspace)
    front, back = str(workspace / "frontend"), str(workspace / "backend")
    res = await worktree_service.create_worktree_for_project(
        db, pid, name="fullstack", repos=[front, back])
    group = Path(res["path"])
    assert res["count"] == 1 and group == workspace / ".chatcoder" / "worktrees" / "fullstack"
    assert {r["rel"] for r in res["repos"]} == {"frontend", "backend"}
    assert (group / "frontend" / "src" / "app.ts").exists() and (group / "backend" / "main.py").exists()
    rows = (await db.execute(select(Project).where(Project.is_worktree == True))).scalars().all()  # noqa: E712
    assert len(rows) == 1, "多仓库工作树应登记为一个工作区"

    members = await worktree_service.worktree_members(str(group))
    assert {m["rel"]: Path(m["source"]) for m in members} == {
        "backend": workspace / "backend", "frontend": workspace / "frontend"}
    # Git 面板在工作树组里同样能识别出两个仓库
    assert [r["rel"] for r in await git_service.discover_repos(str(group))] == ["backend", "frontend"]

    # 改前端：统计按成员汇总；合并必须指定仓库
    (group / "frontend" / "src" / "app.ts").write_text("export const a = 42;\n", encoding="utf-8")
    listed = await worktree_service.list_worktrees(db, project_id=pid)
    assert listed[0]["dirty"] is True
    assert {r["rel"]: r["dirty"] for r in listed[0]["repos"]} == {"backend": False, "frontend": True}
    with pytest.raises(ValueError, match="多个仓库"):
        await worktree_service.merge_preview(db, res["project_id"])
    pv = await worktree_service.merge_preview(db, res["project_id"], repo="frontend")
    assert [f["path"] for f in pv["files"]] == ["src/app.ts"]
    await worktree_service.merge_apply(
        db, res["project_id"], [{"path": "src/app.ts", "content": pv["files"][0]["merged"]}], repo="frontend")
    assert "42" in (workspace / "frontend" / "src" / "app.ts").read_text(encoding="utf-8")
    assert "print('hi')" in (workspace / "backend" / "main.py").read_text(encoding="utf-8")

    # 删除：逐个摘除成员、删除各仓库的分支、清掉组目录
    out = await worktree_service.remove_worktree_project(db, res["project_id"], force=True)
    assert out["ok"] and out["branch_deleted"] is True
    assert not group.exists()
    for repo in (front, back):
        assert "chatcoder/fullstack" not in _git(Path(repo), "branch", "--list")
        assert len(_git(Path(repo), "worktree", "list").strip().splitlines()) == 1


@pytest.mark.asyncio
async def test_group_with_root_repo_excludes_nested(db, tmp_path):
    root = _init_repo(tmp_path / "mono", {"a.txt": "a\n"})
    _init_repo(root / "sub", {"s.txt": "s\n"})
    pid = await _seed_project(db, root)
    res = await worktree_service.create_worktree_for_project(
        db, pid, name="g", repos=[str(root), str(root / "sub")])
    group = Path(res["path"])
    assert (group / "a.txt").exists() and (group / "sub" / "s.txt").exists()
    members = {m["rel"] for m in await worktree_service.worktree_members(str(group))}
    assert members == {"", "sub"}
    exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".chatcoder/" in exclude and "/sub/" in exclude
    # 根成员用 "." 指定；根仓库的工作树状态不应把子仓库当成未跟踪内容
    pv = await worktree_service.merge_preview(db, res["project_id"], repo=".")
    assert pv["files"] == []
    await worktree_service.remove_worktree_project(db, res["project_id"], force=True)
    assert not group.exists()


@pytest.mark.asyncio
async def test_group_creation_rolls_back_on_failure(db, workspace):
    pid = await _seed_project(db, workspace)
    _git(workspace / "backend", "branch", "chatcoder/dup")
    with pytest.raises(ValueError, match="已存在分支"):
        await worktree_service.create_worktree_for_project(
            db, pid, name="dup", repos=[str(workspace / "frontend"), str(workspace / "backend")])
    assert not (workspace / ".chatcoder" / "worktrees" / "dup").exists()
    assert "chatcoder/dup" not in _git(workspace / "frontend", "branch", "--list"), "已建好的成员必须回滚"
    rows = (await db.execute(select(Project).where(Project.is_worktree == True))).scalars().all()  # noqa: E712
    assert rows == []


@pytest.mark.asyncio
async def test_single_subrepo_worktree_merges_against_its_own_repo(db, workspace):
    """只选一个子仓库：工作树建在该子仓库下；合并以子仓库为源（此前错用父项目目录）。"""
    pid = await _seed_project(db, workspace)
    res = await worktree_service.create_worktree_for_project(
        db, pid, name="fe", repos=[str(workspace / "frontend")])
    wt = Path(res["path"])
    assert wt == workspace / "frontend" / ".chatcoder" / "worktrees" / "fe"
    (wt / "src" / "app.ts").write_text("export const a = 7;\n", encoding="utf-8")
    pv = await worktree_service.merge_preview(db, res["project_id"])
    assert [f["path"] for f in pv["files"]] == ["src/app.ts"]
    listed = await worktree_service.list_worktrees(db, project_id=pid)
    assert listed[0]["dirty"] is True and listed[0]["repos"][0]["rel"] == ""
    out = await worktree_service.remove_worktree_project(db, res["project_id"], force=True)
    assert out["branch_deleted"] is True
    assert "chatcoder/fe" not in _git(workspace / "frontend", "branch", "--list")


@pytest.mark.asyncio
async def test_session_worktree_for_multirepo_project(db, workspace):
    pid = await _seed_project(db, workspace)
    s = Session(project_id=pid, title="s")
    db.add(s)
    await db.commit()
    res = await worktree_service.create_worktree(db, s.id)
    group = Path(res["path"])
    assert {m["rel"] for m in await worktree_service.worktree_members(str(group))} == {
        "apps/web", "backend", "frontend"}
    await db.refresh(s)
    out = await worktree_service.remove_worktree(db, s.id)
    assert out["ok"] and not group.exists()
    assert "chatcoder/session-" not in _git(workspace / "backend", "branch", "--list")


@pytest.mark.asyncio
async def test_repo_candidates_cover_second_level(db, workspace):
    pid = await _seed_project(db, workspace)
    cands = await worktree_service.list_repo_candidates(db, pid)
    assert [c["rel"] for c in cands] == ["apps/web", "backend", "frontend"]
    assert all(c["has_commits"] for c in cands)

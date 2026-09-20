"""工作树的真实 git 行为测试（plan-282-1441 #5）。

与纯逻辑单测不同：这里在临时目录里**真的**初始化 git 仓库、创建 worktree、
制造冲突、走三路读取与合并提交——因为该功能的核心风险在于 git 命令的参数
与输出解析是否正确，mock 掉 git 就失去意义。
"""
import asyncio
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models import Project, Session  # noqa: F401 注册模型
from app.services import worktree_service


def _git(cwd: Path, *args: str) -> str:
    """同步跑 git（测试内使用，简单直接）。"""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr or proc.stdout}")
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个已提交初始内容的 git 仓库。"""
    r = tmp_path / "main"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.local")
    _git(r, "config", "user.name", "t")
    (r / "app.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/wt.db"
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


async def _seed_project(db, repo: Path) -> int:
    p = Project(name="main", path=str(repo))
    db.add(p)
    await db.commit()
    return p.id


@pytest.mark.asyncio
async def test_create_and_list_worktree(db, repo):
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-a")
    assert res["ok"] is True
    assert Path(res["path"]).is_dir(), "工作树目录应真实创建"
    assert res["branch"] == "chatcoder/wt-a"

    # 登记为独立工作区（Project 行），这样左侧面板能显示、可在其中建会话
    listed = await worktree_service.list_worktrees(db, project_id=pid)
    assert len(listed) == 1
    assert listed[0]["name"] == "wt-a"
    assert listed[0]["dirty"] is False


async def _commit_in(wt_path: str, content: str, msg: str) -> None:
    p = Path(wt_path)
    (p / "app.txt").write_text(content, encoding="utf-8")
    _git(p, "add", "-A")
    _git(p, "commit", "-m", msg)


@pytest.mark.asyncio
async def test_merge_preview_detects_change_and_file_blobs(db, repo):
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-b")
    await _commit_in(res["path"], "line1\nCHANGED-IN-WORKTREE\nline3\n", "wt change")

    preview = await worktree_service.merge_preview(db, res["project_id"])
    assert preview["ok"] is True
    paths = [f["path"] for f in preview["files"]]
    assert "app.txt" in paths, f"差异文件列表应包含 app.txt，实际 {paths}"

    blobs = await worktree_service.merge_file_blobs(db, res["project_id"], "app.txt")
    assert blobs["theirs"] and "CHANGED-IN-WORKTREE" in blobs["theirs"]
    assert blobs["ours"] and "CHANGED-IN-WORKTREE" not in blobs["ours"]
    assert blobs["base"] is not None


@pytest.mark.asyncio
async def test_merge_apply_writes_to_main_repo(db, repo):
    """合并提交：解决后的内容必须真的落到主工作区。"""
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-c")
    await _commit_in(res["path"], "line1\nMERGED\nline3\n", "wt change")

    apply_res = await worktree_service.merge_apply(
        db, res["project_id"], [{"path": "app.txt", "content": "line1\nMERGED\nline3\n"}],
    )
    assert apply_res["ok"] is True and apply_res["committed"] is True
    assert (repo / "app.txt").read_text(encoding="utf-8") == "line1\nMERGED\nline3\n"
    # 主工作区应保持干净（已提交）
    assert _git(repo, "status", "--porcelain").strip() == ""


@pytest.mark.asyncio
async def test_remove_worktree_refuses_dirty_then_force(db, repo):
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-d")
    # 制造未提交变更
    (Path(res["path"]) / "app.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError):
        await worktree_service.remove_worktree_project(db, res["project_id"], force=False)

    await worktree_service.remove_worktree_project(db, res["project_id"], force=True)
    assert await worktree_service.list_worktrees(db, project_id=pid) == []
    # 目录必须真的从磁盘消失（此前只 prune 掉 git 登记，文件夹留在原地）
    assert not Path(res["path"]).exists(), "删除工作树后目录不应残留"


@pytest.mark.asyncio
async def test_remove_worktree_cleans_ignored_files(db, repo):
    """工作树内有被忽略文件（如 node_modules）时，删除也必须把目录清干净。

    git ≥2.31 对含 ignored 文件的工作树需要 `--force --force`；此前仅一次 --force
    会失败、代码只做 prune 就删库记录，导致磁盘上留下整个目录。
    """
    pid = await _seed_project(db, repo)
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "ignore node_modules")

    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-ignored")
    wt_path = Path(res["path"])
    (wt_path / "node_modules").mkdir()
    (wt_path / "node_modules" / "pkg.js").write_text("x\n", encoding="utf-8")

    await worktree_service.remove_worktree_project(db, res["project_id"], force=False)
    assert not wt_path.exists(), "含被忽略文件的工作树删除后目录不应残留"

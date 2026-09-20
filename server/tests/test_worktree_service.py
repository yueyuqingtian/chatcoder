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
async def test_create_refuses_existing_branch(db, repo):
    """分支已存在时必须报错，而不是退化为检出该既有分支。

    否则删除工作树时无法安全地连带删除分支——那可能是用户自己的分支。
    """
    _git(repo, "branch", "chatcoder/wt-dup")
    pid = await _seed_project(db, repo)
    with pytest.raises(ValueError) as ei:
        await worktree_service.create_worktree_for_project(
            db, pid, name="wt-dup", branch="chatcoder/wt-dup")
    assert "已存在分支" in str(ei.value)


@pytest.mark.asyncio
async def test_remove_worktree_deletes_local_branch(db, repo):
    """删除工作树时必须连带删除其本地分支，否则仓库残留 chatcoder/xxx。"""
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-br")
    branch = res["branch"]
    assert branch in _git(repo, "branch", "--list", branch)

    out = await worktree_service.remove_worktree_project(db, res["project_id"], force=True)
    assert out["ok"] is True and out["branch"] == branch
    assert out["branch_deleted"] is True
    # 分支必须真的从仓库消失
    assert branch not in _git(repo, "branch", "--list", branch)
    assert await worktree_service.list_worktrees(db, project_id=pid) == []


@pytest.mark.asyncio
async def test_remove_worktree_keeps_default_branch(db, repo):
    """工作树检出到主分支时（异常情形）不得把主分支删掉。"""
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-main")
    # 模拟登记分支 == 仓库主分支
    from app.persistence.models.project import Project as _P
    row = await db.get(_P, res["project_id"])
    row.worktree_branch = "main"
    await db.commit()

    out = await worktree_service.remove_worktree_project(db, res["project_id"], force=True)
    assert out["branch_deleted"] is False
    assert "main" in _git(repo, "branch", "--list", "main")


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


# ── 合并（含**未提交改动**，修复"工作树改了却提示无需合并"）──


@pytest.mark.asyncio
async def test_merge_preview_sees_uncommitted_worktree_change(db, repo):
    """工作树里**未提交**的改动也必须被合并预览看到。

    旧实现比较的是提交（git diff base...branch），未提交改动不在范围内，
    因此用户改了文件却被告知"工作树与主工作区没有差异，无需合并"。
    """
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-uncommitted")
    # 只改文件、**不提交**
    (Path(res["path"]) / "app.txt").write_text("line1\nDIRTY-WT\nline3\n", encoding="utf-8")

    preview = await worktree_service.merge_preview(db, res["project_id"])
    paths = [f["path"] for f in preview["files"]]
    assert "app.txt" in paths, f"未提交的改动也应出现在差异列表，实际 {paths}"
    row = next(f for f in preview["files"] if f["path"] == "app.txt")
    assert row["conflict"] is False, "仅来源侧改动应可自动合并"
    assert "DIRTY-WT" in (row["merged"] or "")


@pytest.mark.asyncio
async def test_merge_apply_writes_uncommitted_change(db, repo):
    """应用后未提交的改动必须真的落到主工作区。"""
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-apply-dirty")
    (Path(res["path"]) / "app.txt").write_text("line1\nAPPLIED\nline3\n", encoding="utf-8")

    preview = await worktree_service.merge_preview(db, res["project_id"])
    files = [{"path": f["path"], "content": f["merged"]} for f in preview["files"]]
    apply_res = await worktree_service.merge_apply(db, res["project_id"], files)
    assert apply_res["ok"] is True and apply_res["committed"] is True
    assert (repo / "app.txt").read_text(encoding="utf-8") == "line1\nAPPLIED\nline3\n"


@pytest.mark.asyncio
async def test_merge_preview_allows_dirty_main_repo(db, repo):
    """主工作区有未提交改动时**不再直接报错**：其改动作为 ours 参与三方比较。"""
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-dirty-main")
    await _commit_in(res["path"], "line1\nWT\nline3\n", "wt change")
    # 主工作区改另一个文件（未提交），不应阻塞合并
    (repo / "other.txt").write_text("main dirty\n", encoding="utf-8")

    preview = await worktree_service.merge_preview(db, res["project_id"])
    assert preview["ok"] is True
    assert "app.txt" in [f["path"] for f in preview["files"]]


@pytest.mark.asyncio
async def test_merge_from_main_updates_worktree(db, repo):
    """反向：把主工作区的改动更新到工作树（direction=from_main）。"""
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-from-main")
    # 主工作区提交一个改动
    (repo / "main_only.txt").write_text("from main\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main change")

    preview = await worktree_service.merge_preview(db, res["project_id"], direction="from_main")
    paths = [f["path"] for f in preview["files"]]
    assert "main_only.txt" in paths, f"主工作区的新文件应出现在反向差异中，实际 {paths}"

    files = [{"path": f["path"], "content": f["merged"]} for f in preview["files"]]
    await worktree_service.merge_apply(db, res["project_id"], files, direction="from_main")
    assert (Path(res["path"]) / "main_only.txt").exists(), "反向合并后工作树应拿到主工作区的文件"


@pytest.mark.asyncio
async def test_merge_apply_keeps_unrelated_dirty_files(db, repo):
    """合并只提交本次涉及的文件，不把目标侧无关的未提交改动一并卷进提交。"""
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-keep-dirty")
    await _commit_in(res["path"], "line1\nMERGED\nline3\n", "wt change")
    # 主工作区有一个无关文件的未提交改动
    (repo / "unrelated.txt").write_text("keep me dirty\n", encoding="utf-8")

    preview = await worktree_service.merge_preview(db, res["project_id"])
    files = [{"path": f["path"], "content": f["merged"]} for f in preview["files"]]
    await worktree_service.merge_apply(db, res["project_id"], files)

    # 无关文件仍是未提交状态（未被卷入合并提交）
    st = _git(repo, "status", "--porcelain")
    assert "unrelated.txt" in st, f"无关未提交改动不应被提交，实际状态: {st}"


@pytest.mark.asyncio
async def test_commit_worktree_side(db, repo):
    """合并前自动提交：commit_worktree(side=worktree) 应把工作树改动提交掉。"""
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-commit")
    (Path(res["path"]) / "app.txt").write_text("line1\nCOMMITTED\nline3\n", encoding="utf-8")

    out = await worktree_service.commit_worktree(db, res["project_id"], side="worktree")
    assert out["ok"] is True and out["committed"] is True
    assert _git(Path(res["path"]), "status", "--porcelain").strip() == ""


@pytest.mark.asyncio
async def test_merge_preserves_crlf_line_endings(db, repo):
    """合并写回时应保留目标文件的 CRLF 行尾，避免"整文件重写"的脏 diff。"""
    pid = await _seed_project(db, repo)
    (repo / "win.txt").write_bytes(b"a\r\nb\r\nc\r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add win file")
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-crlf")
    (Path(res["path"]) / "win.txt").write_bytes(b"a\r\nB-WT\r\nc\r\n")

    preview = await worktree_service.merge_preview(db, res["project_id"])
    files = [{"path": f["path"], "content": f["merged"]} for f in preview["files"]]
    await worktree_service.merge_apply(db, res["project_id"], files)
    # 内容已更新，且行尾仍是 CRLF（不是 LF）
    assert (repo / "win.txt").read_bytes() == b"a\r\nB-WT\r\nc\r\n"


@pytest.mark.asyncio
async def test_merge_file_detects_conflict(db, repo):
    """两侧改同一行 → 自动合并失败并标记冲突（交给用户/AI 处理）。"""
    pid = await _seed_project(db, repo)
    res = await worktree_service.create_worktree_for_project(db, pid, name="wt-conflict")
    # 主工作区与工作树各自改 app.txt 的同一行
    (repo / "app.txt").write_text("line1\nMAIN-SIDE\nline3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main side")
    (Path(res["path"]) / "app.txt").write_text("line1\nWT-SIDE\nline3\n", encoding="utf-8")

    preview = await worktree_service.merge_preview(db, res["project_id"])
    row = next(f for f in preview["files"] if f["path"] == "app.txt")
    assert row["conflict"] is True, "两侧改同一行应被判为冲突"
    assert preview["has_conflict"] is True
    assert "<<<<<<<" in (row["merged"] or ""), "冲突内容应含冲突标记"

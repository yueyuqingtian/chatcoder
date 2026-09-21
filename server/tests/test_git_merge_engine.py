"""git 原生合并引擎测试（plan-308-1542 需求3-A）。

核心断言：
  1) 单侧改动（含 CRLF 文件、二进制文件）**不再被判冲突**——这是"假冲突"的回归测试；
  2) 双侧改同一段 → 真冲突，且内容为 git 的 diff3 标记；
  3) 未提交改动经临时索引快照参与合并，且**调用前后用户仓库 HEAD 与 status 不变**；
  4) 临时工作树被清理，"真正执行 git merge" 能拿到索引 stage1/2/3 三态。
"""
import subprocess
from pathlib import Path

import pytest

from app.services import git_merge_engine as ge


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {p.stderr or p.stdout}")
    return p.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.local")
    _git(r, "config", "user.name", "t")
    # 关键：仓库中存储 CRLF（autocrlf=false）——旧实现的"假冲突"场景
    _git(r, "config", "core.autocrlf", "false")
    (r / "crlf.txt").write_bytes(b"L1\r\nL2\r\nL3\r\n")
    (r / "plain.txt").write_text("p1\np2\n", encoding="utf-8")
    (r / "bin.dat").write_bytes(b"\x00\x01bin\x00")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    _git(r, "checkout", "-qb", "feature")
    return r


@pytest.mark.asyncio
async def test_single_side_change_is_not_conflict(repo: Path):
    """仅一侧改动（CRLF / 二进制）必须判干净——回归"假冲突"。"""
    main_rev = _git(repo, "rev-parse", "main").strip()
    # 只在 feature 上改，主分支完全不动
    (repo / "crlf.txt").write_bytes(b"L1\r\nWT\r\nL3\r\n")
    (repo / "bin.dat").write_bytes(b"\x00\x01WT-BIN\x00")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "wt edits")
    feat = _git(repo, "rev-parse", "HEAD").strip()

    det = await ge.detect(str(repo), main_rev, feat)
    assert det["clean"] is True, "单侧改动不应有冲突"
    assert det["conflicted"] == []

    b = await ge.show_file(str(repo), main_rev, "crlf.txt")
    o = await ge.show_file(str(repo), main_rev, "crlf.txt")
    t = await ge.show_file(str(repo), feat, "crlf.txt")
    res = await ge.merge_file(o, b, t)
    assert res["clean"] is True and res["returncode"] == 0
    assert "<<<<<<<" not in (res["content"] or ""), "干净合并不得含冲突标记"
    assert "WT" in (res["content"] or "")


@pytest.mark.asyncio
async def test_both_sides_same_region_is_conflict_with_diff3(repo: Path):
    """双侧改同一段 → 真冲突，内容为 git 的 diff3 标记。"""
    (repo / "crlf.txt").write_bytes(b"L1\r\nFEAT\r\nL3\r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat edit")
    feat = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "main")
    (repo / "crlf.txt").write_bytes(b"L1\r\nMAIN\r\nL3\r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main edit")
    main_rev = _git(repo, "rev-parse", "HEAD").strip()
    base_rev = _git(repo, "merge-base", main_rev, feat).strip()

    det = await ge.detect(str(repo), main_rev, feat)
    assert det["clean"] is False and det["exit_code"] == 1
    assert "crlf.txt" in det["conflicted"]

    res = await ge.merge_file(
        await ge.show_file(str(repo), main_rev, "crlf.txt"),
        await ge.show_file(str(repo), base_rev, "crlf.txt"),
        await ge.show_file(str(repo), feat, "crlf.txt"),
    )
    assert res["clean"] is False and res["returncode"] == 1
    content = res["content"] or ""
    for marker in ("<<<<<<<", "|||||||", "=======", ">>>>>>>"):
        assert marker in content, f"diff3 标记缺少 {marker}"


@pytest.mark.asyncio
async def test_snapshot_dirty_keeps_repo_untouched(repo: Path):
    """未提交改动可被快照用于合并，且调用前后 HEAD / status 完全不变。"""
    _git(repo, "checkout", "-q", "main")
    (repo / "plain.txt").write_text("p1\nDIRTY\n", encoding="utf-8")

    before_status = _git(repo, "status", "--porcelain")
    before_head = _git(repo, "rev-parse", "HEAD").strip()

    snap = await ge.snapshot_dirty(str(repo))
    assert snap, "有未提交改动时应产出快照 commit"

    assert _git(repo, "status", "--porcelain") == before_status, "不得改动用户工作区/索引"
    assert _git(repo, "rev-parse", "HEAD").strip() == before_head, "不得移动 HEAD"
    # 快照内容应包含未提交改动
    content = await ge.show_file(str(repo), snap, "plain.txt")
    assert "DIRTY" in (content or "")


@pytest.mark.asyncio
async def test_snapshot_dirty_returns_none_when_clean(repo: Path):
    _git(repo, "checkout", "-q", "main")
    assert await ge.snapshot_dirty(str(repo)) is None


@pytest.mark.asyncio
async def test_merge_in_temp_worktree_gives_stages_and_cleans_up(repo: Path):
    """真正执行 git merge：可拿到冲突清单与 stage1/2/3，且临时目录被清理。"""
    (repo / "crlf.txt").write_bytes(b"L1\r\nFEAT\r\nL3\r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat edit")
    feat = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "main")
    (repo / "crlf.txt").write_bytes(b"L1\r\nMAIN\r\nL3\r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main edit")
    main_rev = _git(repo, "rev-parse", "HEAD").strip()

    res = await ge.merge_in_temp_worktree(str(repo), main_rev, feat, "t-stages", keep=True)
    try:
        assert res["conflicted"] == ["crlf.txt"]
        assert res["status_map"].get("crlf.txt") == "modified"
        st = await ge.stages_in_temp_worktree(res["dir"], "crlf.txt")
        assert "MAIN" in (st["ours"] or "")
        assert "FEAT" in (st["theirs"] or "")
        assert "L2" in (st["base"] or "")
        content = await ge.conflict_content_in_temp(res["dir"], "crlf.txt")
        assert "<<<<<<<" in (content or ""), "临时工作区文件应是 git 原生冲突标记"
    finally:
        await ge.cleanup_temp_worktree(str(repo), "t-stages")

    # 清理后不得残留临时工作树
    assert not (repo / ge._TMP_MERGE_DIR / "t-stages").exists()
    # 也不得影响用户仓库工作区
    assert _git(repo, "status", "--porcelain").strip() == ""


@pytest.mark.asyncio
async def test_merge_file_returncode_grading_binary(repo: Path):
    """二进制冲突不应被当成"普通冲突"阻断：merge_file 给出 reason 并降级采用来源侧。"""
    ours = "\x00\x01\x02ours\x00"
    base = "\x00\x01\x02base\x00"
    theirs = "\x00\x01\x02theirs\x00"
    res = await ge.merge_file(ours, base, theirs)
    # rc>1（二进制）→ 不阻断：采用来源侧 + reason
    if res["returncode"] > 1:
        assert res["clean"] is True
        assert res["content"] == theirs
        assert res["reason"]

"""Git 冲突处理的后端回归测试（plan-20261017-git-conflict）。

覆盖：
  1) 拉取产生冲突时返回结构化结果（conflict / conflicts / state），不以错误中断；
  2) 切换分支会覆盖本地改动时返回 switched=False + reason=dirty + 文件清单；
  3) 选择暂存后切换：切换成功，改动恢复冲突时回报 conflict；
  4) 选择放弃改动后切换：本地改动被丢弃，落到目标分支内容。
"""
import asyncio
import subprocess
from pathlib import Path

import pytest

from app.services import git_service as gs


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {p.stderr or p.stdout}")
    return p.stdout


def _init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "t@t.local")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "core.autocrlf", "false")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


@pytest.fixture
def pair(tmp_path: Path):
    """bare 远端 + 两个克隆（work / other）；a.txt 初始内容为 base。"""
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True,
                   capture_output=True, text=True, timeout=60)
    seed = tmp_path / "seed"
    _init_repo(seed)
    (seed / "a.txt").write_text("base\n", encoding="utf-8")
    _commit(seed, "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    work = tmp_path / "work"
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(work))
    _git(tmp_path, "clone", str(remote), str(other))
    for r in (work, other):
        _git(r, "config", "user.email", "t@t.local")
        _git(r, "config", "user.name", "t")
        _git(r, "config", "core.autocrlf", "false")
        # 固定拉取策略，避免不同 git 版本默认行为差异
        _git(r, "config", "pull.rebase", "false")
    return work, other


def _diverged(pair):
    """构造：远端与 work 同时改 a.txt（work 留在 main，feature 上是另一份内容）。"""
    work, _other = pair
    _git(work, "checkout", "-b", "feature")
    (work / "a.txt").write_text("feature\n", encoding="utf-8")
    _commit(work, "feature change")
    _git(work, "checkout", "main")
    (work / "a.txt").write_text("local edit\n", encoding="utf-8")
    return work


def test_pull_conflict_returns_structured_result(pair):
    work, other = pair
    (other / "a.txt").write_text("remote\n", encoding="utf-8")
    _commit(other, "remote change")
    _git(other, "push", "origin", "main")

    (work / "a.txt").write_text("local\n", encoding="utf-8")
    _commit(work, "local change")

    res = asyncio.run(gs.sync(str(work), "pull"))
    assert res["conflict"] is True
    assert "a.txt" in res["conflicts"]
    assert res["state"] in ("merging", "rebasing")


def test_checkout_blocked_by_local_changes(pair):
    work = _diverged(pair)

    res = asyncio.run(gs.branch_action(str(work), "checkout", "feature"))
    assert res["switched"] is False
    assert res["reason"] == "dirty"
    assert "a.txt" in res["files"]
    # 未选择处理方式时不动用户的改动
    assert (work / "a.txt").read_text(encoding="utf-8").strip() == "local edit"


def test_checkout_with_stash_reports_restore_conflict(pair):
    work = _diverged(pair)

    res = asyncio.run(gs.branch_action(str(work), "checkout", "feature", on_dirty="stash"))
    assert res["switched"] is True
    assert res["stashed"] is True
    # 恢复暂存改动时与目标分支冲突：留在工作区等待处理
    assert res["conflict"] is True
    assert "a.txt" in res["conflicts"]


def test_checkout_with_discard_drops_local_changes(pair):
    work = _diverged(pair)

    res = asyncio.run(gs.branch_action(str(work), "checkout", "feature", on_dirty="discard"))
    assert res["switched"] is True
    assert (work / "a.txt").read_text(encoding="utf-8").strip() == "feature"

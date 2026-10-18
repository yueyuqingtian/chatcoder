"""Git 面板的数据安全回归测试（丢弃 / 切换分支）。

覆盖：
  1) 丢弃「已暂存的新文件」（HEAD 中不存在）：不再因 checkout 失败，文件被取消暂存并删除；
  2) 一批里混有已跟踪改动与新增暂存文件：整批一次成功，不会因一个文件失败而全部失败；
  3) 切换分支时分支名恰好是一个文件名：不会把该文件恢复成索引版本（静默丢弃本地改动），而是明确报错；
  4) 本地改动会被覆盖时返回 reason=dirty（含已暂存的文件），由界面让用户选择；
  5) 「放弃改动再切换」同时清掉已暂存的改动，切换后工作区干净。
"""
import asyncio
import subprocess
from pathlib import Path

import pytest

from app.services import git_service as gs


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
    _git(r, "config", "core.autocrlf", "false")
    (r / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (r / "README.md").write_text("readme\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


def _run(coro):
    return asyncio.run(coro)


def test_discard_staged_new_file_removes_it(repo: Path):
    new = repo / "fresh.txt"
    new.write_text("brand new\n", encoding="utf-8")
    _git(repo, "add", "fresh.txt")  # 已暂存、HEAD 中不存在

    _run(gs.discard(str(repo), ["fresh.txt"], []))

    assert not new.exists()
    assert "fresh.txt" not in _git(repo, "status", "--porcelain")


def test_discard_batch_mixes_tracked_and_staged_new(repo: Path):
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    fresh = repo / "fresh.txt"
    fresh.write_text("x\n", encoding="utf-8")
    _git(repo, "add", "fresh.txt")

    # 一次批量丢弃：旧实现会因 fresh.txt 不在 HEAD 而整批 checkout 失败
    _run(gs.discard(str(repo), ["a.txt", "fresh.txt"], []))

    assert (repo / "a.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert not fresh.exists()
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_checkout_refuses_file_name_as_branch(repo: Path):
    # 本地改动：若把 README.md 当成路径检出，改动会被静默覆盖
    (repo / "README.md").write_text("my important edit\n", encoding="utf-8")

    with pytest.raises(gs.GitError):
        _run(gs.branch_action(str(repo), "checkout", "README.md"))

    assert (repo / "README.md").read_text(encoding="utf-8") == "my important edit\n"


def _diverge_feature(repo: Path) -> None:
    """feature 分支上 a.txt 的内容与 main 不同，使切换会覆盖本地对 a.txt 的改动。"""
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.txt").write_text("feature version\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feature edit")
    _git(repo, "checkout", "-q", "main")


def test_dirty_switch_reports_staged_blocker(repo: Path):
    _diverge_feature(repo)
    (repo / "a.txt").write_text("staged edit\n", encoding="utf-8")
    _git(repo, "add", "a.txt")

    res = _run(gs.branch_action(str(repo), "checkout", "feature"))

    assert res["switched"] is False
    assert res["reason"] == "dirty"
    assert "a.txt" in res["files"]
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_discard_and_switch_clears_staged_changes(repo: Path):
    _diverge_feature(repo)
    (repo / "a.txt").write_text("staged edit\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    (repo / "fresh.txt").write_text("untracked\n", encoding="utf-8")

    res = _run(gs.branch_action(str(repo), "checkout", "feature", on_dirty="discard"))

    assert res["switched"] is True
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feature"
    assert (repo / "a.txt").read_text(encoding="utf-8") == "feature version\n"
    assert not (repo / "fresh.txt").exists()
    assert _git(repo, "status", "--porcelain").strip() == ""

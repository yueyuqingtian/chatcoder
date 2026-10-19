"""Git 未提交改动增删行数（plan-20261019-toolcard-v2）：工具卡 Git 栏的「更改 +N −M」。

口径：已暂存 + 未暂存相对 HEAD 的行数合计，再加未跟踪文本文件的行数（整文件算新增）。
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


def _init(r: Path) -> None:
    r.mkdir(parents=True, exist_ok=True)
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    _init(r)
    (r / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (r / "b.txt").write_text("keep\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


def test_parse_numstat_counts_lines_and_ignores_binary():
    out = "3\t1\ta.txt\n-\t-\timg.png\n0\t5\tgone.txt\n"
    assert gs._parse_numstat(out) == (3, 6)
    assert gs._parse_numstat("") == (0, 0)


def test_clean_repo_is_zero_and_reports_branch(repo: Path):
    res = asyncio.run(gs.diff_stat(str(repo)))
    assert (res["additions"], res["deletions"], res["files"]) == (0, 0, 0)
    assert [r["rel"] for r in res["repos"]] == [""]
    only = res["repos"][0]
    # 卡片摘要里直接带着分支与领先落后，前端不用再发第二个请求
    assert (only["branch"], only["detached"], only["ahead"], only["behind"]) == ("main", False, 0, 0)
    assert only["name"] == "repo"


def test_counts_changed_files_across_kinds_without_double_counting(repo: Path):
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")  # 未暂存
    (repo / "b.txt").write_text("keep\nmore\n", encoding="utf-8")  # 已暂存 + 之后又改
    _git(repo, "add", "b.txt")
    (repo / "b.txt").write_text("keep\nmore\neven more\n", encoding="utf-8")
    (repo / "n1.txt").write_text("x\n", encoding="utf-8")  # 未跟踪
    (repo / "dir").mkdir()
    (repo / "dir" / "n2.txt").write_text("y\n", encoding="utf-8")  # 未跟踪目录里的文件逐个计
    res = asyncio.run(gs.diff_stat(str(repo)))
    assert res["files"] == 4  # a、b、n1、dir/n2：同一文件的暂存 + 未暂存只算一个


def test_counts_unstaged_staged_and_untracked(repo: Path):
    # 未暂存：a.txt 删一行（-1）、加两行（+2）
    (repo / "a.txt").write_text("one\nthree\nfour\nfive\n", encoding="utf-8")
    # 已暂存：b.txt 追加两行（+2）
    (repo / "b.txt").write_text("keep\nx\ny\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    # 未跟踪：新文本文件 3 行（最后一行没有换行也算一行）+ 一个二进制文件（不计）
    (repo / "new.txt").write_text("l1\nl2\nl3", encoding="utf-8")
    (repo / "pic.bin").write_bytes(b"\x00\x01\x02\x03")

    res = asyncio.run(gs.diff_stat(str(repo)))
    assert res["additions"] == 2 + 2 + 3
    assert res["deletions"] == 1


def test_staged_and_unstaged_same_file_counted_once_against_head(repo: Path):
    (repo / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    res = asyncio.run(gs.diff_stat(str(repo)))
    # 相对 HEAD 共多了 two 行（four、five），不会把暂存区与工作区各算一遍
    assert (res["additions"], res["deletions"]) == (2, 0)


def test_repo_without_commits(tmp_path: Path):
    r = tmp_path / "fresh"
    _init(r)
    (r / "staged.txt").write_text("a\nb\n", encoding="utf-8")
    _git(r, "add", "staged.txt")
    (r / "loose.txt").write_text("c\n", encoding="utf-8")
    res = asyncio.run(gs.diff_stat(str(r)))
    assert (res["additions"], res["deletions"]) == (3, 0)


def test_multi_repo_totals_and_listing(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    _init(root)
    (root / "r.txt").write_text("1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "root")
    sub = root / "web"
    _init(sub)
    (sub / "w.txt").write_text("1\n", encoding="utf-8")
    _git(sub, "add", "-A")
    _git(sub, "commit", "-m", "web")
    (root / "r.txt").write_text("1\n2\n", encoding="utf-8")
    (sub / "w.txt").write_text("changed\n", encoding="utf-8")
    res = asyncio.run(gs.diff_stat(str(root)))
    by_rel = {r["rel"]: (r["additions"], r["deletions"], r["files"]) for r in res["repos"]}
    assert by_rel == {"": (1, 0, 1), "web": (1, 1, 1)}
    assert (res["additions"], res["deletions"], res["files"]) == (2, 1, 2)


def test_not_a_repo_raises(tmp_path: Path):
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(gs.GitError):
        asyncio.run(gs.diff_stat(str(d)))

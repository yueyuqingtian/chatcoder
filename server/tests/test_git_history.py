"""Git 历史视图的后端回归测试（提交列表分页 / 装饰分类 / 提交详情 / 单文件差异）。

覆盖：
  1) 装饰（%D）按 HEAD / 本地分支 / 远端分支 / 标签分类；
  2) skip 翻页不重不漏；
  3) 提交详情的变更类型与增删行数（新增、修改、删除）；
  4) 提交号只接受十六进制串，`HEAD`、`--选项` 等会被拒绝；
  5) 单文件差异与详情使用同一提交。
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


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg, "--allow-empty")
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    (r / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (r / "gone.txt").write_text("bye\n", encoding="utf-8")
    _commit(r, "c1 init")
    return r


def test_refs_are_classified():
    refs = gs._parse_refs("HEAD -> main, origin/main, tag: v1.0, feature/x", {"origin"})
    kinds = {(r["name"], r["kind"]) for r in refs}
    assert ("HEAD", "head") in kinds
    assert ("main", "branch") in kinds
    assert ("origin/main", "remote") in kinds
    assert ("v1.0", "tag") in kinds
    assert ("feature/x", "branch") in kinds


def test_log_pages_without_overlap(repo: Path):
    for i in range(2, 6):
        (repo / "a.txt").write_text(f"v{i}\n", encoding="utf-8")
        _commit(repo, f"c{i}")
    first = asyncio.run(gs.log(str(repo), limit=2, skip=0))
    second = asyncio.run(gs.log(str(repo), limit=2, skip=2))
    third = asyncio.run(gs.log(str(repo), limit=2, skip=4))
    assert [r["subject"] for r in first] == ["c5", "c4"]
    assert [r["subject"] for r in second] == ["c3", "c2"]
    assert [r["subject"] for r in third] == ["c1 init"]
    assert first[0]["refs"][0] == {"name": "HEAD", "kind": "head"}


def test_commit_detail_reports_change_kinds_and_counts(repo: Path):
    (repo / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")  # 修改：+2 -1
    (repo / "new.txt").write_text("x\ny\n", encoding="utf-8")           # 新增：+2
    (repo / "gone.txt").unlink()                                          # 删除：-1
    sha = _commit(repo, "c2 mixed")

    detail = asyncio.run(gs.commit_detail(str(repo), sha))
    assert detail["sha"] == sha
    assert detail["subject"] == "c2 mixed"
    assert detail["merge"] is False
    by_path = {f["path"]: f for f in detail["files"]}
    assert by_path["a.txt"]["code"] == "M"
    assert (by_path["a.txt"]["additions"], by_path["a.txt"]["deletions"]) == (2, 1)
    assert by_path["new.txt"]["code"] == "A"
    assert by_path["gone.txt"]["code"] == "D"
    assert detail["additions"] == 4
    assert detail["deletions"] == 2


def test_commit_sha_rejects_non_hex_input(repo: Path):
    for bad in ["HEAD", "--output=x", "abc def", "zz", ""]:
        with pytest.raises(gs.GitError):
            asyncio.run(gs.commit_detail(str(repo), bad))


def test_commit_diff_for_one_file(repo: Path):
    (repo / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    sha = _commit(repo, "c2 add line")
    res = asyncio.run(gs.commit_diff(str(repo), sha, "a.txt"))
    assert res["binary"] is False
    assert "+three" in res["text"]
    # 路径越界必须被拒绝（与工作区差异共用的相对路径校验）
    with pytest.raises(gs.GitError):
        asyncio.run(gs.commit_diff(str(repo), sha, "../outside.txt"))

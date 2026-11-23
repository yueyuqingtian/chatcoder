"""Git 增强接口与 Stash / Soft Reset / Hunk / Sync 取消回归测试（Group D）。"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import git as git_router
from app.persistence.database import async_session_factory
from app.persistence.models import Project
from app.services import git_service as gs


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {p.stderr or p.stdout}")
    return p.stdout


def _init_empty_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "t@t.local")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "core.autocrlf", "false")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = _init_empty_repo(tmp_path / "repo")
    (r / "a.txt").write_bytes(
        b"line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\nline11\nline12\n"
    )
    (r / "b.txt").write_bytes(b"b1\nb2\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "initial commit")
    return r


@pytest.fixture
async def project_and_client(repo: Path):
    async with async_session_factory() as db:
        proj = Project(name="git-ext-proj", path=str(repo))
        db.add(proj)
        await db.commit()
        await db.refresh(proj)
        pid = proj.id

    app = FastAPI()
    app.include_router(git_router.router, prefix="/api")
    with TestClient(app) as client:
        yield pid, repo, client


# ── 1. status / diff / stage / unstage 边界 ──────────────────────────────────


@pytest.mark.asyncio
async def test_status_splits_mm_and_rename_modified(repo: Path):
    # MM 部分暂存
    (repo / "a.txt").write_bytes(b"line1\nSTAGED\nline3\nline4\nline5\nline6\nline7\nline8\n")
    await gs.stage(str(repo), ["a.txt"])
    (repo / "a.txt").write_bytes(b"line1\nSTAGED\nline3\nline4\nline5\nline6\nline7\nUNSTAGED\n")

    # 重命名 + 工作区再修改 (RM)
    _git(repo, "mv", "b.txt", "b_renamed.txt")
    (repo / "b_renamed.txt").write_bytes(b"b1\nb2\nb3\n")

    st = await gs.status(str(repo))
    staged_map = {e["path"]: e for e in st["staged"]}
    unstaged_map = {e["path"]: e for e in st["unstaged"]}

    assert staged_map["a.txt"]["code"] == "M"
    assert unstaged_map["a.txt"]["code"] == "M"
    assert staged_map["b_renamed.txt"]["code"] == "R"
    assert staged_map["b_renamed.txt"]["old_path"] == "b.txt"
    assert unstaged_map["b_renamed.txt"]["code"] == "M"


@pytest.mark.asyncio
async def test_discard_worktree_preserves_partial_index(repo: Path):
    file = repo / "a.txt"
    file.write_bytes(b"staged\n")
    await gs.stage(str(repo), ["a.txt"])
    file.write_bytes(b"unstaged\n")

    await gs.discard(str(repo), ["a.txt"], [], preserve_index=True)

    assert file.read_bytes() == b"staged\n"
    assert _git(repo, "show", ":a.txt") == "staged\n"
    state = await gs.status(str(repo))
    assert [entry["path"] for entry in state["staged"]] == ["a.txt"]
    assert state["unstaged"] == []


@pytest.mark.asyncio
async def test_sync_progress_reports_real_stages_and_cleans_up(monkeypatch):
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def resolve_path(*_args):
        return "unused"

    async def simulate_sync(_path, _action, *, op_id, on_progress):
        assert op_id == "progress-771"
        await on_progress("remote: Receiving objects: 45% (9/20)")
        entered.set()
        await finish.wait()
        return {"output": "完成"}

    monkeypatch.setattr(git_router, "_body_path", resolve_path)
    monkeypatch.setattr(gs, "sync", simulate_sync)
    app = FastAPI()
    app.include_router(git_router.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        sync = asyncio.create_task(client.post("/api/git/sync", json={"project_id": 1, "action": "fetch", "op_id": "progress-771"}))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            progress = await client.get("/api/git/sync/progress", params={"op_id": "progress-771"})
            assert progress.json() == {"phase": "receiving", "percent": 45}
        finally:
            finish.set()
            await sync
        assert sync.result().json()["ok"]
        cleared = await client.get("/api/git/sync/progress", params={"op_id": "progress-771"})
        assert cleared.json() == {"phase": None, "percent": None}


@pytest.mark.asyncio
async def test_no_head_repo_status_stage_unstage_and_diff(tmp_path: Path):
    empty = _init_empty_repo(tmp_path / "fresh")
    (empty / "first.txt").write_bytes(b"hello\n")
    await gs.stage(str(empty), ["first.txt"])
    (empty / "first.txt").write_bytes(b"hello\nworld\n")

    st = await gs.status(str(empty))
    assert st["staged"] == [{"path": "first.txt", "code": "A", "old_path": None}]
    assert st["unstaged"] == [{"path": "first.txt", "code": "M", "old_path": None}]

    d_staged = await gs.diff(str(empty), "first.txt", staged=True)
    assert "+hello" in d_staged["text"]
    d_unstaged = await gs.diff(str(empty), "first.txt", staged=False)
    assert "+world" in d_unstaged["text"]

    await gs.unstage(str(empty), ["first.txt"])
    st2 = await gs.status(str(empty))
    assert st2["staged"] == []
    assert [e["path"] for e in st2["untracked"]] == ["first.txt"]


@pytest.mark.asyncio
async def test_diff_deleted_in_index_and_untracked_binary(repo: Path):
    # 在 index 中删除 b.txt，工作区无该文件
    _git(repo, "rm", "b.txt")
    d_staged = await gs.diff(str(repo), "b.txt", staged=True)
    assert "-b1" in d_staged["text"]
    d_missing = await gs.diff(str(repo), "b.txt", staged=False)
    assert d_missing == {"path": "b.txt", "text": "", "truncated": False, "binary": False, "new": False}

    # 工作区新建同名文件（仍不在 index 中）
    (repo / "b.txt").write_bytes(b"reborn\n")
    d_reborn = await gs.diff(str(repo), "b.txt", staged=False)
    assert d_reborn["new"] is True
    assert "reborn" in d_reborn["text"]

    # 未跟踪二进制文件
    (repo / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    d_bin = await gs.diff(str(repo), "pic.png", staged=False)
    assert d_bin["binary"] is True and d_bin["new"] is True


# ── 2. Stash 全套操作 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stash_lifecycle_and_conflict(repo: Path, tmp_path: Path):
    # 无提交仓库返回空列表，push 报错
    empty = _init_empty_repo(tmp_path / "no_head")
    assert await gs.stash_list(str(empty)) == []

    # 工作区干净时拒绝 push
    with pytest.raises(gs.GitError, match="没有可贮藏的改动"):
        await gs.stash_push(str(repo))

    # 修改 a.txt、b.txt 与未跟踪文件 u.txt，仅贮藏 a.txt 和 u.txt
    (repo / "a.txt").write_bytes(b"stashed a\n")
    (repo / "b.txt").write_bytes(b"keep b\n")
    (repo / "u.txt").write_bytes(b"untracked u\n")
    res_push = await gs.stash_push(
        str(repo), message="save partial", include_untracked=True, paths=["a.txt", "u.txt"]
    )
    assert res_push["ok"] is True
    assert not (repo / "u.txt").exists()
    assert (repo / "b.txt").read_bytes() == b"keep b\n"

    items = await gs.stash_list(str(repo))
    assert len(items) == 1
    assert items[0]["ref"] == "stash@{0}"
    assert items[0]["index"] == 0
    assert "save partial" in items[0]["message"]
    assert items[0]["branch"] == "main"
    assert len(items[0]["sha"]) >= 7

    shown = await gs.stash_show(str(repo), index=0)
    shown_paths = {f["path"]: f["code"] for f in shown["files"]}
    assert shown_paths == {"a.txt": "M", "u.txt": "A"}
    assert "a.txt" in shown["stat"] and "u.txt" in shown["stat"]

    # 恢复 b.txt 为干净状态再测试 apply + 冲突处理
    _git(repo, "checkout", "--", "b.txt")
    # 故意在工作区制造一个会被 stash apply 冲突的提交
    (repo / "a.txt").write_bytes(b"conflicting commit\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "conflict base")

    res_apply = await gs.stash_apply(str(repo), index=0)
    assert res_apply["ok"] is True
    assert res_apply["conflict"] is True
    assert "a.txt" in res_apply["conflicts"]

    # 清理冲突后测试 drop 与 pop
    _git(repo, "reset", "--hard", "HEAD")
    _git(repo, "clean", "-fd")
    res_drop = await gs.stash_drop(str(repo), index=0)
    assert res_drop["ok"] is True
    assert await gs.stash_list(str(repo)) == []

    # 测试 pop 正常弹出
    (repo / "b.txt").write_bytes(b"for pop\n")
    await gs.stash_push(str(repo), message="pop me")
    res_pop = await gs.stash_pop(str(repo), index=0)
    assert res_pop["ok"] is True and res_pop["conflict"] is False
    assert (repo / "b.txt").read_bytes() == b"for pop\n"
    assert await gs.stash_list(str(repo)) == []

    with pytest.raises(gs.GitError):
        await gs.stash_drop(str(repo), index=-1)


# ── 3. reset-soft 撤销上次提交 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_soft_last(repo: Path):
    # 首次提交不允许撤销
    with pytest.raises(gs.GitError, match="首次提交"):
        await gs.reset_soft_last(str(repo))

    (repo / "a.txt").write_bytes(b"v2\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "feat: second commit\n\ndetail body")
    second_sha = _git(repo, "rev-parse", "--short", "HEAD").strip()

    res = await gs.reset_soft_last(str(repo))
    assert res["ok"] is True
    assert res["previous_sha"] == second_sha
    assert res["message"] == "feat: second commit\n\ndetail body"

    st = await gs.status(str(repo))
    assert [e["path"] for e in st["staged"]] == ["a.txt"]
    assert st["unstaged"] == []


# ── 4. Hunk 块级暂存与取消暂存 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_patch_to_index_stage_and_unstage_hunk(repo: Path):
    # 修改文件首尾两处，生成两个独立 hunk
    (repo / "a.txt").write_bytes(
        b"LINE1_MOD\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\nline11\nLINE12_MOD\n"
    )
    full_diff = (await gs.diff(str(repo), "a.txt", staged=False))["text"]
    assert full_diff.count("\n@@ ") == 2

    # 仅保留头部 header + 第一个 hunk
    first_at = full_diff.index("\n@@ ")
    second_at = full_diff.index("\n@@ ", first_at + 4)
    hunk1_patch = full_diff[: second_at + 1]

    await gs.apply_patch_to_index(str(repo), hunk1_patch, reverse=False)
    st = await gs.status(str(repo))
    assert [e["path"] for e in st["staged"]] == ["a.txt"]
    assert [e["path"] for e in st["unstaged"]] == ["a.txt"]

    staged_diff = (await gs.diff(str(repo), "a.txt", staged=True))["text"]
    assert "+LINE1_MOD" in staged_diff
    assert "+LINE12_MOD" not in staged_diff

    # 取消暂存该 hunk
    await gs.apply_patch_to_index(str(repo), hunk1_patch, reverse=True)
    st_after = await gs.status(str(repo))
    assert st_after["staged"] == []

    with pytest.raises(gs.GitError, match="不能为空"):
        await gs.apply_patch_to_index(str(repo), "   ")
    with pytest.raises(gs.GitError, match="过大"):
        await gs.apply_patch_to_index(str(repo), "x" * (gs.MAX_PATCH_BYTES + 10))


# ── 5. 可取消同步子进程追踪 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_sync_kills_active_process_and_cleans_registry(repo: Path, monkeypatch):
    # 未知 op_id 返回 False
    assert await gs.cancel_sync("not-exists") is False

    # 用真实的长耗时子进程模拟卡住的网络命令
    started = asyncio.Event()
    orig_create = asyncio.create_subprocess_exec

    async def fake_exec(*cmd, **kwargs):
        if "ls-remote" in cmd or "fetch" in cmd:
            started.set()
            return await orig_create(
                sys.executable, "-c", "import time; time.sleep(30)",
                cwd=kwargs.get("cwd"),
                stdin=kwargs.get("stdin"),
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
            )
        return await orig_create(*cmd, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    # 给仓库配置一个假上游，使 sync("fetch") 会触发 _ensure_reachable -> ls-remote
    _git(repo, "remote", "add", "origin", "https://example.invalid/repo.git")

    async def run_fetch():
        return await gs.sync(str(repo), "fetch", op_id="op-hang-1")

    task = asyncio.create_task(run_fetch())
    await asyncio.wait_for(started.wait(), timeout=5)
    # 等待登记进 _ACTIVE_SYNC_PROCS
    for _ in range(50):
        if "op-hang-1" in gs._ACTIVE_SYNC_PROCS:
            break
        await asyncio.sleep(0.02)
    assert "op-hang-1" in gs._ACTIVE_SYNC_PROCS

    cancelled = await gs.cancel_sync("op-hang-1")
    assert cancelled is True

    with pytest.raises(gs.GitError, match="已取消同步操作"):
        await task

    assert "op-hang-1" not in gs._ACTIVE_SYNC_PROCS
    assert "op-hang-1" not in gs._ACTIVE_SYNC_OPS
    assert "op-hang-1" not in gs._CANCELLED_SYNC_OPS


# ── 6. FastAPI 路由端到端测试 ─────────────────────────────────────────────────


def test_git_router_ext_endpoints(project_and_client):
    pid, repo, client = project_and_client

    # stashes 空列表
    r = client.get(f"/api/git/stashes?project_id={pid}")
    assert r.status_code == 200 and r.json() == []

    # reset-soft 在首次提交返回 400
    r = client.post("/api/git/reset-soft", json={"project_id": pid})
    assert r.status_code == 400
    assert "首次提交" in r.json()["detail"]

    # 修改文件并 stash push / list / show / apply / drop
    (repo / "a.txt").write_bytes(b"router stash\n")
    r = client.post("/api/git/stash/push", json={"project_id": pid, "message": "via api"})
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.get(f"/api/git/stashes?project_id={pid}")
    assert r.status_code == 200 and len(r.json()) == 1

    r = client.get(f"/api/git/stash/show?project_id={pid}&index=0")
    assert r.status_code == 200 and r.json()["files"][0]["path"] == "a.txt"

    r = client.post("/api/git/stash/apply", json={"project_id": pid, "index": 0})
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.post("/api/git/stash/drop", json={"project_id": pid, "index": 0})
    assert r.status_code == 200 and r.json()["ok"] is True

    # stage-hunk / unstage-hunk / reset-soft 成功路径
    (repo / "a.txt").write_bytes(
        b"LINE1_MOD\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\nline11\nLINE12_MOD\n"
    )
    diff_text = client.get(f"/api/git/diff?project_id={pid}&path=a.txt").json()["text"]
    second_at = diff_text.index("\n@@ ", diff_text.index("\n@@ ") + 4)
    hunk1 = diff_text[: second_at + 1]

    assert client.post("/api/git/stage-hunk", json={"project_id": pid, "patch": hunk1}).status_code == 200
    assert client.post("/api/git/unstage-hunk", json={"project_id": pid, "patch": hunk1}).status_code == 200

    # 新提交再 reset-soft
    client.post("/api/git/stage", json={"project_id": pid, "paths": ["a.txt"]})
    client.post("/api/git/commit", json={"project_id": pid, "message": "commit for soft reset"})
    r_reset = client.post("/api/git/reset-soft", json={"project_id": pid})
    assert r_reset.status_code == 200
    assert r_reset.json()["ok"] is True
    assert r_reset.json()["message"] == "commit for soft reset"

    # sync/cancel 校验与返回值
    assert client.post("/api/git/sync/cancel", json={"op_id": ""}).status_code == 422
    r_cancel = client.post("/api/git/sync/cancel", json={"op_id": "unknown-op"})
    assert r_cancel.status_code == 200 and r_cancel.json() == {"ok": True, "cancelled": False}

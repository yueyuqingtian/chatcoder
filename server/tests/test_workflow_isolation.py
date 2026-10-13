"""独立 git 工作树（设计 §5A.2）：子工作流在自己的工作树与分支中执行，结束时并入主工作区。

这里使用真实的 git 仓库（临时目录），验证的是 git 层面的行为：准备、并入、冲突留在工作树、
主工作区脏时拒绝快进、放弃时保留分支。
"""

import subprocess
from pathlib import Path

import pytest

from app.orchestration.workflow import isolation, store
from app.orchestration.workflow.executor import NodeResult
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _program_status,
    _runtime,
    _session,
)

POINTS = "server/app/points.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # 字节级写入，不受平台换行转换影响


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "core.autocrlf", "false")
    _write(root / POINTS, "a\nb\nc\n")
    _write(root / "README.md", "readme\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


async def test_prepare_creates_a_worktree_on_a_new_branch_from_head(tmp_path):
    _init_repo(tmp_path)

    wt = await isolation.prepare(str(tmp_path), run_id=1, flow_key="backend")

    assert wt is not None
    assert wt.branch == "chatcoder/wf-1-backend"
    assert (Path(wt.path) / POINTS).read_bytes() == (tmp_path / POINTS).read_bytes()
    assert wt.branch in _git(tmp_path, "branch", "--list", wt.branch)
    assert _git(tmp_path, "status", "--porcelain") == "", "工作树目录不应出现在主工作区的改动里"


async def test_prepare_reuses_the_worktree_after_a_restart(tmp_path):
    _init_repo(tmp_path)
    first = await isolation.prepare(str(tmp_path), run_id=2, flow_key="backend")
    _write(Path(first.path) / POINTS, "a\nb\nc\nkept\n")

    again = await isolation.prepare(str(tmp_path), run_id=2, flow_key="backend")

    assert again is not None and again.path == first.path
    assert _read(Path(again.path) / POINTS) == "a\nb\nc\nkept\n", "已有的改动不得丢失"


async def test_prepare_says_why_it_is_unavailable_outside_a_git_repository(tmp_path):
    with pytest.raises(isolation.WorktreeUnavailable) as caught:
        await isolation.prepare(str(tmp_path), run_id=3, flow_key="backend")
    assert caught.value.reason == "工作区不是 git 仓库"


async def test_prepare_says_why_it_is_unavailable_before_the_first_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    with pytest.raises(isolation.WorktreeUnavailable) as caught:
        await isolation.prepare(str(tmp_path), run_id=4, flow_key="backend")
    assert caught.value.reason == "仓库还没有任何提交"


async def test_integrate_merges_the_flow_into_the_main_workspace_and_cleans_up(tmp_path):
    _init_repo(tmp_path)
    wt = await isolation.prepare(str(tmp_path), run_id=5, flow_key="backend")
    _write(Path(wt.path) / POINTS, "a\nb\nc\nd\n")

    outcome = await isolation.integrate(
        str(tmp_path), wt.path, branch=wt.branch, message="子工作流改动"
    )
    assert outcome.status == "merged"
    assert _read(tmp_path / POINTS) == "a\nb\nc\nd\n"

    await isolation.remove(str(tmp_path), worktree=wt.path, branch=wt.branch, keep_branch=False)
    assert not Path(wt.root).exists()
    assert wt.branch not in _git(tmp_path, "branch", "--list", wt.branch)


async def test_main_branch_commits_are_brought_in_before_the_fast_forward(tmp_path):
    _init_repo(tmp_path)
    wt = await isolation.prepare(str(tmp_path), run_id=6, flow_key="backend")
    _write(Path(wt.path) / POINTS, "a\nb\nc\nd\n")
    _write(tmp_path / "README.md", "readme changed on main\n")
    _git(tmp_path, "commit", "-qam", "main moves on")

    outcome = await isolation.integrate(str(tmp_path), wt.path, branch=wt.branch, message="m")

    assert outcome.status == "merged"
    assert _read(tmp_path / "README.md") == "readme changed on main\n"
    assert _read(tmp_path / POINTS) == "a\nb\nc\nd\n"


async def test_conflicts_stay_in_the_worktree_until_the_merge_step_resolves_them(tmp_path):
    _init_repo(tmp_path)
    wt = await isolation.prepare(str(tmp_path), run_id=7, flow_key="backend")
    _write(Path(wt.path) / POINTS, "a\nFLOW\nc\n")
    _write(tmp_path / POINTS, "a\nMAIN\nc\n")
    _git(tmp_path, "commit", "-qam", "main changes the same line")

    first = await isolation.integrate(str(tmp_path), wt.path, branch=wt.branch, message="m")

    assert first.status == "conflict"
    assert first.files == (POINTS,)
    assert "<<<<<<<" in _read(Path(wt.path) / POINTS), "冲突标记留在工作树里"
    assert _read(tmp_path / POINTS) == "a\nMAIN\nc\n", "主工作区不得被半成品的合并污染"

    _write(Path(wt.path) / POINTS, "a\nFLOW+MAIN\nc\n")  # 合并步骤解决了冲突
    second = await isolation.integrate(str(tmp_path), wt.path, branch=wt.branch, message="m")

    assert second.status == "merged"
    assert _read(tmp_path / POINTS) == "a\nFLOW+MAIN\nc\n"


async def test_unresolved_conflict_markers_are_reported_again(tmp_path):
    _init_repo(tmp_path)
    wt = await isolation.prepare(str(tmp_path), run_id=8, flow_key="backend")
    _write(Path(wt.path) / POINTS, "a\nFLOW\nc\n")
    _write(tmp_path / POINTS, "a\nMAIN\nc\n")
    _git(tmp_path, "commit", "-qam", "main changes the same line")
    await isolation.integrate(str(tmp_path), wt.path, branch=wt.branch, message="m")

    again = await isolation.integrate(str(tmp_path), wt.path, branch=wt.branch, message="m")

    assert again.status == "conflict" and again.files == (POINTS,)


async def test_a_dirty_main_workspace_blocks_the_fast_forward_and_keeps_the_local_work(tmp_path):
    _init_repo(tmp_path)
    wt = await isolation.prepare(str(tmp_path), run_id=9, flow_key="backend")
    _write(Path(wt.path) / POINTS, "a\nb\nc\nd\n")
    _write(tmp_path / POINTS, "a\nb\nc\nlocal edit\n")  # 用户在主工作区的未提交改动

    outcome = await isolation.integrate(str(tmp_path), wt.path, branch=wt.branch, message="m")

    assert outcome.status == "blocked"
    assert "快进" in outcome.message
    assert _read(tmp_path / POINTS) == "a\nb\nc\nlocal edit\n", "本地改动不得被覆盖"


async def test_abandoning_a_flow_removes_the_worktree_but_keeps_the_branch(tmp_path):
    _init_repo(tmp_path)
    wt = await isolation.prepare(str(tmp_path), run_id=10, flow_key="backend")
    _write(Path(wt.path) / POINTS, "a\nb\nc\nunfinished\n")

    await isolation.remove(str(tmp_path), worktree=wt.path, branch=wt.branch, keep_branch=True)

    assert not Path(wt.root).exists()
    assert wt.branch in _git(tmp_path, "branch", "--list", wt.branch), "分支保留，便于查看"


# ── 运行时：独立工作树子工作流的完整流程 ──────────────────────


class WorktreeExecutor(ScriptedExecutor):
    """实现节点把改动写进它所在的工作区（工作树）。

    main_change 可选：实现完成后，主工作区同一处被提交改动（"commit"，制造冲突），
    或留下未提交的改动（"dirty"，使快进合并被拒绝）。
    """

    def __init__(self, *, main_root: Path | None = None, main_change: str | None = None):
        super().__init__()
        self.main_root = main_root
        self.main_change = main_change
        self.workspaces: list[str] = []

    async def run(self, req):
        if req.node_key == "impl":
            self.calls.append((req.node_key, req.mode, req.attempt_no, req.feedback))
            self.workspaces.append(req.workspace)
            _write(Path(req.workspace) / POINTS, "a\nFLOW\nc\nd\n")
            return NodeResult(status="done", output={"summary": "ok"})
        if req.node_key == "review" and self.main_root is not None:
            # 主工作区的改动发生在评估节点执行期间：节点开始前的同步已经完成，之后的并入才会遇到它
            if self.main_change == "commit":
                _write(self.main_root / POINTS, "a\nMAIN\nc\n")
                _git(self.main_root, "commit", "-qam", "main moved while the flow ran")
                self.main_root = None
            elif self.main_change == "dirty":
                _write(self.main_root / POINTS, "a\nb\nc\nlocal edit\n")
                self.main_root = None
        if req.node_key.startswith("merge-") and req.kind == "merge":
            self.calls.append((req.node_key, req.mode, req.attempt_no, req.feedback))
            self.workspaces.append(req.workspace)
            _write(Path(req.workspace) / POINTS, "a\nFLOW+MAIN\nc\nd\n")  # 合并步骤解决冲突
            return NodeResult(status="done", output={"summary": "合并完成"})
        return await super().run(req)


def _worktree_plan() -> dict:
    plan = _backend_plan(scope=POINTS)
    plan["flows"][0]["isolation"] = "worktree"
    return plan


async def test_a_worktree_flow_runs_in_its_own_tree_and_lands_in_the_main_workspace(tmp_path):
    _init_repo(tmp_path)
    executor = WorktreeExecutor()
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=30)

    assert await _program_status(pid) == "succeeded"
    assert executor.workspaces and all(ws != str(tmp_path) for ws in executor.workspaces)
    assert _read(tmp_path / POINTS) == "a\nFLOW\nc\nd\n"
    assert "chatcoder/wf-" not in _git(tmp_path, "branch", "--list"), "并入成功后分支已清理"


async def test_a_worktree_flow_falls_back_to_shared_outside_a_git_repository(tmp_path):
    executor = ScriptedExecutor()
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=30)

    snap = await store.load_program(pid)
    assert snap["flows"][0]["run"]["isolation"] == "shared"
    assert await _program_status(pid) == "succeeded"


async def test_a_blocked_integration_waits_for_the_user_and_retries_after_the_fix(tmp_path):
    _init_repo(tmp_path)
    executor = WorktreeExecutor(main_root=tmp_path, main_change="dirty")
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=30)

    snap = await store.load_program(pid)
    integrate = next(n for n in snap["flows"][0]["nodes"] if n["key"] == "integrate")
    assert integrate["status"] == "waiting_human"
    assert integrate["conflict"]["kind"] == "integration"
    assert "快进" in integrate["conflict"]["message"]
    assert _read(tmp_path / POINTS) == "a\nb\nc\nlocal edit\n", "用户的本地改动不得被覆盖"

    _git(tmp_path, "checkout", "--", POINTS)  # 用户处理了主工作区的本地改动
    await rt.decide_gate(pid, "backend", "integrate", "rework")
    await rt.run_until_idle(timeout=30)

    assert await _program_status(pid) == "succeeded"
    assert _read(tmp_path / POINTS) == "a\nFLOW\nc\nd\n"


async def test_a_conflict_on_integration_is_resolved_by_a_merge_step_in_the_worktree(tmp_path):
    _init_repo(tmp_path)
    # 实现完成后，主分支同一处被提交了改动
    executor = WorktreeExecutor(main_root=tmp_path, main_change="commit")
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=30)

    assert await _program_status(pid) == "succeeded"
    merges = [c for c in executor.calls if c[0].startswith("merge-")]
    assert len(merges) == 1
    assert _read(tmp_path / POINTS) == "a\nFLOW+MAIN\nc\nd\n"
    assert "chatcoder/wf-" not in _git(tmp_path, "branch", "--list")


# ── 开始前的确认：主工作区有未提交的改动（独立工作树看不到这些改动）──


async def test_starting_with_local_changes_asks_the_user_first(tmp_path):
    from app.orchestration.workflow.runtime import WorkflowLocalChangesError

    _init_repo(tmp_path)
    _write(tmp_path / POINTS, "a\nb\nc\nuser edit\n")  # 用户还没提交的改动
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )

    with pytest.raises(WorkflowLocalChangesError) as caught:
        await rt.start(pid)

    assert POINTS in caught.value.files
    assert (await store.load_program(pid))["run"]["status"] == "draft", "未确认前不得开始"


async def test_starting_builds_the_worktree_from_the_current_changes(tmp_path):
    _init_repo(tmp_path)
    _write(tmp_path / POINTS, "a\nb\nc\nuser edit\n")  # 用户还没提交的改动
    _write(tmp_path / "notes.md", "draft\n")  # 未跟踪的新文件也属于当前改动
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )

    await rt.start(pid, accept_local_changes=True)
    snap = await store.load_program(pid)
    worktree = Path(snap["flows"][0]["run"]["workspace"])
    assert worktree.is_dir(), "独立工作树已创建"
    assert _read(worktree / POINTS) == "a\nb\nc\nuser edit\n", "工作树以用户当前的改动为起点"
    assert _read(worktree / "notes.md") == "draft\n", "未跟踪的新文件也带入工作树"
    assert _read(tmp_path / POINTS) == "a\nb\nc\nuser edit\n", "用户的改动保持不变"

    await rt.run_until_idle(timeout=30)
    assert await _program_status(pid) == "succeeded"
    assert _git(tmp_path, "rev-list", "--count", "HEAD").strip() == "1", "主分支不产生提交"


async def test_a_snapshot_worktree_records_its_baseline_on_the_branch(tmp_path):
    _init_repo(tmp_path)
    _write(tmp_path / POINTS, "a\nb\nc\nuser edit\n")

    wt = await isolation.prepare(str(tmp_path), run_id=7, flow_key="backend")

    assert wt is not None and wt.baseline, "以未提交改动为起点时记录快照基线"
    head = _git(tmp_path, "rev-parse", "HEAD").strip()
    assert _git(tmp_path, "rev-parse", f"{wt.baseline}^").strip() == head, "快照以 HEAD 为父提交"
    assert _git(tmp_path, "config", f"branch.{wt.branch}.chatcoderBaseline").strip() == wt.baseline
    assert _read(tmp_path / POINTS) == "a\nb\nc\nuser edit\n", "主工作区本身不被改动"
    await isolation.remove(str(tmp_path), worktree=wt.root, branch=wt.branch, keep_branch=False)
    assert "chatcoder/wf-7" not in _git(tmp_path, "branch", "--list")


async def test_the_flow_changes_land_in_the_main_workspace_uncommitted_and_keep_the_user_edits(
    tmp_path,
):
    _init_repo(tmp_path)
    _write(tmp_path / "README.md", "readme\nuser note\n")  # 与子工作流无关的未提交改动
    rt = _runtime(WorktreeExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )

    await rt.start(pid, accept_local_changes=True)
    await rt.run_until_idle(timeout=30)

    assert await _program_status(pid) == "succeeded"
    assert _read(tmp_path / POINTS) == "a\nFLOW\nc\nd\n", "子工作流的修改写入主工作区"
    assert _read(tmp_path / "README.md") == "readme\nuser note\n", "用户的改动保持不变"
    assert _git(tmp_path, "rev-list", "--count", "HEAD").strip() == "1", "主分支不产生提交"
    assert "chatcoder/wf-" not in _git(tmp_path, "branch", "--list"), "并入后分支与工作树已清理"


async def test_an_overlapping_local_edit_blocks_the_direct_write_until_the_user_restores_it(
    tmp_path,
):
    _init_repo(tmp_path)
    _write(tmp_path / POINTS, "a\nb\nc\nuser edit\n")  # 子工作流以这份改动为起点
    executor = WorktreeExecutor(main_root=tmp_path, main_change="dirty")
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )

    await rt.start(pid, accept_local_changes=True)
    await rt.run_until_idle(timeout=30)

    snap = await store.load_program(pid)
    integrate = next(n for n in snap["flows"][0]["nodes"] if n["key"] == "integrate")
    assert integrate["status"] == "waiting_human"
    assert integrate["conflict"]["kind"] == "integration"
    assert _read(tmp_path / POINTS) == "a\nb\nc\nlocal edit\n", "用户的改动不得被覆盖"

    _write(tmp_path / POINTS, "a\nb\nc\nuser edit\n")  # 用户把改动恢复
    await rt.decide_gate(pid, "backend", "integrate", "rework")
    await rt.run_until_idle(timeout=30)

    assert await _program_status(pid) == "succeeded"
    assert _read(tmp_path / POINTS) == "a\nFLOW\nc\nd\n"


async def test_choosing_the_shared_workspace_runs_in_the_main_tree_without_a_worktree(tmp_path):
    _init_repo(tmp_path)
    _write(tmp_path / POINTS, "a\nb\nc\nuser edit\n")
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )

    await rt.start(pid, shared_workspace=True)

    snap = await store.load_program(pid)
    flow = snap["flows"][0]
    assert flow["run"]["isolation"] == "shared" and flow["run"]["workspace"] == ""
    assert len(_git(tmp_path, "worktree", "list").splitlines()) == 1, "不创建独立工作树"
    integrate = next(n for n in flow["nodes"] if n["kind"] == "integrate")
    assert integrate["status"] == "skipped", "共享执行无需并入"
    assert _read(tmp_path / POINTS) == "a\nb\nc\nuser edit\n", "用户的改动保持不变"

    await rt.run_until_idle(timeout=30)
    assert await _program_status(pid) == "succeeded"


class UserEditsDuringRun(ScriptedExecutor):
    """impl 节点把改动写进工作树，并在它执行期间模拟用户在主工作区的编辑；
    review 节点记录它看到的内容。"""

    def __init__(self, main_root: Path, main_edit: dict[str, str]):
        super().__init__()
        self.main_root = main_root
        self.main_edit = main_edit
        self.seen: dict[str, str] = {}

    async def run(self, req):
        if req.node_key == "impl":
            self.calls.append((req.node_key, req.mode, req.attempt_no, req.feedback))
            _write(Path(req.workspace) / POINTS, "a\nFLOW\nc\n")
            for rel, text in self.main_edit.items():
                _write(self.main_root / rel, text)
            self.main_edit = {}
            return NodeResult(status="done", output={"summary": "ok"})
        if req.node_key == "review":
            self.seen["README.md"] = _read(Path(req.workspace) / "README.md")
        return await super().run(req)


async def test_edits_made_in_the_main_workspace_reach_the_running_worktree_at_the_next_node(
    tmp_path,
):
    _init_repo(tmp_path)
    executor = UserEditsDuringRun(tmp_path, {"README.md": "readme\nedited while the flow runs\n"})
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )

    await rt.start(pid)
    await rt.run_until_idle(timeout=30)

    assert await _program_status(pid) == "succeeded"
    assert executor.seen["README.md"] == "readme\nedited while the flow runs\n", (
        "下一个节点看到用户的新改动"
    )
    assert _read(tmp_path / POINTS) == "a\nFLOW\nc\n", "子工作流的修改并入主工作区"
    assert _read(tmp_path / "README.md") == "readme\nedited while the flow runs\n", (
        "用户的改动保持原样"
    )
    assert _git(tmp_path, "rev-list", "--count", "HEAD").strip() == "1", "主分支不产生提交"


async def test_an_overlapping_edit_fails_the_next_node_and_leaves_both_sides_intact(tmp_path):
    _init_repo(tmp_path)
    executor = UserEditsDuringRun(tmp_path, {POINTS: "a\nUSER\nc\n"})  # 与子工作流改的是同一行
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )

    await rt.start(pid)
    await rt.run_until_idle(timeout=30)

    snap = await store.load_program(pid)
    review = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "review")
    assert review["status"] == "failed"
    assert "重叠" in f"{review.get('reason')} {review.get('error')}", "失败原因说明重叠"
    assert executor.seen == {}, "同步失败的节点不会执行"
    assert _read(tmp_path / POINTS) == "a\nUSER\nc\n", "用户的改动不被覆盖"
    worktree = Path(snap["flows"][0]["run"]["workspace"])
    assert _read(worktree / POINTS) == "a\nFLOW\nc\n", "子工作流已有的修改保持不变"


async def test_the_apps_own_files_are_not_counted_as_local_changes(tmp_path):
    _init_repo(tmp_path)
    _write(tmp_path / ".chatcoder" / "index_state.db", "index\n")  # 代码索引库：应用自己的文件
    assert await isolation.local_changes(str(tmp_path)) == [], "应用自己的索引库不算用户的改动"

    _write(tmp_path / "notes.md", "mine\n")  # 用户自己的新文件
    assert await isolation.local_changes(str(tmp_path)) == ["notes.md"]


async def test_a_clean_workspace_starts_without_asking(tmp_path):
    _init_repo(tmp_path)
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_worktree_plan(), workspace=str(tmp_path)
    )

    await rt.start(pid)
    await rt.run_until_idle(timeout=30)

    assert await _program_status(pid) == "succeeded"


def test_the_start_endpoint_returns_the_changed_files_as_a_conflict(tmp_path, monkeypatch):
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.gateway.routers import workflows as workflows_router
    from app.orchestration.workflow import service

    _init_repo(tmp_path)
    _write(tmp_path / POINTS, "a\nb\nc\nuser edit\n")
    monkeypatch.setattr(service, "_runtime", None)
    rt = service.get_runtime()
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")
    client = TestClient(app)

    pid = asyncio.get_event_loop().run_until_complete(
        rt.create_program(
            session_id=asyncio.get_event_loop().run_until_complete(_session()),
            plan=_worktree_plan(),
            workspace=str(tmp_path),
        )
    )

    refused = client.post(f"/api/workflows/{pid}/start")
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "local_changes" and POINTS in detail["files"]

    accepted = client.post(f"/api/workflows/{pid}/start", json={"accept_local_changes": True})
    assert accepted.status_code == 200 and accepted.json()["run"]["status"] == "running"

    second = asyncio.get_event_loop().run_until_complete(
        rt.create_program(
            session_id=asyncio.get_event_loop().run_until_complete(_session()),
            plan=_worktree_plan(),
            workspace=str(tmp_path),
        )
    )
    shared = client.post(f"/api/workflows/{second}/start", json={"shared_workspace": True})
    assert shared.status_code == 200
    assert shared.json()["flows"][0]["run"]["isolation"] == "shared"

"""命令行写入的事后核对（设计 §5A.1 第 8 条）：范围之外的改动转入用户决定。"""

import subprocess
from pathlib import Path

import pytest

from app.orchestration.tools.base import ToolResult
from app.orchestration.workflow import conflicts, guard, store
from app.orchestration.workflow.conflicts import LeaseOwner, get_lease_table
from tests.test_workflow_guard import _agent, _ctx, _impl_node
from tests.test_workflow_runtime import ScriptedExecutor, _backend_plan, _runtime, _session


@pytest.fixture(autouse=True)
def _reset_guard_state(monkeypatch):
    guard._LEDGERS.clear()
    guard._CONFLICTS.clear()
    guard._GATES.clear()
    guard._SHELL_BEFORE.clear()
    monkeypatch.setattr(guard, "_READ_INDEX", conflicts.ReadIndex())
    yield
    guard._LEDGERS.clear()
    guard._CONFLICTS.clear()
    guard._GATES.clear()
    guard._SHELL_BEFORE.clear()


def _init_repo(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (root / "server" / "app").mkdir(parents=True)
    (root / "server" / "app" / "points.py").write_text("a\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")


async def _shell(tmp_path: Path, agent_id: int, command: str, change):
    """执行一次命令：before → 命令实际改动（change）→ after。返回 (节点编号, 代理, 上下文)。"""
    sid, _pid, node_id = await _impl_node(tmp_path)
    agent, ctx = _agent(agent_id, node_id), _ctx(tmp_path, sid, agent_id)
    args = {"command": command}
    assert await guard.before_tool("terminal_exec", args, agent=agent, ctx=ctx) is None
    change()
    await guard.after_tool(
        "terminal_exec", args, agent=agent, ctx=ctx, result=ToolResult(ok=True, output="")
    )
    return node_id, agent, ctx


async def test_shell_write_outside_the_scope_opens_a_scope_gate(tmp_path):
    _init_repo(tmp_path)
    node_id, _agent_, _ctx_ = await _shell(
        tmp_path,
        41,
        "make docs",
        lambda: (tmp_path / "README.md").write_text("changed\n", encoding="utf-8"),
    )

    gate = guard.pop_gate(node_id)

    assert gate is not None and gate["kind"] == "scope_violation"
    assert gate["paths"] == ["README.md"]
    assert "README.md" in gate["reason"]


async def test_shell_write_inside_the_scope_is_not_a_violation(tmp_path):
    _init_repo(tmp_path)
    node_id, _agent_, _ctx_ = await _shell(
        tmp_path,
        42,
        "make points",
        lambda: (tmp_path / "server" / "app" / "points.py").write_text("b\n", encoding="utf-8"),
    )

    assert guard.pop_gate(node_id) is None


async def test_shell_writes_are_not_checked_outside_a_git_workspace(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)  # 工作区不是 git 仓库
    agent, ctx = _agent(43, node_id), _ctx(tmp_path, sid, 43)
    args = {"command": "echo x > README.md"}
    await guard.before_tool("terminal_exec", args, agent=agent, ctx=ctx)
    (tmp_path / "README.md").write_text("changed\n", encoding="utf-8")
    await guard.after_tool(
        "terminal_exec", args, agent=agent, ctx=ctx, result=ToolResult(ok=True, output="")
    )

    assert guard.pop_gate(node_id) is None, "无法判断时不做核对，行为与文档一致"


async def test_failed_commands_are_checked_too(tmp_path):
    _init_repo(tmp_path)
    sid, _pid, node_id = await _impl_node(tmp_path)
    agent, ctx = _agent(44, node_id), _ctx(tmp_path, sid, 44)
    args = {"command": "make docs-broken"}
    assert await guard.before_tool("terminal_exec", args, agent=agent, ctx=ctx) is None
    (tmp_path / "README.md").write_text("half\n", encoding="utf-8")  # 命令失败，但已写入一半
    await guard.after_tool(
        "terminal_exec",
        args,
        agent=agent,
        ctx=ctx,
        result=ToolResult(ok=False, output="", error="exit 1"),
    )

    gate = guard.pop_gate(node_id)

    assert gate is not None and gate["paths"] == ["README.md"]


async def test_paths_held_by_another_node_are_not_violations(tmp_path):
    _init_repo(tmp_path)
    sid = await _session()
    other = LeaseOwner(run_id=987654, node_key="other-node", session_id=sid)
    table = get_lease_table()
    granted, _conflicts_ = table.acquire(str(tmp_path), other, ("README.md",))
    assert granted
    try:
        node_id, _agent_, _ctx_ = await _shell(
            tmp_path,
            45,
            "make docs",
            lambda: (tmp_path / "README.md").write_text("changed\n", encoding="utf-8"),
        )
        assert guard.pop_gate(node_id) is None, "其它节点正持有的文件不算越界"
    finally:
        table.release(str(tmp_path), other)


async def test_gate_blocks_further_writes_until_the_user_decides(tmp_path):
    _init_repo(tmp_path)
    node_id, agent, ctx = await _shell(
        tmp_path,
        46,
        "make docs",
        lambda: (tmp_path / "README.md").write_text("changed\n", encoding="utf-8"),
    )
    blocked = await guard.before_tool(
        "fs_write", {"path": "server/app/points.py", "content": "x"}, agent=agent, ctx=ctx
    )
    assert blocked is not None and "转交用户决定" in blocked.error
    guard.pop_gate(node_id)


async def _waiting_scope_node(tmp_path: Path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    snap = await store.load_program(pid)
    node = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "impl")
    await store.update_node(
        node["id"],
        status="waiting_human",
        conflict_json={
            "kind": "scope_violation",
            "paths": ["README.md"],
            "command": "make docs",
            "reason": "命令改动了本步骤范围之外的文件：README.md",
        },
    )
    return rt, pid, node["id"]


async def test_include_adds_the_files_to_scope_and_reruns_the_node(tmp_path):
    rt, pid, node_id = await _waiting_scope_node(tmp_path)

    await rt.decide_gate(pid, "backend", "impl", "include", "只改了说明文件")

    snap = await store.load_program(pid)
    node = next(n for f in snap["flows"] for n in f["nodes"] if n["id"] == node_id)
    assert node["status"] == "pending"
    assert "README.md" in node["write_scope"]
    assert node["pending"]["mode"] == "rework"
    assert "README.md" in node["pending"]["feedback"]
    assert "只改了说明文件" in node["pending"]["feedback"]
    assert node["conflict"] is None


async def test_abort_cancels_the_flow_and_approve_is_refused(tmp_path):
    rt, pid, _node_id = await _waiting_scope_node(tmp_path)

    with pytest.raises(ValueError):
        await rt.decide_gate(pid, "backend", "impl", "approve")

    await rt.decide_gate(pid, "backend", "impl", "abort")
    snap = await store.load_program(pid)
    assert snap["flows"][0]["run"]["status"] == "cancelled"

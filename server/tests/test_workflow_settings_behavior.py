"""工作流设置项的真实效果（设置 → 工作流）：检查是否必须通过、自动合并开关、同文件冲突策略。"""

from typing import Any

import pytest

from app.core.config import settings
from app.gateway.routers.settings import _conflict_policy, _isolation_default
from app.orchestration.tools.base import ToolResult
from app.orchestration.workflow import conflicts, guard, store
from tests.test_workflow_guard import _agent, _ctx, _impl_node
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _node_status,
    _runtime,
    _session,
)


@pytest.fixture(autouse=True)
def _reset_guard_state(monkeypatch):
    guard._LEDGERS.clear()
    guard._CONFLICTS.clear()
    guard._GATES.clear()
    monkeypatch.setattr(guard, "_READ_INDEX", conflicts.ReadIndex())
    yield
    guard._LEDGERS.clear()
    guard._CONFLICTS.clear()
    guard._GATES.clear()


async def _stale_read(tmp_path, agent_id: int, other_change: bytes):
    """本节点读取了 points.py，随后其它节点改动了它：返回 (节点, 代理, 上下文, 文件)。"""
    sid, _pid, node_id = await _impl_node(tmp_path)
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"l1\nl2\nl3\nl4\nl5\n")  # 字节级写入，不受平台换行转换影响
    agent, ctx = _agent(agent_id, node_id), _ctx(tmp_path, sid, agent_id)
    await guard.after_tool(
        "fs_read",
        {"path": "server/app/points.py"},
        agent=agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="read"),
    )
    target.write_bytes(other_change)
    return node_id, agent, ctx, target


# ── 自动合并开关 ─────────────────────────────────────────────


async def test_non_overlapping_changes_merge_by_default(tmp_path):
    _node, agent, ctx, _target = await _stale_read(tmp_path, 21, b"L1-other\nl2\nl3\nl4\nl5\n")
    args = {"path": "server/app/points.py", "content": "l1\nl2\nl3\nl4\nL5-mine\n"}

    assert await guard.before_tool("fs_write", args, agent=agent, ctx=ctx) is None
    assert args["content"] == "L1-other\nl2\nl3\nl4\nL5-mine\n"


async def test_auto_merge_switch_off_keeps_the_write_for_a_structured_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workflow_auto_merge", False)
    _node, agent, ctx, _target = await _stale_read(tmp_path, 22, b"L1-other\nl2\nl3\nl4\nl5\n")
    args = {"path": "server/app/points.py", "content": "l1\nl2\nl3\nl4\nL5-mine\n"}

    result = await guard.before_tool("fs_write", args, agent=agent, ctx=ctx)

    assert result is not None and result.data.get("conflict") is True
    assert args["content"] == "l1\nl2\nl3\nl4\nL5-mine\n", "关闭自动合并时不得改写待写入内容"


# ── 同文件冲突策略 ───────────────────────────────────────────


async def test_queue_policy_retries_before_asking_the_user(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workflow_conflict_policy", "queue")
    node_id, agent, ctx, _target = await _stale_read(tmp_path, 23, b"l1\nB-other\nl3\nl4\nl5\n")
    pending = {"path": "server/app/points.py", "content": "l1\nB-mine\nl3\nl4\nl5\n"}

    first = await guard.before_tool("fs_write", dict(pending), agent=agent, ctx=ctx)

    assert first is not None and first.data.get("conflict") is True
    assert first.data.get("gated") is None
    assert guard.pop_gate(node_id) is None


async def test_ask_policy_asks_the_user_on_the_first_real_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workflow_conflict_policy", "ask")
    node_id, agent, ctx, _target = await _stale_read(tmp_path, 24, b"l1\nB-other\nl3\nl4\nl5\n")
    pending = {"path": "server/app/points.py", "content": "l1\nB-mine\nl3\nl4\nl5\n"}

    first = await guard.before_tool("fs_write", dict(pending), agent=agent, ctx=ctx)

    assert first is not None and first.data.get("gated") is True
    gate = guard.pop_gate(node_id)
    assert gate is not None and gate["kind"] == "conflict"
    assert "B-other" in gate["current"] and "B-mine" in gate["pending"]


# ── 评估的确定性检查 ─────────────────────────────────────────


async def _review_with_failing_check(tmp_path, monkeypatch, *, require: bool):
    monkeypatch.setattr(settings, "workflow_require_checks", require)

    async def failing(command: str, cwd: str) -> dict[str, Any]:
        return {
            "cmd": command,
            "ok": False,
            "exit_code": 1,
            "output": "FAILED tests/test_points.py",
        }

    rt = _runtime(ScriptedExecutor(), command_runner=failing)  # 评估者每次都给 pass
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(max_rework=1), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)
    return pid


async def test_failing_check_still_blocks_pass_when_checks_are_required(tmp_path, monkeypatch):
    pid = await _review_with_failing_check(tmp_path, monkeypatch, require=True)

    assert await _node_status(pid, "backend", "review") == "waiting_human"


async def test_failing_check_does_not_block_pass_when_checks_are_optional(tmp_path, monkeypatch):
    pid = await _review_with_failing_check(tmp_path, monkeypatch, require=False)

    assert await _node_status(pid, "backend", "review") == "done"
    snap = await store.load_program(pid)
    review = next(n for n in snap["flows"][0]["nodes"] if n["key"] == "review")
    assert review["checks"][0]["ok"] is False, "检查结果仍然记录，只是不再阻止通过"


# ── 设置值的取值校验 ─────────────────────────────────────────


def test_conflict_policy_accepts_only_known_values():
    assert _conflict_policy("ask") == "ask"
    assert _conflict_policy("queue") == "queue"
    assert _conflict_policy("bogus") == "queue"
    assert _conflict_policy(None) == "queue"


def test_isolation_default_accepts_only_known_values():
    assert _isolation_default("worktree") == "worktree"
    assert _isolation_default("shared") == "shared"
    assert _isolation_default("bogus") == "shared"

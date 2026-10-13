"""工作流写入冲突事件（设计 §5A.1 / §7.4）：L1 自动合并、L2 排队重做、L4 用户决定。

断言以程序事件日志为准（store.list_events）：界面收到的推送与它是同一份数据。
"""

import pytest

from app.orchestration.tools.base import ToolResult
from app.orchestration.workflow import conflict_events, conflicts, guard, store
from tests.test_workflow_guard import _agent, _ctx, _impl_node
from tests.test_workflow_runtime import ScriptedExecutor, _backend_plan, _runtime, _session

PATH = "server/app/points.py"


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


async def _conflict_events(program_id: int, event_type: str | None = None) -> list[dict]:
    events = await store.list_events(program_id)
    return [
        e
        for e in events
        if e["type"].startswith("workflow.conflict.")
        and (event_type is None or e["type"] == event_type)
    ]


async def _read(agent, ctx) -> None:
    await guard.after_tool(
        "fs_read",
        {"path": PATH},
        agent=agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="read"),
    )


async def test_clean_three_way_merge_emits_resolved_l1(tmp_path):
    sid, pid, node_id = await _impl_node(tmp_path)
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"l1\nl2\nl3\nl4\nl5\n")
    agent, ctx = _agent(5, node_id), _ctx(tmp_path, sid, 5)
    await _read(agent, ctx)
    target.write_bytes(b"L1-other\nl2\nl3\nl4\nl5\n")  # 别的节点改了第一行
    args = {"path": PATH, "content": "l1\nl2\nl3\nl4\nL5-mine\n"}  # 本节点改了最后一行

    assert await guard.before_tool("fs_write", args, agent=agent, ctx=ctx) is None

    events = await _conflict_events(pid)
    assert [(e["type"], e["payload"]["level"], e["payload"]["path"]) for e in events] == [
        (conflict_events.RESOLVED, "L1", PATH)
    ]
    assert events[0]["payload"]["result"] == "auto_merged"
    assert events[0]["node_key"] == "impl"


async def test_hard_conflict_is_detected_as_l2_and_resolved_by_a_clean_redo(tmp_path):
    sid, pid, node_id = await _impl_node(tmp_path)
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_text("a\nb\nc\n", encoding="utf-8")
    agent, ctx = _agent(6, node_id), _ctx(tmp_path, sid, 6)
    await _read(agent, ctx)
    target.write_text("a\nB-other\nc\n", encoding="utf-8")  # 同一行被别的节点改动
    stale = {"path": PATH, "content": "a\nB-mine\nc\n"}

    first = await guard.before_tool("fs_write", stale, agent=agent, ctx=ctx)

    assert first is not None and first.data.get("conflict") is True
    detected = await _conflict_events(pid, conflict_events.DETECTED)
    detected_view = [
        (e["payload"]["level"], e["payload"]["path"], e["payload"]["attempts"]) for e in detected
    ]
    assert detected_view == [("L2", PATH, 1)]
    assert await _conflict_events(pid, conflict_events.RESOLVED) == []

    # 基于最新内容重读后重做，写入成功即结束 L2
    await _read(agent, ctx)
    redo = {"path": PATH, "content": "a\nB-other\nC-mine\n"}
    assert await guard.before_tool("fs_write", redo, agent=agent, ctx=ctx) is None
    await guard.after_tool(
        "fs_write", redo, agent=agent, ctx=ctx, result=ToolResult(ok=True, output="written")
    )

    resolved = await _conflict_events(pid, conflict_events.RESOLVED)
    resolved_view = [
        (e["payload"]["level"], e["payload"]["path"], e["payload"]["result"]) for e in resolved
    ]
    assert resolved_view == [("L2", PATH, "redone")]


async def test_gate_on_conflict_emits_l4_detected_and_the_decision_resolves_it(tmp_path):
    def _gate_on_run(req) -> None:
        guard._GATES[req.node_id] = {
            "kind": "conflict",
            "path": PATH,
            "attempts": 3,
            "base": "a",
            "current": "b",
            "pending": "c",
            "reason": "多次冲突，无法自动合并",
        }

    class _GateExecutor(ScriptedExecutor):
        async def run(self, req):
            if req.node_key == "impl":
                from app.orchestration.workflow.executor import NodeResult

                _gate_on_run(req)
                return NodeResult(status="done", output={"summary": "已尝试"}, text="done")
            return await super().run(req)

    rt = _runtime(_GateExecutor())
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    # 冲突闸门先经 L3 合并步骤（合并预算内可重复），预算用尽后才进入 L4 用户决定
    detected = await _conflict_events(pid, conflict_events.DETECTED)
    levels = [e["payload"]["level"] for e in detected]
    assert levels[0] == "L3" and levels[-1] == "L4"
    assert detected[-1]["payload"]["path"] == PATH
    resolved_before = await _conflict_events(pid, conflict_events.RESOLVED)
    assert {e["payload"]["level"] for e in resolved_before} == {"L3"}

    await rt.decide_gate(pid, "backend", "impl", "rework", "请保留对方的改动")

    resolved = await _conflict_events(pid, conflict_events.RESOLVED)
    l4_view = [
        (e["payload"]["level"], e["payload"]["decision"])
        for e in resolved
        if e["payload"]["level"] == "L4"
    ]
    assert l4_view == [("L4", "rework")]

"""工作流节点的工具层约束（设计 §5A.1）：写入范围、读写一致性（CAS）、冲突闸门、git 状态、
申请扩展写入范围、读集失效通知。"""

from types import SimpleNamespace

import pytest

from app.orchestration.tools.base import ToolResult
from app.orchestration.workflow import conflicts, guard, store
from app.orchestration.workflow.conflicts import LeaseOwner, get_lease_table
from app.orchestration.workflow.executor import NodeResult
from tests.test_workflow_runtime import ScriptedExecutor, _backend_plan, _runtime, _session


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


async def _impl_node(tmp_path, scope: str = "server/app/points.py"):
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    pid = await rt.create_program(
        session_id=sid, plan=_backend_plan(scope=scope), workspace=str(tmp_path)
    )
    snap = await store.load_program(pid)
    node = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "impl")
    return sid, pid, node["id"]


def _agent(agent_id: int, node_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(id=agent_id, workflow_node_id=node_id)


def _ctx(tmp_path, sid: int, agent_id: int) -> SimpleNamespace:
    return SimpleNamespace(workspace_root=str(tmp_path), session_id=sid, agent_id=agent_id)


async def test_write_outside_the_node_scope_is_refused(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)
    agent, ctx = _agent(1, node_id), _ctx(tmp_path, sid, 1)

    refused = await guard.before_tool(
        "fs_write", {"path": "server/app/other.py", "content": "x"}, agent=agent, ctx=ctx
    )
    allowed = await guard.before_tool(
        "fs_write", {"path": "server/app/points.py", "content": "x"}, agent=agent, ctx=ctx
    )

    assert refused is not None and refused.ok is False
    assert "不在本节点的写入范围内" in refused.error
    assert refused.data["out_of_scope"] == "server/app/other.py"
    assert allowed is None


async def test_paths_escaping_the_workspace_are_refused(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path, scope="**")
    result = await guard.before_tool(
        "fs_write",
        {"path": "../escape.py", "content": "x"},
        agent=_agent(1, node_id),
        ctx=_ctx(tmp_path, sid, 1),
    )
    assert result is not None and "不在工作区内" in result.error


async def test_plain_agents_are_not_constrained(tmp_path):
    sid, _pid, _node_id = await _impl_node(tmp_path)
    result = await guard.before_tool(
        "fs_write",
        {"path": "anywhere/else.py", "content": "x"},
        agent=_agent(2, None),
        ctx=_ctx(tmp_path, sid, 2),
    )
    assert result is None


async def test_directory_and_recursive_scopes_match_nested_files(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path, scope="client/src/components/points/**")
    agent, ctx = _agent(3, node_id), _ctx(tmp_path, sid, 3)

    nested = await guard.before_tool(
        "fs_write",
        {"path": "client/src/components/points/a/b.tsx", "content": ""},
        agent=agent,
        ctx=ctx,
    )
    sibling = await guard.before_tool(
        "fs_write",
        {"path": "client/src/components/pointsX.tsx", "content": ""},
        agent=agent,
        ctx=ctx,
    )

    assert nested is None
    assert sibling is not None


async def test_stale_read_is_a_structured_conflict_then_the_gate_after_repeats(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_text("a\nb\nc\n", encoding="utf-8")
    agent, ctx = _agent(4, node_id), _ctx(tmp_path, sid, 4)
    await guard.after_tool(
        "fs_read",
        {"path": "server/app/points.py"},
        agent=agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="read"),
    )
    target.write_text("a\nB-other\nc\n", encoding="utf-8")  # 其它节点在读取之后改动了同一行

    pending = {"path": "server/app/points.py", "content": "a\nB-mine\nc\n"}  # 本节点也改了这一行
    first = await guard.before_tool("fs_write", dict(pending), agent=agent, ctx=ctx)
    assert first is not None and first.data.get("conflict") is True
    assert "重新读取" in first.error
    assert guard.pop_gate(node_id) is None

    # 连续冲突达到上限后转入用户决定（第三次）
    await guard.before_tool("fs_write", dict(pending), agent=agent, ctx=ctx)
    third = await guard.before_tool("fs_write", dict(pending), agent=agent, ctx=ctx)
    assert third is not None and third.data.get("gated") is True
    gate = guard.pop_gate(node_id)
    assert gate is not None and gate["kind"] == "conflict"
    assert gate["path"] == "server/app/points.py"
    assert "B-other" in gate["current"]
    assert "B-mine" in gate["pending"]


async def test_full_file_write_is_merged_when_the_changes_do_not_overlap(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"l1\nl2\nl3\nl4\nl5\n")  # 字节级写入，不受平台换行转换影响
    agent, ctx = _agent(5, node_id), _ctx(tmp_path, sid, 5)
    await guard.after_tool(
        "fs_read",
        {"path": "server/app/points.py"},
        agent=agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="read"),
    )
    target.write_bytes(b"L1-other\nl2\nl3\nl4\nl5\n")  # 别人改了第一行
    args = {
        "path": "server/app/points.py",
        "content": "l1\nl2\nl3\nl4\nL5-mine\n",
    }  # 我改了最后一行

    result = await guard.before_tool("fs_write", args, agent=agent, ctx=ctx)

    assert result is None, "两侧改动区域不重叠，应自动三方合并"
    assert args["content"] == "L1-other\nl2\nl3\nl4\nL5-mine\n"


async def test_crlf_files_merge_when_the_model_writes_lf_content(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"l1\r\nl2\r\nl3\r\nl4\r\nl5\r\n")
    agent, ctx = _agent(14, node_id), _ctx(tmp_path, sid, 14)
    await guard.after_tool(
        "fs_read",
        {"path": "server/app/points.py"},
        agent=agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="read"),
    )
    target.write_bytes(b"L1-other\r\nl2\r\nl3\r\nl4\r\nl5\r\n")
    args = {"path": "server/app/points.py", "content": "l1\nl2\nl3\nl4\nL5-mine\n"}

    result = await guard.before_tool("fs_write", args, agent=agent, ctx=ctx)

    assert result is None
    assert args["content"] == "L1-other\r\nl2\r\nl3\r\nl4\r\nL5-mine\r\n"


async def test_anchor_based_edits_are_left_to_the_tool_anchor_check(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")
    agent, ctx = _agent(6, node_id), _ctx(tmp_path, sid, 6)
    await guard.after_tool(
        "fs_read",
        {"path": "server/app/points.py"},
        agent=agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="read"),
    )
    target.write_text("x = 2\n", encoding="utf-8")

    result = await guard.before_tool(
        "editor_apply_diff", {"path": "server/app/points.py"}, agent=agent, ctx=ctx
    )

    assert result is None, "锚点式编辑由工具按唯一锚点校验，不在约束层直接拒绝"


async def test_own_writes_keep_the_baseline_fresh(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_text("v1\n", encoding="utf-8")
    agent, ctx = _agent(7, node_id), _ctx(tmp_path, sid, 7)
    await guard.after_tool(
        "fs_read",
        {"path": "server/app/points.py"},
        agent=agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="read"),
    )
    target.write_text("v2\n", encoding="utf-8")
    await guard.after_tool(
        "fs_write",
        {"path": "server/app/points.py", "content": "v2\n"},
        agent=agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="wrote"),
    )

    again = await guard.before_tool(
        "fs_write", {"path": "server/app/points.py", "content": "v3\n"}, agent=agent, ctx=ctx
    )

    assert again is None, "本节点刚写入的内容即是新基线，连续编辑不应误判冲突"


async def test_shell_commands_writing_outside_the_scope_are_refused(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)
    agent, ctx = _agent(15, node_id), _ctx(tmp_path, sid, 15)

    refused = await guard.before_tool(
        "terminal_exec", {"command": "echo x > server/app/other.py"}, agent=agent, ctx=ctx
    )
    allowed = await guard.before_tool(
        "terminal_exec", {"command": "echo x > server/app/points.py"}, agent=agent, ctx=ctx
    )

    assert refused is not None and "范围之外" in refused.error
    assert allowed is None


async def test_git_state_changing_commands_are_refused_for_workflow_nodes(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)
    agent, ctx = _agent(8, node_id), _ctx(tmp_path, sid, 8)

    checkout = await guard.before_tool(
        "terminal_exec", {"command": "git checkout -- server/app/points.py"}, agent=agent, ctx=ctx
    )
    commit = await guard.before_tool("git", {"command": "commit -m wip"}, agent=agent, ctx=ctx)
    status = await guard.before_tool("git", {"command": "status --short"}, agent=agent, ctx=ctx)
    diff = await guard.before_tool(
        "terminal_exec", {"command": "git diff --stat"}, agent=agent, ctx=ctx
    )

    assert checkout is not None and "改变仓库状态" in checkout.error
    assert commit is not None
    assert status is None and diff is None


async def test_request_write_scope_grants_free_paths_and_refuses_held_ones(tmp_path):
    sid, _pid, node_id = await _impl_node(tmp_path)
    agent, ctx = _agent(9, node_id), _ctx(tmp_path, sid, 9)
    table = get_lease_table()
    other = LeaseOwner(run_id=999999, node_key="front", session_id=sid)
    table.acquire(str(tmp_path), other, ("client/src/shared.ts",))
    try:
        granted = await guard.before_tool(
            guard.REQUEST_SCOPE_TOOL, {"paths": ["server/app/extra.py"]}, agent=agent, ctx=ctx
        )
        held = await guard.before_tool(
            guard.REQUEST_SCOPE_TOOL, {"paths": ["client/src/shared.ts"]}, agent=agent, ctx=ctx
        )
    finally:
        table.release(str(tmp_path), other)

    assert granted is not None and granted.ok is True
    assert "server/app/extra.py" in granted.data["write_scope"]
    write = await guard.before_tool(
        "fs_write", {"path": "server/app/extra.py", "content": ""}, agent=agent, ctx=ctx
    )
    assert write is None, "授予后的路径纳入节点范围"
    assert held is not None and held.ok is False and "被其它节点占用" in held.error


async def test_request_write_scope_is_only_for_workflow_nodes(tmp_path):
    sid, _pid, _node_id = await _impl_node(tmp_path)
    result = await guard.before_tool(
        guard.REQUEST_SCOPE_TOOL,
        {"paths": ["a.py"]},
        agent=_agent(10, None),
        ctx=_ctx(tmp_path, sid, 10),
    )
    assert result is not None and result.ok is False


async def test_a_write_notifies_other_attempts_that_read_the_file(tmp_path, monkeypatch):
    sid, _pid, node_id = await _impl_node(tmp_path)
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_text("v1\n", encoding="utf-8")
    reader_agent = _agent(11, node_id)
    writer_agent = _agent(12, node_id)
    ctx = _ctx(tmp_path, sid, 0)
    await guard.after_tool(
        "fs_read",
        {"path": "server/app/points.py"},
        agent=reader_agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="read"),
    )

    pushed: list[tuple[int, str]] = []

    class _Manager:
        def push_agent_input(self, agent_id: int, message: str) -> bool:
            pushed.append((agent_id, message))
            return True

    from app.orchestration import subagent as subagent_module

    monkeypatch.setattr(subagent_module, "get_subagent_manager", lambda session_id: _Manager())
    target.write_text("v2\n", encoding="utf-8")
    await guard.after_tool(
        "fs_write",
        {"path": "server/app/points.py", "content": "v2\n"},
        agent=writer_agent,
        ctx=ctx,
        result=ToolResult(ok=True, output="wrote"),
    )

    assert [agent_id for agent_id, _ in pushed] == [11]
    assert "已被节点" in pushed[0][1] and "重新读取" in pushed[0][1]


async def test_guard_failures_fail_closed_for_workflow_writes(tmp_path, monkeypatch):
    sid, _pid, node_id = await _impl_node(tmp_path)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(guard, "_before_tool", boom)
    result = await guard.before_tool(
        "fs_write",
        {"path": "server/app/points.py", "content": ""},
        agent=_agent(13, node_id),
        ctx=_ctx(tmp_path, sid, 13),
    )

    assert result is not None and result.ok is False
    assert "约束检查异常" in result.error


async def test_runtime_turns_a_conflict_gate_into_a_human_decision(tmp_path):
    def _gate_on_run(req) -> NodeResult:
        guard._GATES[req.node_id] = {
            "kind": "conflict",
            "path": "server/app/points.py",
            "attempts": 3,
            "base": "a",
            "current": "b",
            "pending": "c",
            "reason": "多次冲突，无法自动合并",
        }
        return NodeResult(status="done", output={"summary": "已尝试"}, text="done")

    class _GateExecutor(ScriptedExecutor):
        async def run(self, req):
            if req.node_key == "impl":
                return _gate_on_run(req)
            return await super().run(req)

    rt = _runtime(_GateExecutor())
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    snap = await store.load_program(pid)
    impl = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "impl")
    assert impl["status"] == "waiting_human"
    assert impl["conflict"]["path"] == "server/app/points.py"
    assert snap["run"]["status"] == "waiting_human"

    await rt.decide_gate(pid, "backend", "impl", "rework", "请保留对方的改动")
    snap = await store.load_program(pid)
    impl = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "impl")
    assert impl["status"] == "pending"
    assert impl["pending"]["mode"] == "rework"
    assert impl["conflict"] is None

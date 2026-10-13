"""工具调用台账（设计 §6.5）：调用开始即落库、结束补写结果；续跑时对账未知与未记录的操作。"""

import asyncio
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from app.orchestration.tools import executor as exec_mod
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.registry import ToolRegistry
from app.orchestration.workflow import checkpoints, conflicts, guard, store
from app.orchestration.workflow.effects import shell_effect, write_effect
from tests.test_workflow_runtime import ScriptedExecutor, _backend_plan, _runtime, _session


class _EchoTool(Tool):
    name = "fake.echo"
    risk_level = "low"
    description = "回显参数"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output=str(args.get("text", "")))


class _SlowTool(_EchoTool):
    name = "fake.slow"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(30)  # 模拟执行中被中断（进程被结束、节点被暂停）
        return ToolResult(ok=True, output="never")


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


@pytest.fixture
def executor(monkeypatch):
    reg = ToolRegistry()
    reg.register(_EchoTool())
    reg.register(_SlowTool())
    monkeypatch.setattr(exec_mod, "tool_registry", reg)
    return exec_mod.ServerToolExecutor()


async def _workflow_agent(tmp_path) -> tuple[SimpleNamespace, ToolContext, int]:
    """一个工作流节点的尝试：返回 (代理, 上下文, 节点编号)。"""
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    snap = await store.load_program(pid)
    node = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "impl")
    attempt_id = await store.add_attempt(node["id"], 1, "fresh")
    agent = SimpleNamespace(
        id=7, template_id=None, workflow_node_id=node["id"], workflow_attempt_id=attempt_id
    )
    ctx = ToolContext(
        workspace_root=str(tmp_path), session_id=sid, task_id=1, agent_id=7, agent_name="t"
    )
    return agent, ctx, node["id"]


async def test_a_completed_call_is_recorded_with_its_outcome(tmp_path, executor):
    agent, ctx, node_id = await _workflow_agent(tmp_path)

    result = await executor.execute(
        tool_name="fake.echo",
        args={"text": "hi"},
        call_key="k1",
        agent=agent,
        ctx=ctx,
        llm_call_id="call_1",
    )

    assert result.ok and result.output == "hi"
    calls = await store.node_tool_calls(node_id)
    assert [(c["llm_call_id"], c["tool"], c["status"]) for c in calls] == [
        ("call_1", "fake.echo", "done")
    ]


async def test_a_blocked_call_leaves_no_ledger_entry(tmp_path, executor):
    agent, ctx, node_id = await _workflow_agent(tmp_path)

    result = await executor.execute(
        tool_name="fs_write",
        args={"path": "server/app/other.py", "content": "x"},
        call_key="k2",
        agent=agent,
        ctx=ctx,
        llm_call_id="call_blocked",
    )

    assert result.ok is False, "写入范围之外的调用被约束层拦截"
    assert await store.node_tool_calls(node_id) == []


async def test_an_interrupted_call_stays_unknown_until_it_is_reconciled(tmp_path, executor):
    agent, ctx, node_id = await _workflow_agent(tmp_path)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            executor.execute(
                tool_name="fake.slow",
                args={"text": "long"},
                call_key="k3",
                agent=agent,
                ctx=ctx,
                llm_call_id="call_slow",
            ),
            timeout=0.05,
        )

    unknown, unrecorded = await checkpoints.ledger_notes(node_id, [])
    assert unknown == ['fake.slow {"text": "long"}'] and unrecorded == []
    assert await checkpoints.ledger_notes(node_id, []) == ([], []), "已告知过的操作不再重复提示"


async def test_an_executed_call_missing_from_the_transcript_is_reported(tmp_path, executor):
    agent, ctx, node_id = await _workflow_agent(tmp_path)
    await executor.execute(
        tool_name="fake.echo",
        args={"text": "done"},
        call_key="k4",
        agent=agent,
        ctx=ctx,
        llm_call_id="call_4",
    )

    unknown, unrecorded = await checkpoints.ledger_notes(node_id, [])

    assert unknown == []
    assert unrecorded == ['fake.echo {"text": "done"}']


async def test_calls_already_in_the_transcript_are_not_repeated(tmp_path, executor):
    agent, ctx, node_id = await _workflow_agent(tmp_path)
    await executor.execute(
        tool_name="fake.echo",
        args={"text": "x"},
        call_key="k5",
        agent=agent,
        ctx=ctx,
        llm_call_id="call_5",
    )
    transcript = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_5", "name": "fake.echo"}]},
        {"role": "tool", "tool_call_id": "call_5", "name": "fake.echo", "content": "x"},
    ]

    assert await checkpoints.ledger_notes(node_id, transcript) == ([], [])


async def test_calls_outside_workflows_are_not_recorded(tmp_path, executor):
    _agent, ctx, node_id = await _workflow_agent(tmp_path)
    plain = SimpleNamespace(id=9, template_id=None)  # 普通子代理：没有工作流节点与尝试

    await executor.execute(
        tool_name="fake.echo",
        args={"text": "plain"},
        call_key="k6",
        agent=plain,
        ctx=ctx,
        llm_call_id="call_6",
    )

    assert await store.node_tool_calls(node_id) == []


def test_resume_note_lists_both_kinds_of_operations_and_keeps_the_feedback():
    note = checkpoints.resume_note(
        ['fs_write {"path": "a.py"}'],
        ['fake.echo {"text": "done"}'],
        "请改用方案 B",
    )

    assert "结果未知" in note and "fs_write" in note
    assert "已经执行" in note and "fake.echo" in note
    assert note.endswith("请改用方案 B")
    assert checkpoints.resume_note([], [], None) is None


WRITE_ARGS = {"path": "notes/plan.md", "content": "new\n"}
WRITE_PREVIEW = 'fs_write {"path": "notes/plan.md"}'


async def _interrupted_write(tmp_path) -> int:
    """一次写入已经发出（台账已开始）但没有返回结果：返回节点编号。"""
    agent, _ctx, node_id = await _workflow_agent(tmp_path)
    effect = write_effect("fs_write", dict(WRITE_ARGS), str(tmp_path))
    await store.begin_tool_call(
        agent.workflow_attempt_id,
        node_id,
        "call_w",
        "fs_write",
        '{"path": "notes/plan.md"}',
        effect,
    )
    return node_id


async def test_an_interrupted_write_that_never_touched_the_file_is_safe_to_redo(tmp_path):
    node_id = await _interrupted_write(tmp_path)

    unknown, unrecorded = await checkpoints.ledger_notes(node_id, [])

    assert unknown == [f"{WRITE_PREVIEW}（已确认未执行，可以重做）"]
    assert unrecorded == []


async def test_an_interrupted_write_whose_content_is_already_on_disk_is_not_repeated(tmp_path):
    node_id = await _interrupted_write(tmp_path)
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "plan.md").write_bytes(b"new\n")  # 写入已生效，只是结果没有记录下来

    unknown, _ = await checkpoints.ledger_notes(node_id, [])

    assert unknown == [f"{WRITE_PREVIEW}（已确认已执行，无需重做）"]


async def test_an_interrupted_write_to_a_file_someone_else_changed_is_left_to_verify(tmp_path):
    node_id = await _interrupted_write(tmp_path)
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "plan.md").write_bytes(b"edited by the user\n")

    unknown, _ = await checkpoints.ledger_notes(node_id, [])

    assert unknown == [f"{WRITE_PREVIEW}（文件已被改动，无法确定是否执行，需核实）"]


def test_only_file_writes_carry_an_effect(tmp_path):
    assert write_effect("fake.echo", {"text": "x"}, str(tmp_path)) is None
    assert write_effect("shell", {"command": "npm test"}, str(tmp_path)) is None


def _git_repo(root) -> None:
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "README.md").write_bytes(b"readme\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "init"], check=True, capture_output=True
    )


SHELL_ARGS = '{"command": "npm test"}'
SHELL_PREVIEW = f"terminal_exec {SHELL_ARGS}"


async def _interrupted_command(tmp_path) -> int:
    """一条命令已经发出（台账已开始）但没有返回结果，工作区是一个 git 仓库：返回节点编号。"""
    _git_repo(tmp_path)
    agent, _ctx, node_id = await _workflow_agent(tmp_path)
    effect = await shell_effect("terminal_exec", str(tmp_path))
    await store.begin_tool_call(
        agent.workflow_attempt_id, node_id, "call_sh", "terminal_exec", SHELL_ARGS, effect
    )
    return node_id


async def test_an_interrupted_command_that_left_the_workspace_untouched_is_reported_so(tmp_path):
    node_id = await _interrupted_command(tmp_path)

    unknown, _ = await checkpoints.ledger_notes(node_id, [])

    assert unknown == [f"{SHELL_PREVIEW}（未观察到工作区文件改动，是否执行需核实）"]


async def test_an_interrupted_command_that_changed_the_workspace_is_reported_as_likely_executed(
    tmp_path,
):
    node_id = await _interrupted_command(tmp_path)
    (tmp_path / "build.log").write_bytes(b"done\n")  # 命令已经产生了文件

    unknown, _ = await checkpoints.ledger_notes(node_id, [])

    assert unknown == [f"{SHELL_PREVIEW}（工作区文件已有改动，很可能已执行，需核实）"]


async def test_commands_in_a_workspace_that_is_not_git_keep_the_plain_unknown_result(tmp_path):
    agent, _ctx, node_id = await _workflow_agent(tmp_path)
    assert await shell_effect("terminal_exec", str(tmp_path)) is None
    await store.begin_tool_call(
        agent.workflow_attempt_id, node_id, "call_sh2", "terminal_exec", SHELL_ARGS, None
    )

    unknown, _ = await checkpoints.ledger_notes(node_id, [])

    assert unknown == [SHELL_PREVIEW]

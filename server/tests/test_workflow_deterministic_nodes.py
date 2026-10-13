"""确定性的节点（设计 §4.5）：检查节点只运行命令，闸门节点等待用户决定；两者都不调用模型。"""

from app.orchestration.workflow import store
from tests.test_workflow_runtime import ScriptedExecutor, _program_status, _runtime, _session


def _plan(node: dict) -> dict:
    return {
        "title": "确定性节点",
        "goal": "检查与闸门",
        "flows": [
            {
                "key": "backend",
                "title": "后端",
                "nodes": [
                    {"key": "explore", "kind": "explore", "task": "探索"},
                    {"inputs": ["explore"], **node},
                ],
            }
        ],
    }


async def _node(program_id: int, key: str) -> dict:
    snap = await store.load_program(program_id)
    flow = next(f for f in snap["flows"] if f["run"]["package_key"] == "backend")
    return next(n for n in flow["nodes"] if n["key"] == key)


async def test_a_check_node_runs_its_commands_without_calling_a_model(tmp_path):
    ran: list[tuple[str, str]] = []

    async def runner(command: str, cwd: str) -> dict:
        ran.append((command, cwd))
        return {"ok": True, "output": "1 passed"}

    executor = ScriptedExecutor()
    rt = _runtime(executor, command_runner=runner)
    plan = _plan({"key": "verify", "kind": "check", "commands": ["pytest -q"], "task": "跑测试"})
    pid = await rt.create_program(session_id=await _session(), plan=plan, workspace=str(tmp_path))

    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    assert ran == [("pytest -q", str(tmp_path))]
    assert [key for key, *_ in executor.calls] == ["explore"], "检查节点不应调用模型"
    verify = await _node(pid, "verify")
    assert verify["output"]["checks"][0]["ok"] is True


async def test_a_failing_check_fails_its_step(tmp_path):
    async def runner(command: str, cwd: str) -> dict:
        return {"ok": False, "output": "2 failed"}

    rt = _runtime(ScriptedExecutor(), command_runner=runner)
    plan = _plan({"key": "verify", "kind": "check", "commands": ["pytest -q"], "task": "跑测试"})
    pid = await rt.create_program(session_id=await _session(), plan=plan, workspace=str(tmp_path))

    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "failed"
    verify = await _node(pid, "verify")
    assert verify["status"] == "failed"
    assert verify["reason"] == "检查未通过：pytest -q"


async def test_a_gate_waits_for_the_user_and_continues_on_approval(tmp_path):
    executor = ScriptedExecutor()
    rt = _runtime(executor)
    plan = _plan(
        {"key": "confirm", "kind": "gate", "question": "是否继续进入实现？", "task": "确认"}
    )
    pid = await rt.create_program(session_id=await _session(), plan=plan, workspace=str(tmp_path))

    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "waiting_human"
    gate = await _node(pid, "confirm")
    assert gate["status"] == "waiting_human"
    assert gate["reason"] == "是否继续进入实现？"
    assert [key for key, *_ in executor.calls] == ["explore"], "闸门不应调用模型"

    await rt.decide_gate(pid, "backend", "confirm", "approve")
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    assert (await _node(pid, "confirm"))["status"] == "done"


async def test_a_gate_aborted_by_the_user_fails_its_flow(tmp_path):
    rt = _runtime(ScriptedExecutor())
    plan = _plan({"key": "confirm", "kind": "gate", "question": "是否继续？", "task": "确认"})
    pid = await rt.create_program(session_id=await _session(), plan=plan, workspace=str(tmp_path))
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    await rt.decide_gate(pid, "backend", "confirm", "abort")

    assert (await _node(pid, "confirm"))["status"] == "failed"
    assert await _program_status(pid) == "failed"

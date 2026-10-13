"""工作流增量检查点日志（设计 §6.2）：每步记录、压缩重建、进程结束后还原、安装到子代理行。"""

from app.orchestration.workflow import checkpoints, store
from app.persistence.database import run_write_locked
from app.persistence.models.agent import Agent as AgentRow
from tests.test_workflow_runtime import ScriptedExecutor, _backend_plan, _runtime, _session

SPEC = {"workspace": "", "tool_names": ["fs_read"], "subagent_mode": "default"}


async def _impl_node(tmp_path):
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    snap = await store.load_program(pid)
    node = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "impl")
    return sid, node["id"]


async def test_step_log_rebuilds_the_latest_context_after_a_restart(tmp_path):
    _sid, node_id = await _impl_node(tmp_path)
    attempt_id = await store.add_attempt(node_id, 1, "fresh")
    recorder = checkpoints.StepRecorder(attempt_id)
    m1 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "任务"}]
    m2 = m1 + [{"role": "assistant", "content": "第一步"}, {"role": "tool", "content": "输出一"}]
    m3 = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "压缩后的摘要"},
    ]  # 压缩重建
    m4 = m3 + [{"role": "assistant", "content": "第二步"}]

    recorder.record(m1, SPEC)
    recorder.record(m2, SPEC)
    recorder.record(m3, SPEC)
    recorder.record(m4, SPEC)
    await recorder._last  # 写入按提交顺序串行完成，等最后一个即可

    restored = await checkpoints.materialize(node_id)
    assert restored is not None
    assert restored["messages"] == m4
    assert restored["spec"] == SPEC


async def test_a_new_attempt_does_not_restore_its_own_partial_log(tmp_path):
    _sid, node_id = await _impl_node(tmp_path)
    first = await store.add_attempt(node_id, 1, "fresh")
    recorder = checkpoints.StepRecorder(first)
    old = [{"role": "system", "content": "sys"}, {"role": "user", "content": "旧任务"}]
    recorder.record(old, SPEC)
    await recorder._last

    second = await store.add_attempt(node_id, 2, "checkpoint")

    restored = await checkpoints.materialize(node_id, exclude_attempt_id=second)
    assert restored is not None and restored["messages"] == old


async def test_nothing_to_restore_without_a_log(tmp_path):
    _sid, node_id = await _impl_node(tmp_path)
    await store.add_attempt(node_id, 1, "fresh")
    assert await checkpoints.materialize(node_id) is None


async def test_install_only_replaces_a_less_complete_checkpoint(tmp_path):
    sid, _node_id = await _impl_node(tmp_path)

    def _create(s):
        row = AgentRow(kind="sub", name="日志还原", session_id=sid, status="paused")
        s.add(row)
        s.flush()
        agent_id = row.id
        s.commit()
        return agent_id

    agent_id = await run_write_locked(_create, label="test.agent")
    short = {"messages": [{"role": "user", "content": "a"}], "spec": SPEC}
    full = {
        "messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        "spec": SPEC,
    }

    assert await checkpoints.install(agent_id, full) is True
    assert await checkpoints.install(agent_id, short) is False, "较旧的日志不能覆盖更完整的检查点"


# ── 续跑结果对账（设计 §6.5）：已发出但没有结果的工具调用如实标注，不假定结果 ──


def test_calls_without_results_are_marked_unknown_right_after_the_call():
    from app.orchestration.workflow.checkpoints import reconcile_unknown_results

    messages = [
        {"role": "user", "content": "开始"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "name": "fs_write", "arguments": {"path": "a.py"}},
                {"id": "c2", "name": "fs_read", "arguments": {"path": "b.py"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "name": "fs_read", "content": "b"},
    ]

    out, unknown = reconcile_unknown_results(messages)

    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool"]
    assert out[2]["tool_call_id"] == "c1" and "结果未知" in out[2]["content"]
    assert out[3]["tool_call_id"] == "c2" and out[3]["content"] == "b"
    assert unknown == ['fs_write {"path": "a.py"}']


def test_complete_transcripts_are_left_alone():
    from app.orchestration.workflow.checkpoints import reconcile_unknown_results

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "fs_read"}]},
        {"role": "tool", "tool_call_id": "c1", "name": "fs_read", "content": "x"},
    ]

    out, unknown = reconcile_unknown_results(messages)

    assert out == messages and unknown == []


def test_resume_note_lists_unknown_operations_and_keeps_the_feedback():
    from app.orchestration.workflow.checkpoints import resume_note

    note = resume_note(['fs_write {"path": "a.py"}'], [], "请改用方案 B")

    assert "结果未知" in note and "fs_write" in note
    assert note.endswith("请改用方案 B")
    assert resume_note([], [], None) is None

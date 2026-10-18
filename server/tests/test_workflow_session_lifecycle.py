"""会话生命周期与工作流（设计 §5.3）、事件保留（§5.2）、主代理的续跑规则（§6.7、§6.9）。"""

from app.orchestration.prompts.main import MAIN_SYSTEM_PROMPT
from app.orchestration.workflow import store
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _program_status,
    _runtime,
    _session,
)


async def test_archiving_a_session_pauses_its_running_workflows_and_keeps_their_progress(tmp_path):
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    running = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    await rt.start(running)
    other_session_draft = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )

    paused = await rt.pause_session_programs(
        sid, kind="archived", reason="会话已归档，已保留进度，恢复会话后可继续"
    )

    assert paused == 1
    snap = await store.load_program(running)
    assert snap["run"]["status"] == "paused"
    assert snap["run"]["pause_kind"] == "archived"
    assert (await store.load_program(other_session_draft))["run"]["status"] == "draft"


async def test_deleting_a_session_cancels_its_unfinished_workflows_only(tmp_path):
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    draft = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    finished = await rt.create_program(
        session_id=sid, plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(finished)
    await rt.run_until_idle(timeout=20)
    assert await _program_status(finished) == "succeeded"

    cancelled = await rt.cancel_session_programs(sid)

    assert cancelled == 1
    assert (await store.load_program(draft))["run"]["status"] == "cancelled"
    assert (await store.load_program(finished))["run"]["status"] == "succeeded"


async def test_the_event_log_keeps_only_the_latest_entries_of_a_run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "EVENT_RETENTION", 5)
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    for i in range(12):
        await store.emit_event(
            pid, "workflow.log.appended", None, {"text": f"第{i}条", "level": "info"}
        )

    events = await store.list_events(pid, 0, 100)

    assert len(events) == 5
    assert events[-1]["payload"]["text"] == "第11条"


def test_the_prompt_tells_the_main_agent_not_to_resume_on_unrelated_requests():
    # plan-156-619 S9：工作流提示词改为中文「拼积木」口径，
    # 断言随之改为语义等价的表述（不再匹配英文原句）；
    # 关键词用中文引号「」——与提示词源文写法保持一致。
    assert "不要自动续跑任何暂停的工作流" in MAIN_SYSTEM_PROMPT
    assert "「全部」" in MAIN_SYSTEM_PROMPT
    assert "「算了」" in MAIN_SYSTEM_PROMPT

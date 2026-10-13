"""子代理面板的工作流节点头（设计 §7.6）：节点、子工作流与程序、尝试模式、评估结论与产出摘要。"""

from app.orchestration.workflow import store
from app.orchestration.workflow.executor import NodeResult
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _runtime,
    _session,
)


async def test_node_briefs_describe_the_latest_attempt_verdict_and_output(tmp_path):
    executor = ScriptedExecutor(
        {"impl": [NodeResult(status="done", output={"summary": "实现完成"})]}
    )
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    snap = await store.load_program(pid)
    nodes = {n["key"]: n for f in snap["flows"] for n in f["nodes"]}
    briefs = await store.node_briefs([nodes["impl"]["id"], nodes["review"]["id"]])

    impl = briefs[nodes["impl"]["id"]]
    assert impl["mode"] == "新建"
    assert impl["summary"] == "实现完成"
    assert impl["flow_title"] == "后端"
    assert impl["program_id"] == pid and impl["program_title"] == "积分兑换"

    review = briefs[nodes["review"]["id"]]
    assert review["verdict"] == "pass"
    assert review["kind"] == "review"


async def test_node_briefs_ignore_unknown_ids():
    assert await store.node_briefs([]) == {}
    assert await store.node_briefs([987654]) == {}


async def test_session_state_shows_the_most_urgent_workflow(tmp_path):
    sid = await _session()
    rt = _runtime(ScriptedExecutor())
    draft = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    assert (await store.session_workflow_states([sid]))[sid] == "draft"

    paused = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    await store.update_run(paused, status="paused")
    assert (await store.session_workflow_states([sid]))[sid] == "paused"

    waiting = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    await store.update_run(waiting, status="waiting_human")
    assert (await store.session_workflow_states([sid]))[sid] == "attention"

    await store.update_run(waiting, status="succeeded")
    await store.update_run(paused, status="cancelled")
    assert (await store.session_workflow_states([sid]))[sid] == "draft"
    assert draft is not None
    assert await store.session_workflow_states([]) == {}


async def test_the_session_list_carries_the_workflow_state_for_the_sidebar(tmp_path):
    from app.gateway.routers.sessions import list_sessions
    from app.persistence.database import async_session_factory

    sid = await _session()
    plain = await _session()
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))

    async with async_session_factory() as db:
        rows = await list_sessions(db=db)
    states = {row.id: row.workflow_state for row in rows}

    assert states[sid] == "draft", "有待确认的工作流的会话带上侧栏标记"
    assert states[plain] is None, "没有工作流的会话不带标记"

    await store.update_run(pid, status="running")
    async with async_session_factory() as db:
        rows = await list_sessions(db=db)
    assert {row.id: row.workflow_state for row in rows}[sid] == "running", "进行中的工作流"

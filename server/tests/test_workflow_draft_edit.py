"""草稿计划的编辑（设计 §7.7、§7.8）：替换子工作流的模板、按调整取消的原因，以及新建模板。"""

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import workflows as workflows_router
from app.orchestration.workflow import presets, store
from app.orchestration.workflow.runtime import WorkflowPlanInvalidError
from app.orchestration.workflow.tools import validate_plan_for_run
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _runtime,
    _session,
)


def _preset_flow(key: str, flow: str | None = None) -> dict:
    plan = presets.preset_plan(key)
    if flow is None:
        return plan["flows"][0]
    return next(f for f in plan["flows"] if f["key"] == flow)


def _two_flow_plan() -> dict:
    """两个互不依赖的子工作流，写入范围各自独立（可以并行）。"""
    plan = _backend_plan(scope="server/app/points.py")
    plan["flows"].append(
        {
            "key": "frontend",
            "title": "前端",
            "nodes": [
                {"key": "explore", "kind": "explore", "task": "探索兑换页"},
                {
                    "key": "impl",
                    "kind": "execute",
                    "inputs": ["explore"],
                    "write_scope": ["client/src/points.ts"],
                    "acceptance": ["兑换页可用"],
                    "task": "实现兑换页",
                },
                {
                    "key": "review",
                    "kind": "review",
                    "inputs": ["impl"],
                    "rework_to": "impl",
                    "max_rework": 1,
                    "acceptance": ["页面通过评估"],
                    "task": "评估兑换页",
                },
            ],
        }
    )
    return plan


async def test_a_draft_flow_takes_the_nodes_of_another_template_and_keeps_its_name(tmp_path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    source = _preset_flow("fix_and_verify")

    await rt.replace_draft_flow(
        pid, "backend", source["nodes"], "修复并验证 · 主流程", validate=validate_plan_for_run
    )

    snap = await store.load_program(pid)
    assert snap["run"]["status"] == "draft"
    assert snap["run"]["plan_rev"] == 1, "替换模板不占用运行中修订的次数"
    flow = next(f for f in snap["run"]["graph"]["flows"] if f["key"] == "backend")
    assert flow["title"] == "后端"
    assert [n["key"] for n in flow["nodes"]] == [n["key"] for n in source["nodes"]]
    stored = next(f for f in snap["flows"] if f["run"]["package_key"] == "backend")
    assert [n["key"] for n in stored["nodes"]] == [n["key"] for n in source["nodes"]]
    events = await store.list_events(pid)
    assert any("改用模板『修复并验证 · 主流程』" in str(e.get("payload")) for e in events)


async def test_a_swap_that_breaks_the_plan_is_refused_and_nothing_changes(tmp_path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_two_flow_plan(), workspace=str(tmp_path)
    )
    # 把前端换成写入后端文件的步骤：两个子工作流并行时写入范围重叠
    clashing = _backend_plan(scope="server/app/points.py")["flows"][0]["nodes"]

    with pytest.raises(WorkflowPlanInvalidError) as caught:
        await rt.replace_draft_flow(
            pid, "frontend", clashing, "后端 · 实现", validate=validate_plan_for_run
        )

    assert any(
        e["code"] in {"write_scope_overlap", "hot_file_overlap"} for e in caught.value.errors
    )
    assert all(e["message"] and "path" in e for e in caught.value.errors)
    snap = await store.load_program(pid)
    frontend = next(f for f in snap["run"]["graph"]["flows"] if f["key"] == "frontend")
    assert [n["key"] for n in frontend["nodes"]] == ["explore", "impl", "review"]
    impl = next(n for n in frontend["nodes"] if n["key"] == "impl")
    assert impl["write_scope"] == ["client/src/points.ts"]


async def test_only_a_draft_can_have_its_flow_templates_swapped(tmp_path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.pause(pid)

    with pytest.raises(ValueError, match="只有待确认的计划"):
        await rt.replace_draft_flow(
            pid,
            "backend",
            _preset_flow("fix_and_verify")["nodes"],
            "x",
            validate=validate_plan_for_run,
        )


async def test_the_generated_integration_flow_cannot_be_swapped(tmp_path):
    plan = _backend_plan()
    plan["after"] = {"acceptance": ["兑换流程端到端可用"], "commands": ["pytest -q"]}
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(session_id=await _session(), plan=plan, workspace=str(tmp_path))

    with pytest.raises(ValueError, match="集成验收"):
        await rt.replace_draft_flow(
            pid,
            "integration",
            _preset_flow("fix_and_verify")["nodes"],
            "x",
            validate=validate_plan_for_run,
        )


async def test_a_draft_cancelled_for_an_adjustment_is_recorded_as_adjusted(tmp_path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )

    await rt.cancel(pid, reason="adjusted")

    snap = await store.load_program(pid)
    assert snap["run"]["status"] == "cancelled"
    assert snap["run"]["closed_reason"] == "adjusted"


async def test_a_plain_cancel_is_recorded_as_the_users_choice(tmp_path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )

    await rt.cancel(pid)

    assert (await store.load_program(pid))["run"]["closed_reason"] == "user"


async def test_new_template_starts_with_one_explore_step_and_is_listed(monkeypatch):
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/api/workflows/templates", json={"name": "我的积分流程"})
        listed = await client.get("/api/workflows/templates")

    assert created.status_code == 200, created.text
    plan = created.json()["plan"]
    assert [[n["kind"] for n in f["nodes"]] for f in plan["flows"]] == [["explore"]]
    assert any(t["key"] == created.json()["key"] for t in listed.json())


async def test_swap_route_answers_an_invalid_plan_with_structured_errors(monkeypatch, tmp_path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_two_flow_plan(), workspace=str(tmp_path)
    )
    # 一个用户模板，其步骤的写入范围与后端子工作流重叠：换入前端后整体校验不通过
    saved = await store.save_template(_backend_plan(), name="后端流程", description="")
    monkeypatch.setattr(workflows_router.service, "_runtime", rt)
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post(
            f"/api/workflows/{pid}/flows/frontend/template",
            json={"template": f"user:{saved['key']}", "flow": "backend"},
        )
        missing = await client.post(
            f"/api/workflows/{pid}/flows/frontend/template",
            json={"template": "no_such_template", "flow": "main"},
        )

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "plan_invalid"
    assert detail["errors"] and "path" in detail["errors"][0]
    assert missing.status_code == 404


async def test_a_swapped_flow_lists_its_steps_in_plan_order(tmp_path):
    # 旧的“评估”步骤在替换后保留原行（编号较小），新的“实现”步骤编号较大：
    # 快照仍须按计划顺序给出（探索 → 实现 → 评估），否则界面的步骤轨道会错序
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    source = _preset_flow("explore_build_review")

    await rt.replace_draft_flow(
        pid,
        "backend",
        source["nodes"],
        "探索 · 实现 · 评估 · 主流程",
        validate=validate_plan_for_run,
    )

    snap = await store.load_program(pid)
    stored = next(f for f in snap["flows"] if f["run"]["package_key"] == "backend")
    assert [n["key"] for n in stored["nodes"]] == [n["key"] for n in source["nodes"]]

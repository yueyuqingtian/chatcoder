"""工作流的主代理工具（§5.8）与 REST API（§5.9）：规划校验回传、模式门禁、会话归属、状态动作。"""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import workflows as workflows_router
from app.orchestration.workflow import service
from app.orchestration.workflow.tools import (
    WORKFLOW_TOOL_NAMES,
    append_workflow_tools,
    run_workflow_tool,
    workflow_tool_schemas,
)
from tests.test_workflow_runtime import _backend_plan, _session


def _program_id(text: str) -> int:
    match = re.search(r"program_id=(\d+)", text)
    assert match, text
    return int(match.group(1))


@pytest.fixture
def fresh_runtime(monkeypatch):
    """每个用例使用独立的运行时单例，避免跨用例的内存状态互相影响。"""
    monkeypatch.setattr(service, "_runtime", None)
    return service.get_runtime()


def test_schemas_cover_all_tool_names_and_respect_readonly_filter():
    names = {s["function"]["name"] for s in workflow_tool_schemas()}
    assert names == WORKFLOW_TOOL_NAMES
    readonly = {s["function"]["name"] for s in append_workflow_tools([], readonly_only=True)}
    assert readonly == {"plan_workflow", "workflow_status"}


async def test_plan_workflow_returns_diagnostics_for_bad_plan(fresh_runtime, tmp_path):
    bad = _backend_plan()
    bad["flows"][0]["nodes"][1].pop("write_scope")
    sid = await _session()
    text = await run_workflow_tool(
        "plan_workflow",
        {"title": "坏计划", "goal": "x", "flows": bad["flows"]},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "未通过校验" in text and "missing_write_scope" in text


async def test_plan_workflow_creates_draft_and_start_is_refused_in_plan_mode(
    fresh_runtime, tmp_path
):
    sid = await _session()
    plan = _backend_plan()
    text = await run_workflow_tool(
        "plan_workflow",
        {"title": plan["title"], "goal": plan["goal"], "flows": plan["flows"]},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "已生成工作流计划（program_id=" in text
    program_id = _program_id(text)

    refused = await run_workflow_tool(
        "start_workflow",
        {"program_id": program_id},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=True,
    )
    assert "只读/计划模式" in refused

    status = await run_workflow_tool(
        "workflow_status",
        {"program_id": program_id},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=True,
    )
    assert f"program_id={program_id}" in status and "draft" in status


async def test_plan_workflow_explains_flows_kept_from_the_preset(fresh_runtime, tmp_path):
    """基于骨架提交 6 个新子工作流：合并后 7 个，提示要指出多出的 main 来自骨架保留。"""
    sid = await _session()
    flows = [
        {"key": f"f{i}", "nodes": [{"key": "explore", "kind": "explore", "task": "x"}]}
        for i in range(6)
    ]
    text = await run_workflow_tool(
        "plan_workflow",
        {"title": "合并后超限", "goal": "g", "based_on": "fix_and_verify", "flows": flows},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "当前 7 个" in text
    assert "main" in text and "f5" in text
    assert "骨架子工作流" in text


async def test_plan_workflow_labels_the_generated_integration_flow(fresh_runtime, tmp_path):
    """5 个提交 + after：成功提示标注集成验收来自自动展开，模型能对上自己提交的数量。"""
    sid = await _session()
    flows = [
        {"key": f"f{i}", "nodes": [{"key": "explore", "kind": "explore", "task": "x"}]}
        for i in range(5)
    ]
    text = await run_workflow_tool(
        "plan_workflow",
        {
            "title": "含集成验收",
            "goal": "g",
            "flows": flows,
            "after": {"acceptance": ["整体通过"]},
        },
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "子工作流 6 个（含 after 自动展开的集成验收 1 个）" in text


async def test_plan_workflow_rejects_a_non_array_flows_argument(fresh_runtime, tmp_path):
    """flows 传成对象时应返回可读提示，而不是在合并骨架时抛异常。"""
    sid = await _session()
    text = await run_workflow_tool(
        "plan_workflow",
        {"title": "坏参数", "goal": "g", "based_on": "fix_and_verify", "flows": {"key": "x"}},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "flows 必须是数组" in text


async def test_programs_are_scoped_to_their_session(fresh_runtime, tmp_path):
    owner = await _session()
    other = await _session()
    plan = _backend_plan()
    text = await run_workflow_tool(
        "plan_workflow",
        {"title": plan["title"], "goal": plan["goal"], "flows": plan["flows"]},
        session_id=owner,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    program_id = _program_id(text)
    denied = await run_workflow_tool(
        "start_workflow",
        {"program_id": program_id},
        session_id=other,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "不属于本会话" in denied


@pytest.fixture
def api_client(fresh_runtime):
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")
    app.include_router(workflows_router.session_router, prefix="/api")
    return TestClient(app)


async def test_rest_snapshot_cancel_and_conflict_states(api_client, fresh_runtime, tmp_path):
    sid = await _session()
    pid = await fresh_runtime.create_program(
        session_id=sid, plan=_backend_plan(), workspace=str(tmp_path)
    )

    listing = api_client.get(f"/api/sessions/{sid}/workflows")
    assert listing.status_code == 200 and [p["id"] for p in listing.json()] == [pid]

    snap = api_client.get(f"/api/workflows/{pid}")
    assert snap.status_code == 200
    assert snap.json()["run"]["status"] == "draft"
    assert len(snap.json()["flows"][0]["nodes"]) == 3
    # 列表摘要的进度与快照的节点数一致（草稿阶段都未完成）
    total = sum(len(f["nodes"]) for f in snap.json()["flows"])
    assert listing.json()[0]["progress"] == {"done": 0, "total": total}

    assert api_client.get("/api/workflows/999999").status_code == 404
    # 草稿状态不能续跑：返回 409 与明确原因
    conflict = api_client.post(f"/api/workflows/{pid}/resume", json={"note": ""})
    assert conflict.status_code == 409

    cancelled = api_client.post(f"/api/workflows/{pid}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["run"]["status"] == "cancelled"

    events = api_client.get(f"/api/workflows/{pid}/events", params={"after_seq": 0})
    assert events.status_code == 200 and events.json()
    assert events.json()[0]["seq"] == 1

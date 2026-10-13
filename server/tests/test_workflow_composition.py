"""工作流组装（设计 §4.5 / §8）：预设骨架与 based_on、用户模板、仅预设组装方式、运行中修订。"""

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings as app_settings
from app.gateway.routers import workflows as workflows_router
from app.orchestration.workflow import presets, service, store
from app.orchestration.workflow.graph import parse_plan, validate_plan
from app.orchestration.workflow.tools import run_workflow_tool
from tests.test_workflow_runtime import ScriptedExecutor, _backend_plan, _runtime, _session


@pytest.fixture
def fresh_runtime(monkeypatch):
    monkeypatch.setattr(service, "_runtime", None)
    return service.get_runtime()


def _valid(plan: dict) -> None:
    spec, diags = parse_plan(plan)
    assert spec is not None, diags
    result = validate_plan(spec)
    assert result.ok, result.errors


# ── 预设骨架 ───────────────────────────────────────────────


@pytest.mark.parametrize("key", ["explore_build_review", "parallel_research", "fix_and_verify"])
def test_every_preset_is_a_valid_plan(key):
    _valid(presets.preset_plan(key))


async def test_based_on_merges_flows_by_key_and_keeps_the_rest(tmp_path, fresh_runtime):
    sid = await _session()
    override = {
        "key": "main",
        "title": "改写后的主流程",
        "nodes": [
            {"key": "explore", "kind": "explore", "task": "只探索后端"},
            {
                "key": "implement",
                "kind": "execute",
                "inputs": ["explore"],
                "task": "实现后端",
                "write_scope": ["server/app/points.py"],
                "acceptance": ["余额不足返回 409"],
            },
            {
                "key": "review",
                "kind": "review",
                "inputs": ["implement"],
                "rework_to": "implement",
                "task": "评估",
                "acceptance": ["测试通过"],
            },
        ],
    }
    text = await run_workflow_tool(
        "plan_workflow",
        {
            "title": "积分兑换",
            "goal": "实现兑换",
            "based_on": "explore_build_review",
            "flows": [override],
        },
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )

    assert "已生成工作流计划" in text
    listed = await store.list_programs(sid)
    snap = await store.load_program(listed[0]["id"])
    assert [f["run"]["package_key"] for f in snap["flows"]] == ["main"]
    assert snap["flows"][0]["run"]["title"] == "改写后的主流程"


async def test_unknown_based_on_is_reported_with_the_available_skeletons(tmp_path, fresh_runtime):
    sid = await _session()
    text = await run_workflow_tool(
        "plan_workflow",
        {"title": "x", "goal": "y", "based_on": "nope", "flows": []},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "未找到骨架" in text and "explore_build_review" in text


async def test_presets_only_mode_requires_based_on(tmp_path, fresh_runtime, monkeypatch):
    monkeypatch.setattr(app_settings, "workflow_composition_mode", "presets_only")
    sid = await _session()
    plan = _backend_plan()
    refused = await run_workflow_tool(
        "plan_workflow",
        {"title": plan["title"], "goal": plan["goal"], "flows": plan["flows"]},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    allowed = await run_workflow_tool(
        "plan_workflow",
        {"title": plan["title"], "goal": plan["goal"], "based_on": "fix_and_verify", "flows": []},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "仅允许预设骨架" in refused
    assert "已生成工作流计划" in allowed


# ── 用户模板 ───────────────────────────────────────────────


@pytest.fixture
def template_client(fresh_runtime):
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")
    return TestClient(app)


async def test_saved_template_round_trips_the_after_gate_without_duplicating_it(
    tmp_path, fresh_runtime
):
    plan = _backend_plan()
    plan["after"] = {"acceptance": ["端到端通过"], "commands": ["pytest -q"]}
    sid = await _session()
    pid = await fresh_runtime.create_program(session_id=sid, plan=plan, workspace=str(tmp_path))
    snap = await store.load_program(pid)

    template_plan = presets.to_template_plan(snap["run"]["graph"])

    assert [f["key"] for f in template_plan["flows"]] == ["backend"], "集成子工作流应还原为 after"
    assert template_plan["after"]["acceptance"] == ["端到端通过"]
    _valid(template_plan)


async def test_template_rest_lifecycle(template_client, tmp_path, fresh_runtime):
    sid = await _session()
    pid = await fresh_runtime.create_program(
        session_id=sid, plan=_backend_plan(), workspace=str(tmp_path)
    )

    saved = template_client.post(
        f"/api/workflows/{pid}/template", json={"name": "后端改造", "description": "常用"}
    )
    assert saved.status_code == 200
    key = saved.json()["key"]

    listing = template_client.get("/api/workflows/templates").json()
    sources = {item["key"]: item["source"] for item in listing}
    assert sources[key] == "user"
    assert any(item["source"] == "preset" for item in listing)

    copied = template_client.post("/api/workflows/templates/fix_and_verify/copy").json()
    assert copied["source"] == "user" and copied["name"].endswith("（副本）")

    patched = template_client.patch(f"/api/workflows/templates/{key}", json={"enabled": False})
    assert patched.status_code == 200 and patched.json()["enabled"] is False

    disabled = await run_workflow_tool(
        "plan_workflow",
        {"title": "x", "goal": "y", "based_on": f"user:{key}", "flows": []},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "未找到骨架" in disabled, "停用的模板不能被引用"

    assert template_client.delete(f"/api/workflows/templates/{key}").status_code == 200
    assert template_client.delete(f"/api/workflows/templates/{key}").status_code == 404


async def test_user_template_can_be_used_as_a_base(tmp_path, fresh_runtime):
    sid = await _session()
    pid = await fresh_runtime.create_program(
        session_id=sid, plan=_backend_plan(), workspace=str(tmp_path)
    )
    snap = await store.load_program(pid)
    saved = await store.save_template(presets.to_template_plan(snap["run"]["graph"]), name="复用")

    text = await run_workflow_tool(
        "plan_workflow",
        {"title": "新任务", "goal": "复用模板", "based_on": f"user:{saved['key']}", "flows": []},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )

    assert "已生成工作流计划" in text
    newest = (await store.list_programs(sid))[0]
    assert (await store.load_program(newest["id"]))["run"]["title"] == "新任务"


# ── 运行中修订 ─────────────────────────────────────────────


async def test_amend_adds_pending_steps_and_bumps_the_revision(tmp_path):
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    plan = copy.deepcopy(_backend_plan())
    plan["flows"][0]["nodes"].append({"key": "extra", "kind": "explore", "task": "补充调研"})

    rev = await rt.amend(pid, plan, reason="探索后发现还需补充调研")

    snap = await store.load_program(pid)
    assert rev == 2 and snap["run"]["plan_rev"] == 2
    assert "extra" in [n["key"] for n in snap["flows"][0]["nodes"]]


async def test_amend_cannot_change_or_remove_started_nodes(tmp_path):
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    snap = await store.load_program(pid)
    explore = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "explore")
    await store.update_node(explore["id"], status="done")

    changed = copy.deepcopy(_backend_plan())
    changed["flows"][0]["nodes"][0]["task"] = "换一个探索任务"
    with pytest.raises(ValueError, match="已开始执行，不能修改"):
        await rt.amend(pid, changed)

    removed = copy.deepcopy(_backend_plan())
    removed["flows"][0]["nodes"] = removed["flows"][0]["nodes"][1:]
    for node in removed["flows"][0]["nodes"]:
        node["inputs"] = [i for i in node.get("inputs", []) if i != "explore"]
    with pytest.raises(ValueError, match="已开始执行，不能删除"):
        await rt.amend(pid, removed)


async def test_amend_is_capped_at_three_revisions(tmp_path):
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    for n in range(3):
        plan = copy.deepcopy(_backend_plan())
        plan["goal"] = f"第 {n + 2} 版"
        await rt.amend(pid, plan)
    with pytest.raises(ValueError, match="修订次数已达上限"):
        await rt.amend(pid, _backend_plan())


async def test_amend_tool_reports_the_new_revision_and_refuses_in_plan_mode(
    tmp_path, fresh_runtime
):
    sid = await _session()
    pid = await fresh_runtime.create_program(
        session_id=sid, plan=_backend_plan(), workspace=str(tmp_path)
    )

    refused = await run_workflow_tool(
        "amend_workflow",
        {"program_id": pid, "reason": "x"},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=True,
    )
    done = await run_workflow_tool(
        "amend_workflow",
        {"program_id": pid, "reason": "补充说明", "goal": "新的目标"},
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )

    assert "只读/计划模式" in refused
    assert f"工作流 {pid} 计划已修订为第 2 版" in done
    assert (await store.load_program(pid))["run"]["goal"] == "新的目标"


# ── 隔离方式：独立工作树的子工作流与共享写入范围的重叠检查 ──


def _two_writers(isolation_a: str | None, isolation_b: str | None) -> dict:
    """两个并行子工作流都要写入同一文件（无依赖路径）。"""

    def flow(key: str, isolation: str | None) -> dict:
        spec = {
            "key": key,
            "title": key,
            "depends_on": [],
            "nodes": [
                {"key": "explore", "kind": "explore", "task": "看看"},
                {
                    "key": "impl",
                    "kind": "execute",
                    "task": "改",
                    "acceptance": ["完成"],
                    "write_scope": ["server/app/points.py"],
                    "inputs": ["explore"],
                },
            ],
        }
        if isolation is not None:
            spec["isolation"] = isolation
        return spec

    return {
        "title": "并行写入",
        "goal": "验证隔离",
        "flows": [flow("a", isolation_a), flow("b", isolation_b)],
    }


def test_shared_writers_of_the_same_file_are_reported_as_overlapping():
    spec, diags = parse_plan(_two_writers(None, "shared"))
    assert spec is not None, diags
    codes = [d.code for d in validate_plan(spec).errors]
    assert "write_scope_overlap" in codes


def test_a_worktree_flow_may_overlap_because_its_changes_are_merged_at_integration():
    spec, diags = parse_plan(_two_writers("worktree", None))
    assert spec is not None, diags
    result = validate_plan(spec)
    assert result.ok, result.errors


def test_integrate_is_a_reserved_node_key_in_worktree_flows():
    plan = _two_writers("worktree", None)
    plan["flows"][0]["nodes"][1]["key"] = "integrate"
    plan["flows"][0]["nodes"][1]["inputs"] = ["explore"]
    spec, diags = parse_plan(plan)
    assert spec is not None, diags
    codes = [d.code for d in validate_plan(spec).errors]
    assert "reserved_node_key" in codes


async def test_template_edit_is_validated_like_a_plan(template_client, tmp_path, fresh_runtime):
    sid = await _session()
    pid = await fresh_runtime.create_program(
        session_id=sid, plan=_backend_plan(), workspace=str(tmp_path)
    )
    key = template_client.post(
        f"/api/workflows/{pid}/template", json={"name": "待编辑", "description": ""}
    ).json()["key"]
    edited = copy.deepcopy(_backend_plan())
    edited["flows"][0]["nodes"][2]["acceptance"] = ["评估通过后才能继续"]

    ok = template_client.patch(f"/api/workflows/templates/{key}", json={"plan": edited})
    assert ok.status_code == 200
    assert ok.json()["plan"]["flows"][0]["nodes"][2]["acceptance"] == ["评估通过后才能继续"]

    broken = copy.deepcopy(edited)
    broken["flows"][0]["nodes"][1]["write_scope"] = []  # 实现节点必须声明写入范围
    refused = template_client.patch(f"/api/workflows/templates/{key}", json={"plan": broken})
    assert refused.status_code == 422
    codes = [item["code"] for item in refused.json()["detail"]["errors"]]
    assert codes, "校验错误应逐条返回"

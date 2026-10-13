"""工作区漂移检测（§6.6）：暂停时记录写入范围指纹，续跑前比对，外部改动需用户确认。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import workflows as workflows_router
from app.orchestration.workflow import service
from app.orchestration.workflow.drift import ABSENT, diff_fingerprints, snapshot_fingerprint
from app.orchestration.workflow.runtime import WorkflowDriftError
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _model_reachable,
    _program_status,
    _runtime,
    _session,
)


def test_snapshot_covers_globs_and_records_absent_literals(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("a", encoding="utf-8")
    (tmp_path / "src" / "b.ts").write_text("b", encoding="utf-8")

    fp = snapshot_fingerprint(str(tmp_path), ["src/*.ts", "docs/new.md"])

    assert set(fp) == {"src/a.ts", "src/b.ts", "docs/new.md"}
    assert fp["docs/new.md"] == ABSENT


def test_diff_reports_changed_added_and_removed_files(tmp_path):
    (tmp_path / "src").mkdir()
    keep = tmp_path / "src" / "keep.ts"
    gone = tmp_path / "src" / "gone.ts"
    keep.write_text("k", encoding="utf-8")
    gone.write_text("g", encoding="utf-8")
    patterns = ["src/*.ts", "src/new.ts"]
    before = snapshot_fingerprint(str(tmp_path), patterns)

    gone.unlink()
    keep.write_text("changed content", encoding="utf-8")
    (tmp_path / "src" / "new.ts").write_text("n", encoding="utf-8")
    after = snapshot_fingerprint(str(tmp_path), patterns)

    assert diff_fingerprints(before, after) == ["src/gone.ts", "src/keep.ts", "src/new.ts"]


def test_paths_escaping_the_workspace_are_ignored(tmp_path):
    assert snapshot_fingerprint(str(tmp_path), ["../outside.txt"]) == {}


async def test_resume_requires_confirmation_when_scope_changed_during_pause(tmp_path):
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_text("v1", encoding="utf-8")
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.pause(pid, reason="网络中断")

    # 暂停期间用户在工作流之外改动了写入范围内的文件
    target.write_text("v2 edited outside the workflow", encoding="utf-8")

    assert await rt.drift_of(pid) == ["server/app/points.py"]
    with pytest.raises(WorkflowDriftError) as exc:
        await rt.resume(pid)
    assert exc.value.files == ["server/app/points.py"]
    assert await _program_status(pid) == "paused", "未确认前不得恢复运行"

    await rt.resume(pid, accept_drift=True)
    assert await _program_status(pid) == "running"
    assert await rt.drift_of(pid) == [], "续跑后指纹已清空，不再重复提示"


async def test_resume_without_external_changes_needs_no_confirmation(tmp_path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.pause(pid, reason="用户已暂停")

    assert await rt.drift_of(pid) == []
    await rt.resume(pid)
    assert await _program_status(pid) == "running"


@pytest.fixture
def drift_client(monkeypatch):
    monkeypatch.setattr(service, "_runtime", None)
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")
    return TestClient(app)


async def test_rest_drift_preview_and_resume_guard(drift_client, tmp_path):
    target = tmp_path / "server" / "app" / "points.py"
    target.parent.mkdir(parents=True)
    target.write_text("v1", encoding="utf-8")
    rt = service.get_runtime()
    rt._probe = _model_reachable  # 测试环境没有模型：续跑前的可达性预检取“可达”
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.pause(pid, reason="用户已暂停")
    target.write_text("v2", encoding="utf-8")

    preview = drift_client.get(f"/api/workflows/{pid}/drift")
    assert preview.status_code == 200
    assert preview.json()["files"] == ["server/app/points.py"]
    assert preview.json()["head"] is None  # 不是 Git 仓库

    guarded = drift_client.post(f"/api/workflows/{pid}/resume", json={"note": ""})
    assert guarded.status_code == 409
    assert "暂停期间有改动" in guarded.json()["detail"]

    confirmed = drift_client.post(
        f"/api/workflows/{pid}/resume", json={"note": "", "accept_drift": True}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["run"]["status"] == "running"

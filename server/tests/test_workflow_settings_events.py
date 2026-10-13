"""工作流的设置读写（设置 → 工作流）与事件信封口径（程序内序号不得冒充会话级 seq）。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings as app_settings
from app.gateway.routers import settings as settings_router
from app.orchestration.workflow import service, store
from tests.test_workflow_runtime import _backend_plan, _session


@pytest.fixture
def fresh_runtime(monkeypatch):
    monkeypatch.setattr(service, "_runtime", None)
    return service.get_runtime()


async def test_event_envelope_carries_run_seq_not_session_seq(fresh_runtime, tmp_path):
    """信封顶层不带 seq（否则 ws 层不分配会话序号，客户端 lastSeq 会被程序序号推高）。"""
    sid = await _session()
    pid = await fresh_runtime.create_program(
        session_id=sid, plan=_backend_plan(), workspace=str(tmp_path)
    )

    emitted = await store.emit_event(
        pid, "workflow.log.appended", None, {"text": "hi", "level": "info"}
    )
    envelope = emitted["envelope"]
    snap = await store.load_program(pid)

    assert "seq" not in envelope
    assert emitted["session_id"] == sid
    assert envelope["payload"]["run_seq"] == snap["run"]["last_seq"]
    assert envelope["payload"]["run_id"] == pid


@pytest.fixture
def settings_client(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(settings_router, "_load_config", lambda: dict(saved))
    monkeypatch.setattr(settings_router, "_save_config", lambda data: saved.update(data))
    monkeypatch.setattr(app_settings, "workflow_max_concurrent_global", 3)
    monkeypatch.setattr(app_settings, "workflow_max_concurrent_per_session", 2)
    monkeypatch.setattr(app_settings, "workflow_max_parallel_nodes_per_flow", 2)
    monkeypatch.setattr(app_settings, "workflow_default_max_rework", 2)
    monkeypatch.setattr(app_settings, "workflow_token_budget_per_run", 0)
    monkeypatch.setattr(app_settings, "workflow_enabled", True)
    app = FastAPI()
    app.include_router(settings_router.router, prefix="/api")
    return TestClient(app), saved


def test_defaults_are_conservative(settings_client):
    client, _ = settings_client
    body = client.get("/api/settings/global").json()
    assert body["workflow_enabled"] is True
    assert body["workflow_max_concurrent_global"] == 3
    assert body["workflow_max_concurrent_per_session"] == 2
    assert body["workflow_max_parallel_nodes_per_flow"] == 2
    assert body["workflow_default_max_rework"] == 2
    assert body["workflow_token_budget_per_run"] == 0


def test_zero_means_unlimited_for_concurrency_and_values_are_clamped(settings_client):
    client, saved = settings_client
    body = client.put(
        "/api/settings/global",
        json={
            "workflow_max_concurrent_global": 0,
            "workflow_max_concurrent_per_session": 99,
            "workflow_max_parallel_nodes_per_flow": 0,
            "workflow_default_max_rework": 9,
            "workflow_token_budget_per_run": -5,
        },
    ).json()

    assert body["workflow_max_concurrent_global"] == 0
    assert body["workflow_max_concurrent_per_session"] == 16
    assert body["workflow_max_parallel_nodes_per_flow"] == 1
    assert body["workflow_default_max_rework"] == 3
    assert body["workflow_token_budget_per_run"] == 0
    assert app_settings.workflow_max_concurrent_global == 0
    assert saved["workflow_max_concurrent_per_session"] == 16


def test_disabling_workflows_is_persisted(settings_client):
    client, saved = settings_client
    body = client.put("/api/settings/global", json={"workflow_enabled": False}).json()
    assert body["workflow_enabled"] is False
    assert saved["workflow_enabled"] is False
    assert app_settings.workflow_enabled is False

"""单会话并发上限（plan-474-2105）回归测试。

覆盖三件事：
1. 配置默认值：子代理 / 子会话并发均为 4，废弃的每轮总量配置已彻底移除；
2. 设置接口的取值夹紧：超界值被夹到 1~30；
3. 运行时只读接口 /api/runtime/limits 可用（路径挂在 /api/runtime 下，不在 /api/subagents 下）。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings as app_settings
from app.gateway.routers import settings as settings_router
from app.gateway.routers import subagents as subagents_router
from app.orchestration import subagent as subagent_module
from app.services import subsession_service


def test_config_defaults():
    """默认值与遗留配置清理。"""
    assert app_settings.max_concurrent_subagents == 4
    assert app_settings.max_subsessions_per_parent == 4
    # 每轮派发总量限制本轮已废弃：只限并发、不限累计总量
    assert not hasattr(app_settings, "max_subagents_per_turn")
    # plan-20261022-subagent-reject-fast：并发满直接拒绝，排队等待配置已移除
    assert not hasattr(app_settings, "subagent_queue_wait_s")


def test_gate_rejection_explains_why_and_what_next():
    """并发满的拒绝文案：说明未创建、当前占用、可执行动作。

    该函数是纯同步函数——拒绝路径不再等待空位（等待期对主模型静默，
    最终仍会失败），用例同时锁住「不排队」这一语义。
    """
    from app.orchestration.agent_loop import _subagent_concurrency_rejection

    msg = _subagent_concurrency_rejection(2, 2, "#253 调研 A（已运行 12 秒）")
    assert msg.startswith("Error:")
    assert "2/2" in msg
    assert "本次未创建子代理" in msg
    assert "#253 调研 A" in msg
    assert "collect_results(wait=true)" in msg
    assert "设置 →「子代理」" in msg
    # 没有运行中摘要时也不留空行残句
    plain = _subagent_concurrency_rejection(1, 1)
    assert "当前在运行：" not in plain
    assert "1/1" in plain


@pytest.fixture
def settings_client(monkeypatch):
    """只挂设置路由，配置文件读写全部走内存字典。"""
    saved: dict = {}
    monkeypatch.setattr(settings_router, "_load_config", lambda: dict(saved))
    monkeypatch.setattr(settings_router, "_save_config", lambda data: saved.update(data))
    app = FastAPI()
    app.include_router(settings_router.router, prefix="/api")
    return TestClient(app), saved


def test_put_clamps_limits(settings_client):
    """PUT 传入 99 / 0 时被夹到 30 / 1。"""
    client, saved = settings_client
    originals = (
        app_settings.max_concurrent_subagents,
        app_settings.max_subsessions_per_parent,
    )
    try:
        r = client.put("/api/settings/global", json={
            "max_concurrent_subagents": 0,
            "max_subsessions_per_parent": 99,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["max_concurrent_subagents"] == 1
        assert body["max_subsessions_per_parent"] == 30
        assert saved["max_concurrent_subagents"] == 1
        assert saved["max_subsessions_per_parent"] == 30
    finally:
        # 恢复单例，避免污染同批次其他用例
        app_settings.max_concurrent_subagents, app_settings.max_subsessions_per_parent = originals


def test_runtime_limits_route_mounted_under_api_runtime():
    """/runtime/limits 不能落到 /api/subagents 前缀下。"""
    assert subagents_router.limits_router.prefix == "/runtime"
    paths = {r.path for r in subagents_router.limits_router.routes}
    assert "/runtime/limits" in paths


def test_runtime_limits_returns_snapshot(monkeypatch):
    """接口返回上限与各会话占用（数据库为空时 sessions 为空列表即可）。"""
    app = FastAPI()
    app.include_router(subagents_router.limits_router, prefix="/api")
    r = TestClient(app).get("/api/runtime/limits")
    assert r.status_code == 200
    body = r.json()
    assert body["max_concurrent_subagents"] == app_settings.max_concurrent_subagents
    assert body["max_subsessions_per_parent"] == app_settings.max_subsessions_per_parent
    assert body["sessions"] == []


def test_runtime_limits_aggregates_running(monkeypatch):
    """同一主会话下：子会话占用来自统计函数，子代理占用来自内存管理器。"""

    async def _fake_counts() -> dict[int, int]:
        return {12: 3}

    class _Manager:
        def pending_count(self) -> int:
            return 2

    monkeypatch.setattr(subsession_service, "list_running_counts_by_parent", _fake_counts)
    monkeypatch.setattr(
        subagent_module, "peek_subagent_manager", lambda sid: _Manager() if sid == 12 else None
    )

    app = FastAPI()
    app.include_router(subagents_router.limits_router, prefix="/api")
    body = TestClient(app).get("/api/runtime/limits").json()
    assert body["sessions"] == [
        {"session_id": 12, "subagents_running": 2, "subsessions_running": 3}
    ]


async def test_list_running_counts_by_parent_roundtrip():
    """统计函数：有运行中 turn 的子会话计数，归档子会话不进入结果。"""
    from app.core.enums import SideKind
    from app.persistence.database import run_write_locked
    from app.persistence.models.message import Session as SessionRow
    from app.persistence.models.project import Project as ProjectRow
    from app.persistence.models.turn import Turn

    def _seed(s):
        proj = ProjectRow(name="并发上限测试", path="C:/tmp/plan-474-2105")
        s.add(proj)
        s.flush()
        parent = SessionRow(title="主会话", project_id=proj.id)
        s.add(parent)
        s.flush()
        running = SessionRow(
            title="跑着的子会话", project_id=proj.id,
            side_parent_id=parent.id, side_kind=SideKind.SUB.value,
        )
        idle = SessionRow(
            title="空闲子会话", project_id=proj.id,
            side_parent_id=parent.id, side_kind=SideKind.SUB.value,
        )
        archived = SessionRow(
            title="归档子会话", project_id=proj.id, status="archived",
            side_parent_id=parent.id, side_kind=SideKind.SUB.value,
        )
        s.add_all([running, idle, archived])
        s.flush()
        s.add(Turn(session_id=running.id, status="running"))
        s.add(Turn(session_id=archived.id, status="running"))
        ids = {"parent": parent.id}
        s.commit()
        return ids

    ids = await run_write_locked(_seed, label="test.session.concurrency.limits")
    counts = await subsession_service.list_running_counts_by_parent()
    # 只有 running 子会话计入；归档的虽然有运行中 turn 也不再计入
    assert counts.get(ids["parent"]) == 1

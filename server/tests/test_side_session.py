"""侧边会话（子会话）后端回归测试（plan-20261017-side-session P4）。

覆盖：
  1) 开侧边会话：沿用主会话的项目、模型与执行/权限模式，写入 side_parent_id；
  2) 子会话不出现在普通会话列表中，但主会话的侧边列表能查到它；
  3) 侧边会话不能再开侧边会话；没有所属项目的主会话拒绝开启；
  4) 主会话不存在时返回 404。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import sessions as sessions_router
from app.persistence.database import run_write_locked
from app.persistence.models.message import Session as SessionRow
from app.persistence.models.project import Project as ProjectRow


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_router.router)
    return app


async def _project_and_session() -> tuple[int, int]:
    # plan-474-2105：工作区路径用唯一值——以前写死 "C:/tmp/side-session-test"，
    # 同一测试库连续跑多个用例时第二次插入就撞 projects.path 唯一约束（假失败）。
    import uuid

    unique_path = f"C:/tmp/side-session-test-{uuid.uuid4().hex[:8]}"

    def _op(s):
        proj = ProjectRow(name="侧边测试", path=unique_path)
        s.add(proj)
        s.flush()
        row = SessionRow(title="主会话", project_id=proj.id, permission_mode="plan", approval_mode="auto")
        s.add(row)
        s.flush()
        ids = (proj.id, row.id)
        s.commit()
        return ids

    return await run_write_locked(_op, label="test.side.session")


async def test_side_session_inherits_parent_and_is_hidden_from_list():
    project_id, parent_id = await _project_and_session()
    client = TestClient(_app())

    res = client.post(f"/sessions/{parent_id}/side-sessions")
    assert res.status_code == 200, res.text
    child = res.json()
    assert child["side_parent_id"] == parent_id
    assert child["project_id"] == project_id
    assert child["permission_mode"] == "plan"
    assert child["approval_mode"] == "auto"

    listed = client.get(f"/sessions?project_id={project_id}").json()
    ids = [s["id"] for s in listed]
    assert parent_id in ids
    assert child["id"] not in ids

    children = client.get(f"/sessions/{parent_id}/side-sessions").json()
    assert [c["id"] for c in children] == [child["id"]]


async def test_subsession_cannot_create_subsession():
    """plan-474-2105：子会话不得再创建任何同级 / 下级会话。

    旧行为（plan-156-619 S5）允许子会话派生同级，结果是一个任务可以无限套娃、
    并绕开单会话并发上限；需求明确改为直接拒绝。工具层已剥离 create_subsession，
    这里验证 REST 入口的防线。
    """
    _, parent_id = await _project_and_session()
    client = TestClient(_app())
    child_id = client.post(f"/sessions/{parent_id}/side-sessions").json()["id"]

    res = client.post(f"/sessions/{child_id}/side-sessions")
    assert res.status_code == 400, res.text
    assert "子会话不能再创建会话" in res.text

    # 主会话下的子会话仍然只有原来那一个
    children = client.get(f"/sessions/{parent_id}/side-sessions").json()
    assert [c["id"] for c in children] == [child_id]



async def test_side_session_requires_project():
    def _op(s):
        row = SessionRow(title="无项目会话")
        s.add(row)
        s.flush()
        sid = row.id
        s.commit()
        return sid

    orphan_id = await run_write_locked(_op, label="test.side.orphan")
    res = TestClient(_app()).post(f"/sessions/{orphan_id}/side-sessions")
    assert res.status_code == 400


async def test_permanent_delete_cascades_to_side_sessions():
    _, parent_id = await _project_and_session()
    client = TestClient(_app())
    child_id = client.post(f"/sessions/{parent_id}/side-sessions").json()["id"]

    res = client.delete(f"/sessions/{parent_id}?permanent=true")
    assert res.status_code == 200, res.text

    assert client.get(f"/sessions/{parent_id}").status_code == 404
    assert client.get(f"/sessions/{child_id}").status_code == 404


async def test_side_session_unknown_parent_is_404():
    res = TestClient(_app()).post("/sessions/999999/side-sessions")
    assert res.status_code == 404

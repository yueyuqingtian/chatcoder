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
    def _op(s):
        proj = ProjectRow(name="侧边测试", path="C:/tmp/side-session-test")
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


async def test_subsession_can_create_sibling_not_nested():
    """plan-156-619 S5：子会话可以再建子会话，但结果必须是**同级**（一层扁平）。

    旧实现直接 400 拒绝（`test_side_session_cannot_nest`），导致子会话无法派生同级；
    新语义：`side_parent_id` 一律指向根主会话，因此永远不会出现三级嵌套。
    """
    _, parent_id = await _project_and_session()
    client = TestClient(_app())
    child_id = client.post(f"/sessions/{parent_id}/side-sessions").json()["id"]

    res = client.post(f"/sessions/{child_id}/side-sessions")
    assert res.status_code == 200, res.text
    sibling = res.json()
    # 关键断言：新会话挂在**根主会话**下，而不是挂在 child 下
    assert sibling["side_parent_id"] == parent_id
    assert sibling["side_parent_id"] != child_id

    # 与第一个子会话同级：两者都从主会话的列表里能查到
    children = client.get(f"/sessions/{parent_id}/side-sessions").json()
    assert {c["id"] for c in children} == {child_id, sibling["id"]}



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

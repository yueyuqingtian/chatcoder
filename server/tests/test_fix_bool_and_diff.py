# -*- coding: utf-8 -*-
import pytest
from app.core.config import settings
from app.orchestration.approval import approval_manager
from app.persistence.database import async_session_factory
from app.persistence.models.message import Session
from app.persistence.models.rollback import RollbackWrite
from app.services import rollback_service, task_service


@pytest.mark.asyncio
async def test_approval_manager_ignores_bool_auto_approve():
    """回归：旧配置 auto_approve_tools 为布尔值时不再触发 'bool' object is not iterable。

    plan-75-332: approval.py 不再读该配置自行放行——是否放行由 approval_policy 在
    executor 侧判定。走 request() 即意味着需要询问用户，无人作答时超时返回 False。
    """
    settings.auto_approve_tools = True
    try:
        approved = await approval_manager.request(
            detail={"tool": "fs_write", "kind": "tool_call", "risk_level": "medium"}
        )
        assert approved is False  # 不再自行放行，等用户决定
    finally:
        settings.auto_approve_tools = False


@pytest.mark.asyncio
async def test_task_service_create_artifact():
    """验证 task_service.create_artifact 可正常创建产物（写引擎单写线程）。"""
    from app.persistence.models.task import Artifact
    async with async_session_factory() as db:
        session = Session(project_id=None, permission_mode="agent")
        db.add(session)
        await db.commit()  # 提交数据（create_task 经写引擎独立连接写入，需先提交）

        task_id = await task_service.create_task(db, session_id=session.id, title="测试产物任务")
        art_id = await task_service.create_artifact(
            db,
            task_id=task_id,
            type="code",
            title="测试代码块",
            storage_ref="inline://test/1",
            summary="测试 summary",
            files=["test.py"],
        )
        assert art_id is not None
        art = await db.get(Artifact, art_id)
        await db.refresh(art)  # 写引擎独立连接提交，重读最新
        assert art.type == "code"
        assert art.title == "测试代码块"
        await db.rollback()


@pytest.mark.asyncio
async def test_get_file_diff_path_normalization():
    """验证 get_file_diff 对正反斜杠的容错匹配。"""
    async with async_session_factory() as db:
        session = Session(project_id=None, permission_mode="agent")
        db.add(session)
        await db.flush()
        rw = RollbackWrite(
            session_id=session.id,
            turn_id=1,
            tool="fs_write",
            path="src\\components\\App.tsx",
            old_content="old line",
            new_content="new line",
            binary=False,
        )
        db.add(rw)
        await db.flush()

        # 前端以正斜杠路径查询
        diff = await rollback_service.get_file_diff(
            db, session_id=1, turn_id=1, workspace=".", path="src/components/App.tsx"
        )
        assert diff is not None
        assert diff["path"] == "src/components/App.tsx"

        await db.rollback()

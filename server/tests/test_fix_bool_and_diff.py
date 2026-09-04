# -*- coding: utf-8 -*-
import pytest
from app.core.config import settings
from app.orchestration.approval import approval_manager
from app.persistence.database import async_session_factory
from app.persistence.models.message import Session
from app.persistence.models.rollback import RollbackWrite
from app.services import rollback_service, task_service


@pytest.mark.asyncio
async def test_approval_manager_with_bool_auto_approve():
    """验证 settings.auto_approve_tools 为 True 时，不会抛出 'bool' object is not iterable。"""
    settings.auto_approve_tools = True
    approved = await approval_manager.request(
        detail={"tool": "fs_write", "kind": "tool_call", "risk_level": "high"}
    )
    assert approved is True

    # 设为 False
    settings.auto_approve_tools = False


@pytest.mark.asyncio
async def test_task_service_create_artifact():
    """验证 task_service.create_artifact 可正常创建产物。"""
    async with async_session_factory() as db:
        session = Session(project_id=None, permission_mode="default")
        db.add(session)
        await db.flush()

        task = await task_service.create_task(db, session_id=session.id, title="测试产物任务")
        art = await task_service.create_artifact(
            db,
            task_id=task.id,
            type="code",
            title="测试代码块",
            storage_ref="inline://test/1",
            summary="测试 summary",
            files=["test.py"],
        )
        assert art.id is not None
        assert art.type == "code"
        assert art.title == "测试代码块"
        await db.rollback()


@pytest.mark.asyncio
async def test_get_file_diff_path_normalization():
    """验证 get_file_diff 对正反斜杠的容错匹配。"""
    async with async_session_factory() as db:
        rw = RollbackWrite(
            session_id=1,
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

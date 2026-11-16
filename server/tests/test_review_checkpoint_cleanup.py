"""plan-20261116-review-fixes：引用未知时绝不执行文件删除。"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.gateway.routers import diagnostics
from app.persistence import database


@pytest.mark.parametrize("workspace", [None, "D:/workspace"])
async def test_reference_query_failure_never_calls_cleanup(monkeypatch, workspace):
    @asynccontextmanager
    async def factory():
        yield object()

    cleanup = AsyncMock()
    projects = AsyncMock()
    monkeypatch.setattr(database, "async_session_factory", factory)
    monkeypatch.setattr(diagnostics.checkpoint_gc, "_referenced_ckpts", AsyncMock(side_effect=OSError("数据库暂时不可读")))
    monkeypatch.setattr(diagnostics.checkpoint_gc, "cleanup", cleanup)
    monkeypatch.setattr(diagnostics.project_service, "list_projects", projects)
    with pytest.raises(HTTPException) as error:
        await diagnostics.cleanup_checkpoints(workspace=workspace, db=None)
    assert error.value.status_code == 503
    cleanup.assert_not_awaited()
    projects.assert_not_awaited()


async def test_successful_empty_reference_query_still_cleans_orphans(monkeypatch):
    db = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield db

    cleanup = AsyncMock(return_value={"deleted": 2, "kept": 0})
    monkeypatch.setattr(database, "async_session_factory", factory)
    monkeypatch.setattr(diagnostics.checkpoint_gc, "_referenced_ckpts", AsyncMock(return_value=set()))
    monkeypatch.setattr(diagnostics.checkpoint_gc, "cleanup", cleanup)
    result = await diagnostics.cleanup_checkpoints(workspace="D:/workspace", db=None)
    cleanup.assert_awaited_once_with(db, "D:/workspace", refs=set())
    db.commit.assert_awaited_once()
    assert result["results"][0]["deleted"] == 2

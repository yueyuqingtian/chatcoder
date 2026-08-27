"""会话首条消息自动命名测试。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.session_service import auto_title_session


@pytest.mark.asyncio
async def test_auto_title_session_normalizes_and_truncates_first_message():
    db = SimpleNamespace(flush=AsyncMock())
    session = SimpleNamespace(title=None)

    title = await auto_title_session(db, session, "  abcdefghijklmnop\nqrstuvwxyz1234567890  ")

    assert title == "abcdefghijklmnop qrstuvwxyz123"
    assert session.title == title
    db.flush.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_auto_title_session_does_not_replace_existing_title():
    db = SimpleNamespace(flush=AsyncMock())
    session = SimpleNamespace(title="用户自定义标题")

    title = await auto_title_session(db, session, "新的首条消息")

    assert title is None
    assert session.title == "用户自定义标题"
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_title_session_ignores_blank_message():
    db = SimpleNamespace(flush=AsyncMock())
    session = SimpleNamespace(title=None)

    title = await auto_title_session(db, session, " \n ")

    assert title is None
    assert session.title is None
    db.flush.assert_not_awaited()

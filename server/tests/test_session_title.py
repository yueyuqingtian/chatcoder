"""会话首条消息自动命名测试。

plan-206-975：auto_title_session 经写引擎单写线程（patch_session），
不再直接修改传入对象/flush；测试以 mock patch_session 断言。
"""
from types import SimpleNamespace

import pytest

from app.services import session_service
from app.services.session_service import auto_title_session


@pytest.mark.asyncio
async def test_auto_title_session_normalizes_and_truncates_first_message(monkeypatch):
    calls: list[tuple] = []

    async def fake_patch(sid, **kw):
        calls.append((sid, kw))

    monkeypatch.setattr(session_service, "patch_session", fake_patch)
    session = SimpleNamespace(id=7, title=None)

    title = await auto_title_session(None, session, "  abcdefghijklmnop\nqrstuvwxyz1234567890  ")

    assert title == "abcdefghijklmnop qrstuvwxyz123"
    assert calls == [(7, {"title": "abcdefghijklmnop qrstuvwxyz123"})]
    assert session.title is None  # 对象不再被直接修改（写引擎负责落库）


@pytest.mark.asyncio
async def test_auto_title_session_does_not_replace_existing_title(monkeypatch):
    calls: list[tuple] = []

    async def fake_patch(sid, **kw):
        calls.append((sid, kw))

    monkeypatch.setattr(session_service, "patch_session", fake_patch)
    session = SimpleNamespace(id=7, title="用户自定义标题")

    title = await auto_title_session(None, session, "新的首条消息")

    assert title is None
    assert calls == []  # 已有标题时不写库


@pytest.mark.asyncio
async def test_auto_title_session_ignores_blank_message(monkeypatch):
    calls: list[tuple] = []

    async def fake_patch(sid, **kw):
        calls.append((sid, kw))

    monkeypatch.setattr(session_service, "patch_session", fake_patch)
    session = SimpleNamespace(id=7, title=None)

    title = await auto_title_session(None, session, " \n ")

    assert title is None
    assert calls == []

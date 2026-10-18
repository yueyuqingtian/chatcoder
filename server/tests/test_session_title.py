"""会话首条消息自动命名测试。

plan-206-975：auto_title_session 经写引擎单写线程（patch_session），
不再直接修改传入对象/flush；测试以 mock patch_session 断言。

plan-156-619 S8：标题来源改为三态（auto / named / manual）——
auto = 前端建会话的占位标题（允许归纳覆盖）；named = 正式标题；manual = 用户手改。
本文件同时覆盖「占位标题才会被归纳」这条新口径。
"""
from types import SimpleNamespace

import pytest

from app.services import session_service
from app.services.session_service import auto_title_session


def _session(**kw):
    """构造最小 Session 替身：默认 title_source=auto（与新建会话一致）。"""
    base = {"id": 7, "title": None, "title_source": "auto", "model_id": None}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_auto_title_session_normalizes_and_truncates_first_message(monkeypatch):
    calls: list[tuple] = []

    async def fake_patch(sid, **kw):
        calls.append((sid, kw))

    monkeypatch.setattr(session_service, "patch_session", fake_patch)
    # db=None → summarize_title 直接回落，这里验证的是「回落路径 + 写回 named」
    session = _session()

    title = await auto_title_session(None, session, "  abcdefghijklmnop\nqrstuvwxyz1234567890  ")

    assert title == "abcdefghijklmnop qrstuvwxyz123"
    # 写回后来源变 named：同会话不再重复调模型归纳
    assert calls == [(7, {"title": "abcdefghijklmnop qrstuvwxyz123", "title_source": "named"})]
    assert session.title is None  # 对象不再被直接修改（写引擎负责落库）


@pytest.mark.asyncio
async def test_auto_title_session_does_not_replace_existing_title(monkeypatch):
    calls: list[tuple] = []

    async def fake_patch(sid, **kw):
        calls.append((sid, kw))

    monkeypatch.setattr(session_service, "patch_session", fake_patch)
    # 存量会话：title_source 迁移后为 auto，但标题是旧逻辑生成的正式标题
    session = _session(title="用户自定义标题")

    title = await auto_title_session(None, session, "新的首条消息")

    assert title is None
    assert calls == []  # 标题与首条消息截断结果不同 → 视为正式标题，不覆盖


@pytest.mark.asyncio
async def test_auto_title_session_ignores_blank_message(monkeypatch):
    calls: list[tuple] = []

    async def fake_patch(sid, **kw):
        calls.append((sid, kw))

    monkeypatch.setattr(session_service, "patch_session", fake_patch)
    session = _session()

    title = await auto_title_session(None, session, " \n ")

    assert title is None
    assert calls == []


@pytest.mark.asyncio
async def test_auto_title_session_skips_manual_title(monkeypatch):
    calls: list[tuple] = []

    async def fake_patch(sid, **kw):
        calls.append((sid, kw))

    monkeypatch.setattr(session_service, "patch_session", fake_patch)
    # 用户手改过：即使标题恰好等于首条消息截断，也绝不覆盖
    session = _session(title="abc", title_source="manual")

    title = await auto_title_session(None, session, "abcdefghij")

    assert title is None
    assert calls == []


@pytest.mark.asyncio
async def test_auto_title_session_skips_named_title(monkeypatch):
    calls: list[tuple] = []

    async def fake_patch(sid, **kw):
        calls.append((sid, kw))

    monkeypatch.setattr(session_service, "patch_session", fake_patch)
    # 子会话 / fork 的正式标题：不再归纳
    session = _session(title="重构侧栏几何", title_source="named")

    title = await auto_title_session(None, session, "重构侧栏几何相关的所有细节")

    assert title is None
    assert calls == []

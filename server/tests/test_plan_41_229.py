# -*- coding: utf-8 -*-
"""plan-41-229: 模型切换 divider 的落库与广播。

覆盖用户反馈「切换模型后必须再发一条消息才提示切换了模型」：
- `session_service.create_system_message` 在落库 SYSTEM 消息后广播 `message.created`，
  前端切换模型当场即收到提示，不再等到下一次消息刷新。
- `PATCH /sessions/{id}`：仅在模型真正变化时写入 divider；重复选择同一模型不产生新提示。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway import ws as ws_mod
from app.gateway.routers.sessions import update_session
from app.gateway.schemas import SessionUpdate
from app.persistence.database import Base
from app.persistence.models import Turn  # noqa: F401 注册全部模型到 Base.metadata
from app.persistence.models.message import Message
from app.persistence.models.message import Session as SessionModel
from app.services import session_service


@pytest.fixture
async def db_env(tmp_path):
    """临时文件库 + 同源写引擎（create_message 经 run_write_locked 独立连接写入）。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/plan41229.db"
    eng = create_async_engine(db_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as session:
        yield session
    await eng.dispose()
    _we.configure(None)


@pytest.fixture
def captured(monkeypatch):
    """捕获 WS 广播事件（message_service 经全局 manager 广播）。

    注意：把函数挂到类属性上后经实例调用会绑定 self，故签名必须与真实方法一致，
    否则 TypeError 会被 create_message 的广播兜底 except 吞掉，表现为「一次都没广播」。
    """
    events: list[tuple[int, dict]] = []

    async def fake_broadcast(self, session_id, event):  # noqa: ANN001
        events.append((session_id, event))

    monkeypatch.setattr(ws_mod.ConnectionManager, "broadcast", fake_broadcast)
    return events


async def _mk_session(db, *, session_id: int = 1, model_id: int | None = None) -> None:
    db.add(SessionModel(id=session_id, model_id=model_id, permission_mode="default"))
    await db.commit()


async def _system_messages(db) -> list[Message]:
    rows = await db.execute(select(Message).where(Message.msg_type == "system"))
    return list(rows.scalars().all())


async def test_create_system_message_broadcasts_message_created(db_env, captured):
    """系统消息落库即广播——这是「切模型立即出提示」的关键。"""
    db = db_env
    await _mk_session(db)

    mid = await session_service.create_system_message(
        db, session_id=1,
        content={"text": "模型已切换为 测试模型", "divider": "model_changed"},
    )

    assert mid > 0
    row = await db.get(Message, mid)
    assert row is not None
    assert row.sender_type == "system"
    assert row.msg_type == "system"
    assert row.turn_id is None  # divider 不挂 turn，由前端按时间线位置归位
    assert row.content["divider"] == "model_changed"

    assert len(captured) == 1
    sid, ev = captured[0]
    assert sid == 1
    assert ev["event"] == "message.created"
    payload = ev["payload"]["msg"]
    assert payload["id"] == mid
    assert payload["msg_type"] == "system"
    assert payload["content"]["text"] == "模型已切换为 测试模型"


async def test_update_session_writes_divider_only_on_real_change(db_env, captured):
    """模型真变化 → 写一条 divider 并广播；重复 PATCH 同一模型 → 不再出提示。"""
    db = db_env
    await _mk_session(db, model_id=1)

    await update_session(1, SessionUpdate(model_id=2), db)
    rows = await _system_messages(db)
    assert len(rows) == 1
    assert "已切换" in rows[0].content["text"]
    assert len(captured) == 1
    assert captured[0][1]["event"] == "message.created"

    # 重复 PATCH 同一模型：不应再刷出「模型已切换」（旧实现曾重复写，现已加判重）
    await update_session(1, SessionUpdate(model_id=2), db)
    assert len(await _system_messages(db)) == 1
    assert len(captured) == 1


async def test_update_session_divider_not_written_for_other_fields(db_env, captured):
    """PATCH 其它字段（如置顶/标题）不产生模型切换提示。"""
    db = db_env
    await _mk_session(db, model_id=1)

    await update_session(1, SessionUpdate(pinned=True), db)
    assert await _system_messages(db) == []
    assert captured == []

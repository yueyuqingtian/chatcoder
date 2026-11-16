# -*- coding: utf-8 -*-
"""plan-41-229: 模型切换 divider 的落库与广播。

覆盖用户反馈「切换模型后必须再发一条消息才提示切换了模型」：
- `session_service.create_system_message` 在落库 SYSTEM 消息后广播 `message.created`，
  前端切换模型当场即收到提示，不再等到下一次消息刷新。
- plan-20261017-ux-overhaul：分割线改为发送时写入（与上一条用户消息所用模型比较，带供应商名），
  `PATCH /sessions/{id}` 切换当下不再写入。
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


async def _add_user_message(db, *, session_id: int = 1, deleted: bool = False) -> None:
    db.add(Message(
        session_id=session_id, sender_type="user", msg_type="text",
        content={"text": "你好"}, deleted=deleted,
    ))
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


async def _mk_model(db, *, model_id: int, name: str, provider_name: str = "") -> None:
    from app.persistence.models.model_reg import Model, Provider
    prov_id = None
    if provider_name:
        db.add(Provider(id=model_id, name=provider_name))
        prov_id = model_id
    db.add(Model(id=model_id, name=name, provider_id=prov_id, source_type="custom"))
    await db.commit()


async def _add_user_message_with_model(db, *, model_id: int, session_id: int = 1) -> None:
    db.add(Message(
        session_id=session_id, sender_type="user", msg_type="text",
        content={"text": "你好", "model_id": model_id},
    ))
    await db.commit()


async def test_patch_model_writes_no_divider(db_env, captured):
    """plan-20261017-ux-overhaul：切换当下不写分割线（只切不发不留痕迹）。"""
    db = db_env
    await _mk_session(db, model_id=1)
    await _add_user_message(db)

    await update_session(1, SessionUpdate(model_id=2), db)
    assert await _system_messages(db) == []
    assert captured == []
    refreshed = await session_service.get_session(db, 1)
    assert refreshed is not None and refreshed.model_id == 2


async def test_divider_written_on_send_with_provider(db_env, captured):
    """发送时与上一条用户消息所用模型不同 → 写「已切换至 供应商 / 模型」；相同则不写。"""
    from app.gateway.routers.turns import _write_model_divider_if_changed

    db = db_env
    await _mk_session(db, model_id=2)
    await _mk_model(db, model_id=1, name="glm-4.6", provider_name="智谱")
    await _mk_model(db, model_id=2, name="glm-4.6", provider_name="硅基流动")
    await _add_user_message_with_model(db, model_id=1)

    await _write_model_divider_if_changed(db, 1, 2)
    rows = await _system_messages(db)
    assert len(rows) == 1
    assert rows[0].content["divider"] == "model_changed"
    assert rows[0].content["text"] == "已切换至 硅基流动 / glm-4.6"

    # 上一条用户消息已用同一模型：不再写
    await _add_user_message_with_model(db, model_id=2)
    await _write_model_divider_if_changed(db, 1, 2)
    assert len(await _system_messages(db)) == 1


async def test_divider_skipped_without_history_model(db_env, captured):
    """空会话或历史消息没有记录模型（旧数据）时不写分割线。"""
    from app.gateway.routers.turns import _write_model_divider_if_changed

    db = db_env
    await _mk_session(db, model_id=2)
    await _mk_model(db, model_id=2, name="m2")
    await _write_model_divider_if_changed(db, 1, 2)
    await _add_user_message(db)  # 旧数据：content 无 model_id
    await _write_model_divider_if_changed(db, 1, 2)
    assert await _system_messages(db) == []


async def test_update_session_no_divider_in_empty_session(db_env, captured):
    """plan-20261016-ux（问题 8）：会话里没有对话（新建未发送 / 发送后撤销）时切模型不写提示。"""
    db = db_env
    await _mk_session(db, model_id=1)

    await update_session(1, SessionUpdate(model_id=2), db)
    assert await _system_messages(db) == []

    # 发送后又撤销：用户消息被软删，仍视为空会话
    await _add_user_message(db, deleted=True)
    await update_session(1, SessionUpdate(model_id=3), db)
    assert await _system_messages(db) == []
    assert captured == []

    # 模型确实已经切换（只是不写提示）
    refreshed = await session_service.get_session(db, 1)
    assert refreshed is not None and refreshed.model_id == 3


async def test_update_session_divider_not_written_for_other_fields(db_env, captured):
    """PATCH 其它字段（如置顶/标题）不产生模型切换提示。"""
    db = db_env
    await _mk_session(db, model_id=1)

    await update_session(1, SessionUpdate(pinned=True), db)
    assert await _system_messages(db) == []
    assert captured == []


async def test_divider_attached_to_new_turn_after_user_message(db_env, captured):
    """plan-20261115：切换提示必须落在「新用户消息之后、AI 执行之前」。

    用户反馈：发送前切了模型，提示却出现在**上一轮**的复制按钮组前面。
    根因有二——① 旧实现先写提示再写用户消息，提示拿到更小的 id；
    ② 提示不带 turn_id，前端把无 turn 的系统消息并入上一个 turn。
    这里同时锁住「归属本轮 turn」与「id 大于本批用户消息」两条。
    """
    from app.gateway.routers.turns import _write_model_divider_if_changed

    db = db_env
    await _mk_session(db, model_id=2)
    await _mk_model(db, model_id=1, name="glm-4.6", provider_name="智谱")
    await _mk_model(db, model_id=2, name="glm-4.6", provider_name="硅基流动")
    await _add_user_message_with_model(db, model_id=1)  # 上一轮用户消息

    # 新用户消息先入库（与 create_turn 中的顺序一致）
    new_user = Message(session_id=1, sender_type="user", msg_type="text",
                      content={"text": "下一条", "model_id": 2}, turn_id=99)
    db.add(new_user)
    await db.commit()

    await _write_model_divider_if_changed(
        db, 1, 2, turn_id=99, before_message_id=new_user.id,
    )

    rows = await _system_messages(db)
    assert len(rows) == 1
    divider = rows[0]
    # 归属新的一轮：前端按 turn 分组时才会渲染在 AI 执行之前
    assert divider.turn_id == 99
    # 时间序在新用户消息之后
    assert divider.id > new_user.id


async def test_divider_compares_against_message_before_new_user_message(db_env, captured):
    """回看基准必须排除刚插入的新用户消息，否则永远判成「没换模型」。"""
    from app.gateway.routers.turns import _write_model_divider_if_changed

    db = db_env
    await _mk_session(db, model_id=1)
    await _mk_model(db, model_id=1, name="m1")
    await _mk_model(db, model_id=2, name="m2")
    await _add_user_message_with_model(db, model_id=1)

    new_user = Message(session_id=1, sender_type="user", msg_type="text",
                      content={"text": "换了模型的新消息", "model_id": 2}, turn_id=7)
    db.add(new_user)
    await db.commit()

    # 不传 before_message_id 时，最新用户消息就是刚插入的这条 → 不写提示
    await _write_model_divider_if_changed(db, 1, 2)
    assert await _system_messages(db) == []

    # 传了之后回看得到上一条（model_id=1），正确写出提示
    await _write_model_divider_if_changed(db, 1, 2, turn_id=7,
                                          before_message_id=new_user.id)
    rows = await _system_messages(db)
    assert len(rows) == 1 and rows[0].turn_id == 7

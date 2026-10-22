"""v21: 主会话摘要系统落库 + 主路径阈值前不丢信息测试。

覆盖修复：
- Session.shared_context 是持久化列（此前仅内存属性，摘要从不落库）
- maybe_summarize_main_session 在未摘要历史超阈值时写入 shared_context，
  使窗口截断前旧上下文先被摘要保留。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.enums import MsgType, SenderType
from app.orchestration.context_memory import maybe_summarize_main_session
from app.persistence.database import Base
from app.persistence.models.message import Message, Session


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/ctx_summary.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)  # 写引擎（单写线程）与测试库同源
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()
    _we.configure(None)


async def _mk_session(db: AsyncSession) -> Session:
    s = Session(project_id=1, title="test")
    db.add(s)
    await db.flush()
    await db.refresh(s)
    return s


async def _mk_text_messages(db: AsyncSession, session_id: int, n: int, body: str) -> list[Message]:
    msgs = []
    for i in range(n):
        m = Message(
            session_id=session_id, turn_id=1, thread_id=None,
            sender_type=SenderType.USER.value, sender_id=None,
            msg_type=MsgType.TEXT.value,
            content={"text": f"{body} #{i}"},
        )
        db.add(m)
        msgs.append(m)
    await db.flush()
    return msgs


class TestSharedContextPersisted:
    def test_session_has_shared_context_column(self):
        """Session 模型已映射 shared_context 列（可落库）。"""
        assert "shared_context" in Session.__table__.columns

    async def test_shared_context_round_trips(self, db: AsyncSession):
        s = await _mk_session(db)
        s.shared_context = {"summary": "abc", "summarized_ids": [1, 2, 3]}
        await db.commit()
        # 重新加载（模拟跨 turn/重启），摘要必须还在
        reloaded = (await db.execute(
            select(Session).where(Session.id == s.id)
        )).scalars().first()
        assert reloaded.shared_context == {"summary": "abc", "summarized_ids": [1, 2, 3]}


class TestSummarizeChannelDisabled:
    """plan-527-2218：渐进摘要通道已停用，历史裁剪统一由压缩负责。

    停用原因：v16 起重建不再注入 ctx["summary"]，该通道一旦触发只会把消息写进
    summarized_ids、让它们从上下文消失且没有替代物。以下用例锁定「不再写入」，
    防止后续有人误以为仍是活跃通道而去调阈值。
    """

    async def test_over_threshold_does_not_write(self, db: AsyncSession):
        s = await _mk_session(db)
        # 20×6000 字符约 3 万 token，用 30K 窗口明确超过 0.85 阈值——
        # 停用前这里会写入摘要与 summarized_ids，停用后必须两者都不写。
        await _mk_text_messages(db, s.id, 20, "x" * 6000)
        await db.commit()  # 提交数据（maybe_summarize 经写引擎独立连接写入，避免快照隔离）

        await maybe_summarize_main_session(db, s, context_window=30_000)

        await db.refresh(s)  # 写引擎独立连接提交，重读最新
        ctx = s.shared_context or {}
        assert not ctx.get("summary"), "通道停用后不应写入摘要文本"
        assert not (ctx.get("summarized_ids") or []), "通道停用后不应标记摘要 id"

    async def test_below_threshold_skips(self, db: AsyncSession):
        s = await _mk_session(db)
        await _mk_text_messages(db, s.id, 3, "short")

        await maybe_summarize_main_session(db, s, context_window=80_000)

        ctx = s.shared_context or {}
        assert not ctx.get("summary"), "未超阈值不应触发摘要"

    async def test_restored_compaction_messages_not_touched(self, db: AsyncSession):
        """已还原压缩块的消息同样不会被标记——停用后整条通道都不再改写摘要状态，
        「还原后又被摘要吞掉」的旧坑不可能复现。"""
        s = await _mk_session(db)
        msgs = await _mk_text_messages(db, s.id, 14, "x" * 4000)
        restored_ids = [m.id for m in msgs[:7]]

        ctx = dict(s.shared_context or {})
        ctx["compactions"] = [{
            "compaction_id": "cp-restored-1", "index": 1,
            "summary_message_id": 0, "shadowed_ids": restored_ids,
            "shadowed_tokens": 999, "saved_tokens": 900,
            "trigger": "context-overflow", "restored": True,
        }]
        s.shared_context = ctx
        await db.commit()  # 提交数据（maybe_summarize 经写引擎独立连接写入，避免快照隔离）

        await maybe_summarize_main_session(db, s, context_window=5_000)

        await db.refresh(s)  # 写引擎独立连接提交，重读最新
        after = s.shared_context or {}
        summarized_ids = set(after.get("summarized_ids") or [])
        assert not summarized_ids.intersection(restored_ids), "已还原消息不应被摘要吞掉"
        assert not summarized_ids, "通道停用后不应新增摘要标记"

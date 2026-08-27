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
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


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


class TestSummarizeWritesContext:
    async def test_over_threshold_summarizes_and_marks_ids(self, db: AsyncSession):
        s = await _mk_session(db)
        # 阈值 = max(6000, 0.35×80000) = 28000；制造 20×6000 字符 = 120KB ≈ 3 万 token → 超阈值
        msgs = await _mk_text_messages(db, s.id, 20, "x" * 6000)

        await maybe_summarize_main_session(db, s, context_window=80_000)

        ctx = s.shared_context or {}
        assert ctx.get("summary"), "摘要应写入 shared_context"
        summarized_ids = set(ctx.get("summarized_ids") or [])
        assert summarized_ids, "被摘要的消息 id 应被标记"
        assert len(summarized_ids) >= 3

    async def test_below_threshold_skips(self, db: AsyncSession):
        s = await _mk_session(db)
        await _mk_text_messages(db, s.id, 3, "short")

        await maybe_summarize_main_session(db, s, context_window=80_000)

        ctx = s.shared_context or {}
        assert not ctx.get("summary"), "未超阈值不应触发摘要"

    async def test_restored_compaction_messages_not_summarized(self, db: AsyncSession):
        """v33: 已还原压缩块的消息不进入渐进摘要——还原后立即被摘要吞掉是
        "还原后 AI 仍看不到原文"的根因，必须排除。"""
        s = await _mk_session(db)
        # 14 条 × 6000 字符 ≈ 2.1 万 token；阈值(80k×0.35)=28k，
        # 排除前 7 条已还原消息后剩余 7 条 ≈ 1.05 万 token × ... 需确保剩余部分也超阈值
        msgs = await _mk_text_messages(db, s.id, 14, "x" * 8000)
        restored_ids = [m.id for m in msgs[:7]]

        ctx = dict(s.shared_context or {})
        ctx["compactions"] = [{
            "compaction_id": "cp-restored-1", "index": 1,
            "summary_message_id": 0, "shadowed_ids": restored_ids,
            "shadowed_tokens": 999, "saved_tokens": 900,
            "trigger": "context-overflow", "restored": True,
        }]
        s.shared_context = ctx
        await db.flush()

        # 候选 = 后 7 条 ≈ 7×6000 字节 = 42KB ≈ 10500 token < 28k 阈值？——
        # 直接用更小窗口保证候选超阈值
        await maybe_summarize_main_session(db, s, context_window=20_000)

        after = s.shared_context or {}
        summarized_ids = set(after.get("summarized_ids") or [])
        # 已还原的消息不得被摘要标记
        assert not summarized_ids.intersection(restored_ids), "已还原消息不应被渐进摘要吞掉"
        # 未还原的消息（后半段）正常摘要
        assert summarized_ids, "未还原消息应正常触发渐进摘要"

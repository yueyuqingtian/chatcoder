"""v30.1: 压缩服务测试——压缩块索引、原文还原、AI 工具。

覆盖：
- list_compaction_index：从 shared_context.compactions 生成索引（含摘要预览）
- get_compacted_messages：按 compaction_id 取被压缩消息原文
- restore_compaction：还原后 compacted_ids 移除、compactions 移除、SUMMARY 标记 restored
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.enums import MsgType, SenderType
from app.persistence.database import Base
from app.persistence.models.message import Message, Session
from app.services import compression_service


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


async def _mk_msgs(db: AsyncSession, session_id: int, n: int) -> list[Message]:
    msgs = []
    for i in range(n):
        m = Message(
            session_id=session_id, turn_id=1, thread_id=None,
            sender_type=SenderType.AGENT.value, sender_id=None,
            msg_type=MsgType.TEXT.value,
            content={"text": f"compacted message #{i}"},
        )
        db.add(m)
        msgs.append(m)
    await db.flush()
    return msgs


async def _mk_summary(db: AsyncSession, session_id: int, shadowed_ids: list[int],
                      index: int = 1) -> Message:
    m = Message(
        session_id=session_id, turn_id=1, thread_id=None,
        sender_type=SenderType.SYSTEM.value, sender_id=None,
        msg_type=MsgType.SUMMARY.value,
        content={
            "text": "preamble\n\n<compacted-summary>\n## Current Work\n- test\n</compacted-summary>",
            "compaction_id": f"cp-test-{index}",
            "index": index,
            "checkpoint": True,
            "shadowed_ids": shadowed_ids,
            "shadowed_tokens": 100,
            "saved_tokens": 80,
            "trigger": "pressure",
        },
    )
    db.add(m)
    await db.flush()
    return m


async def _install_compaction(db: AsyncSession, session: Session, summary: Message,
                              shadowed_ids: list[int], index: int = 1) -> None:
    ctx = dict(session.shared_context or {})
    ctx["compacted_ids"] = sorted(set(ctx.get("compacted_ids") or []) | set(shadowed_ids))
    comps = list(ctx.get("compactions") or [])
    comps.append({
        "compaction_id": summary.content["compaction_id"],
        "index": index,
        "summary_message_id": summary.id,
        "shadowed_ids": shadowed_ids,
        "shadowed_tokens": 100,
        "saved_tokens": 80,
        "trigger": "pressure",
        "created_at": "2026-08-26T00:00:00",
    })
    ctx["compactions"] = comps
    session.shared_context = ctx
    await db.flush()


class TestCompactionIndex:
    async def test_list_empty(self, db: AsyncSession):
        s = await _mk_session(db)
        assert await compression_service.list_compaction_index(db, s.id) == []

    async def test_list_with_preview(self, db: AsyncSession):
        s = await _mk_session(db)
        msgs = await _mk_msgs(db, s.id, 3)
        summary = await _mk_summary(db, s.id, [m.id for m in msgs])
        await _install_compaction(db, s, summary, [m.id for m in msgs])
        await db.commit()

        entries = await compression_service.list_compaction_index(db, s.id)
        assert len(entries) == 1
        e = entries[0]
        assert e["index"] == 1
        assert e["compaction_id"] == "cp-test-1"
        assert e["shadowed_ids"] == [m.id for m in msgs]
        assert e["saved_tokens"] == 80
        # 摘要预览剥掉帧标签
        assert e["summary_preview"].startswith("## Current Work")


class TestCompactedMessages:
    async def test_get_original(self, db: AsyncSession):
        s = await _mk_session(db)
        msgs = await _mk_msgs(db, s.id, 3)
        summary = await _mk_summary(db, s.id, [m.id for m in msgs])
        await _install_compaction(db, s, summary, [m.id for m in msgs])
        await db.commit()

        got = await compression_service.get_compacted_messages(db, s.id, "cp-test-1")
        assert [m.id for m in got] == [m.id for m in msgs]
        assert "compacted message" in got[0].content["text"]

    async def test_unknown_compaction_raises(self, db: AsyncSession):
        s = await _mk_session(db)
        with pytest.raises(KeyError):
            await compression_service.get_compacted_messages(db, s.id, "cp-nope")


class TestRestore:
    async def test_restore_reverts_context(self, db: AsyncSession):
        s = await _mk_session(db)
        msgs = await _mk_msgs(db, s.id, 3)
        summary = await _mk_summary(db, s.id, [m.id for m in msgs])
        await _install_compaction(db, s, summary, [m.id for m in msgs])
        await db.commit()

        count = await compression_service.restore_compaction(db, s.id, "cp-test-1")
        assert count == 3

        await db.refresh(s)
        ctx = s.shared_context or {}
        # compacted_ids 清空、injected 移除
        assert ctx.get("compacted_ids") == []
        assert ctx.get("injected_compactions") == []
        # v33: compactions 记录保留并标记 restored（原文接口/索引不失效，序号不重复）
        comps = ctx.get("compactions") or []
        assert len(comps) == 1
        assert comps[0]["compaction_id"] == "cp-test-1"
        assert comps[0].get("restored") is True
        # SUMMARY 消息标记 restored
        res = await db.execute(select(Message).where(Message.id == summary.id))
        m = res.scalar_one()
        assert m.content.get("restored") is True
        # 还原后原文仍可按压缩块 id 查询
        got = await compression_service.get_compacted_messages(db, s.id, "cp-test-1")
        assert len(got) == 3

    async def test_restore_unknown_raises(self, db: AsyncSession):
        s = await _mk_session(db)
        with pytest.raises(KeyError):
            await compression_service.restore_compaction(db, s.id, "cp-nope")


class TestAiTools:
    def test_message_to_text(self):
        """工具文本化：tool_call/tool_result/text 各类消息可读。"""
        from app.orchestration.tools.compaction_view import _message_to_text

        m = Message(
            session_id=1, turn_id=1, thread_id=None,
            sender_type=SenderType.AGENT.value, sender_id=None,
            msg_type=MsgType.TOOL_CALL.value,
            content={"tool": "fs_read", "args": {"path": "a.py"}},
        )
        t = _message_to_text(m)
        assert "fs_read" in t and "a.py" in t

        m2 = Message(
            session_id=1, turn_id=1, thread_id=None,
            sender_type=SenderType.AGENT.value, sender_id=None,
            msg_type=MsgType.TOOL_RESULT.value,
            content={"tool": "fs_read", "output": "content"},
        )
        assert "content" in _message_to_text(m2)

    async def test_index_tool_schema(self):
        """compaction_index 工具 schema 合法。"""
        from app.orchestration.tools.compaction_view import CompactionIndexTool, CompactionViewTool

        idx = CompactionIndexTool()
        view = CompactionViewTool()
        assert idx.function_schema()["function"]["name"] == "compaction_index"
        assert view.function_schema()["function"]["name"] == "compaction_view"
        props = view.function_schema()["function"]["parameters"]["properties"]
        assert "index" in props and "compaction_id" in props

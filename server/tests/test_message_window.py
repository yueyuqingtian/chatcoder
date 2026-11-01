"""plan-20261101-performance：分页、历史修订、工具跨页和索引回归。"""
import pytest
from sqlalchemy import select, update, text
from app.persistence.database import async_session_factory
from app.persistence.models.message import Message, Session
from app.persistence.migrations import ensure_message_revision
from app.services.message_window import page, search


async def seed(count=250):
    async with async_session_factory() as db:
        session = Session(title="分页测试")
        db.add(session)
        await db.flush()
        sid = session.id
        db.add_all([Message(session_id=sid, turn_id=i // 10 + 1, sender_type="agent", msg_type="text",
                            content={"text": f"消息 {i:04d}"}) for i in range(count)])
        await db.commit()
        await ensure_message_revision(db)
        return sid


@pytest.mark.asyncio
async def test_tail_before_after_and_around():
    sid = await seed()
    async with async_session_factory() as db:
        tail = await page(db, sid)
        assert len(tail["items"]) == 100 and tail["has_before"] and not tail["has_after"]
        assert tail["items"][0]["content"]["text"] == "消息 0150"
        older = await page(db, sid, before_id=tail["before_id"])
        assert older["after_id"] < tail["before_id"]
        newer = await page(db, sid, after_id=older["after_id"])
        assert [m["id"] for m in newer["items"]] == [m["id"] for m in tail["items"]]
        target = tail["before_id"] + 10
        around = await page(db, sid, around_id=target)
        assert target in [m["id"] for m in around["items"]]
        with pytest.raises(ValueError):
            await page(db, sid, before_id=1, after_id=2)


@pytest.mark.asyncio
async def test_thread_scope_deleted_and_same_length_revision():
    sid = await seed(4)
    async with async_session_factory() as db:
        db.add(Message(session_id=sid, thread_id=7, sender_type="agent", msg_type="text", content={"text": "线程"}))
        await db.commit()
        assert len((await page(db, sid))["items"]) == 4
        assert len((await page(db, sid, thread_id=7))["items"]) == 1
        before = await page(db, sid)
        mid = before["items"][0]["id"]
        await db.execute(update(Message).where(Message.id == mid).values(content={"text": "改写 0000"}))
        await db.commit()
        assert (await page(db, sid))["revision"] > before["revision"]
        await db.execute(update(Message).where(Message.id == mid).values(deleted=True))
        await db.commit()
        assert mid not in [m["id"] for m in (await page(db, sid))["items"]]


@pytest.mark.asyncio
async def test_cross_page_tool_context_and_search():
    sid = await seed(1)
    async with async_session_factory() as db:
        call = Message(session_id=sid, turn_id=1, sender_type="agent", msg_type="tool_call",
                       content={"call_key": "call-a", "tool": "fs_read", "args": {"path": "a.py"}})
        db.add(call)
        await db.flush()
        cid = call.id
        db.add(Message(session_id=sid, turn_id=1, sender_type="agent", msg_type="tool_result",
                       content={"call_key": "call-a", "output": "完整输出 100%"}))
        await db.commit()
        tail = await page(db, sid, limit=1)
        assert tail["context"][0]["id"] == cid
        assert tail["before_id"] == tail["items"][0]["id"]
        found = await search(db, sid, "100%")
        assert len(found["items"]) == 1 and "100%" in found["items"][0]["snippet"]


@pytest.mark.asyncio
async def test_window_query_uses_composite_index():
    sid = await seed(1)
    async with async_session_factory() as db:
        rows = (await db.execute(text("EXPLAIN QUERY PLAN SELECT id FROM messages WHERE session_id=:sid AND thread_id IS NULL AND deleted=0 AND id<100 ORDER BY id DESC LIMIT 100"), {"sid": sid})).all()
        assert any("idx_messages_window" in str(row) for row in rows)


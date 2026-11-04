"""plan-20261103-fork：临时数据库中的分支快照回归。"""
import pytest
from sqlalchemy import select
from app.persistence.database import async_session_factory, run_write_locked
from app.persistence.models.message import Session, Message
from app.persistence.models.turn import Turn
from app.persistence.models.agent import Agent
from app.persistence.models.tool_call import ToolCall
from app.services.session_service import fork_session, delete_session_permanent


def seed(s, status="running", broken=False):
    src = Session(title="源", worktree_path="D:/worktree", permission_mode="plan", approval_mode="full")
    s.add(src); s.flush()
    t = Turn(session_id=src.id, status="completed", summary="已完成", completed_at="2026-11-03 01:00:00")
    s.add(t); s.flush()
    a = Agent(kind="sub", name="探索", session_id=src.id, turn_id=t.id, status="done")
    s.add(a); s.flush()
    def msg(kind, content, sender="agent", thread=None):
        m = Message(session_id=src.id, turn_id=t.id, thread_id=thread, sender_type=sender, sender_id=a.id if thread else None, msg_type=kind, content=content)
        s.add(m); s.flush(); return m
    u = msg("text", {"text":"问题"}, "user"); t.user_message_id=u.id
    msg("thinking", {"text":"思考一"})
    msg("tool_call", {"tool":"fs_read","call_key":"a","args":{}})
    msg("tool_call", {"tool":"fs_read","call_key":"b","args":{}})
    msg("tool_result", {"tool":"fs_read","call_key":"b","output":"二"})
    if not broken: msg("tool_result", {"tool":"fs_read","call_key":"a","output":"一"})
    msg("text", {"text":"子代理思考", "thinking":True}, thread=a.id)
    msg("text", {"text":"完成"})
    t2 = Turn(session_id=src.id, status=status)
    s.add(t2); s.flush()
    s.add(Message(session_id=src.id, turn_id=t2.id, sender_type="user", msg_type="text", content={"text":"未完成问题"}))
    s.commit(); return src.id


@pytest.mark.asyncio
async def test_fork_preserves_structure_and_stops_before_running():
    sid = await run_write_locked(seed)
    async with async_session_factory() as db:
        fid = await fork_session(db, sid, request_id="same")
        assert await fork_session(db, sid, request_id="same") == fid
        child = await db.get(Session, fid)
        assert child.worktree_path == "D:/worktree" and child.approval_mode == "full"
        assert child.fork_info["copied_turns"] == 1 and child.fork_info["excluded_incomplete"]
        turns = list((await db.scalars(select(Turn).where(Turn.session_id == fid))).all())
        assert len(turns) == 1 and turns[0].inherited
        rows = list((await db.scalars(select(Message).where(Message.session_id == fid).order_by(Message.id))).all())
        assert turns[0].user_message_id == rows[0].id
        assert any(m.thread_id is not None and m.thread_id < 0 for m in rows)
        assert all(m.content.get("text") != "未完成问题" for m in rows)
        calls = [m.content["call_key"] for m in rows if m.msg_type == "tool_call"]
        results = [m.content["call_key"] for m in rows if m.msg_type == "tool_result"]
        assert len(set(calls)) == 2 and set(calls) == set(results) and "a" not in calls
        assert not (await db.scalars(select(ToolCall).where(ToolCall.session_id == fid))).all()
        assert not (await db.scalars(select(Agent).where(Agent.session_id == fid))).all()
        assert child.fork_snapshot["agents"][0]["inherited"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "failed", "cancelled", "interrupted", "awaiting_confirmation"])
async def test_excludes_noncompleted_turns(status):
    sid = await run_write_locked(lambda s: seed(s, status))
    async with async_session_factory() as db:
        child = await db.get(Session, await fork_session(db, sid))
        assert child.fork_info["copied_turns"] == 1


@pytest.mark.asyncio
async def test_broken_tool_round_becomes_empty_fork():
    sid = await run_write_locked(lambda s: seed(s, broken=True))
    async with async_session_factory() as db:
        child = await db.get(Session, await fork_session(db, sid))
        assert child.fork_info["copied_turns"] == 0
        assert "空白分支" in child.fork_info["warnings"][-1]


@pytest.mark.asyncio
async def test_refork_survives_source_deletion():
    sid = await run_write_locked(seed)
    async with async_session_factory() as db:
        fid = await fork_session(db, sid)
        await delete_session_permanent(db, sid)
        second = await db.get(Session, await fork_session(db, fid))
        assert second.fork_info["copied_turns"] == 1
        assert second.fork_snapshot["agents"][0]["message_count"] == 1

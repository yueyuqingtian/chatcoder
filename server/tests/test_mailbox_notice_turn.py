"""邮箱通知的轮次归属回归（plan-20261026-notice-turn）。

缺陷原貌：撤销会话的第一条消息后，消息流仍残留「【子代理进度上报…】」「【子代理已完成…】」。
根因是子代理三类通知（提问 / 进度上报 / 完成行）落库时 `turn_id` 恒为 None，而撤回只软删
`turn_id >= 目标轮` 的消息，于是这些行永远匹配不到软删条件。

本文件锁定四个口径：
1. 通知落库必须带 turn_id（撤回时才能一起软删）；
2. 归属轮次已撤回的迟到通知不再落库 / 不再投递；
3. 撤回兜底补删覆盖全部邮箱通知（不只是后台命令），且不误删用户 / 代理正文；
4. 队列里带 turn_id 的待投递通知在撤回时一并丢弃。
"""
import asyncio
import types

import pytest
from sqlalchemy import select

from app.orchestration import engine, mailbox
from app.persistence.database import async_session_factory
from app.persistence.models.message import Message, Session
from app.persistence.models.turn import Turn


@pytest.fixture(autouse=True)
def _clean():
    mailbox.reset_for_tests()
    yield
    mailbox.reset_for_tests()


async def _make_session() -> int:
    async with async_session_factory() as db:
        row = Session(title="notice-turn-test")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return int(row.id)


async def _make_turn(session_id: int, status: str) -> int:
    async with async_session_factory() as db:
        row = Turn(session_id=session_id, status=status)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return int(row.id)


async def _messages(session_id: int) -> list[Message]:
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(Message).where(Message.session_id == session_id).order_by(Message.id)
        )).scalars().all()
        return list(rows)


@pytest.fixture(autouse=True)
def _quiet_delivery(monkeypatch):
    """投递层静音：本文件只验证落库与清理，不让会话被真正唤醒。"""
    monkeypatch.setattr(engine, "running_turn_id", lambda sid: None)

    async def _no_wakeup(session_id, *, prompt="", reason=""):
        return None

    monkeypatch.setattr(engine, "wakeup_session", _no_wakeup)


@pytest.mark.asyncio
async def test_subagent_notices_persist_with_turn_id():
    """子代理进度上报 / 完成行落库必须带归属轮次，撤回才能按 turn_id 匹配。"""
    sid = await _make_session()

    await mailbox.publish(
        mailbox.MailKind.SUBAGENT_REPORT, session_id=sid, text="【子代理进度上报 subagent#3】过半",
        source_session_id=sid, turn_id=7,
    )
    await mailbox.publish(
        mailbox.MailKind.SUBAGENT_COMPLETION, session_id=sid, text="【子代理已完成 subagent#3「T」】",
        source_session_id=sid, turn_id=7,
    )

    rows = await _messages(sid)
    assert [int(r.turn_id or 0) for r in rows] == [7, 7]
    assert all((r.content or {}).get("mailbox") for r in rows)
    # 两类通知的回放标记不能混：进度上报进模型上下文，完成行纯展示
    assert rows[0].content.get("subsession_report") is True
    assert rows[1].content.get("display_only") is True


@pytest.mark.asyncio
async def test_publish_drops_notice_of_rolled_back_turn():
    """撤回后才到达的通知（子代理取消收尾等）不得再落库——否则会留下删不掉的孤儿行。"""
    sid = await _make_session()
    tid = await _make_turn(sid, "rolled_back")

    res = await mailbox.publish(
        mailbox.MailKind.SUBAGENT_COMPLETION, session_id=sid, text="【子代理已取消 subagent#3】",
        source_session_id=sid, turn_id=tid,
    )

    assert res.get("delivered") == "ignored"
    assert res.get("reason") == "turn_rolled_back"
    assert await _messages(sid) == []
    assert mailbox.pending_count(sid) == 0


@pytest.mark.asyncio
async def test_mark_notices_deleted_covers_all_mailbox_notices():
    """兜底补删覆盖提问 / 进度上报 / 完成行 / 后台命令，且不碰代理正文。"""
    sid = await _make_session()
    tid = await _make_turn(sid, "completed")
    for kind, text in (
        (mailbox.MailKind.SUBAGENT_REPORT, "【子代理进度上报 subagent#3】过半"),
        (mailbox.MailKind.SUBAGENT_COMPLETION, "【子代理已完成 subagent#3「T」】"),
        (mailbox.MailKind.BG_COMMAND_DONE, "[后台命令完成] npm test"),
    ):
        await mailbox.publish(kind, session_id=sid, text=text, source_session_id=sid, turn_id=tid)

    from app.core.enums import MsgType, SenderType
    from app.services import message_service

    async with async_session_factory() as db:
        await message_service.create_message(
            db, session_id=sid, turn_id=tid,
            sender_type=SenderType.AGENT.value, msg_type=MsgType.TEXT.value,
            content={"text": "这是主代理正文，不该被撤回清理误删"},
        )

    removed = await mailbox.mark_notices_deleted(sid, tid)
    assert removed == 3

    rows = await _messages(sid)
    notices = [r for r in rows if isinstance((r.content or {}).get("mailbox"), dict)]
    bodies = [r for r in rows if not isinstance((r.content or {}).get("mailbox"), dict)]
    assert notices and all(r.deleted for r in notices)
    assert bodies and not bodies[0].deleted


def test_drop_from_turn_clears_subagent_notices_in_queue():
    """队列里的子代理通知同样要按轮次清掉，否则下一轮 take_pending 会把它带回消息流。"""
    mailbox._push(mailbox.MailItem(
        kind=mailbox.MailKind.SUBAGENT_REPORT.value, session_id=71, text="report", turn_id=9))
    mailbox._push(mailbox.MailItem(
        kind=mailbox.MailKind.SUBAGENT_QUESTION.value, session_id=71, text="ask", turn_id=9))
    # 不同 kind，避免命中 _push 的同来源合并窗口（合并会改写 turn_id 归属）
    mailbox._push(mailbox.MailItem(
        kind=mailbox.MailKind.BG_COMMAND_DONE.value, session_id=71, text="unrelated", turn_id=None))

    assert mailbox.drop_from_turn(71, 9) == 2
    rest = mailbox.take_pending(71)
    assert [it["content"] for it in rest] == ["unrelated"]


@pytest.mark.asyncio
async def test_on_agent_report_forwards_turn_id(monkeypatch):
    """进度上报通道必须把主代理轮次透传给邮箱，不能只传会话。"""
    calls: list[dict] = []

    async def _fake_publish(kind, **kwargs):
        calls.append({"kind": kind, **kwargs})
        return {"delivered": "queued"}

    monkeypatch.setattr(mailbox, "publish", _fake_publish)

    engine._on_agent_report(None, 5, 42, 3, "过半", "progress")
    await asyncio.sleep(0)

    assert calls and calls[0]["kind"] == mailbox.MailKind.SUBAGENT_REPORT
    assert calls[0]["turn_id"] == 42


@pytest.mark.asyncio
async def test_on_subagent_finished_forwards_handle_turn_id(monkeypatch):
    """完成行取子代理 handle 的归属轮次（子代理在 turn 结束后跑完也归原轮）。"""
    calls: list[dict] = []

    async def _fake_publish(kind, **kwargs):
        calls.append({"kind": kind, **kwargs})
        return {"delivered": "display_only"}

    async def _noop_async(*args, **kwargs):
        return None

    monkeypatch.setattr(mailbox, "publish", _fake_publish)
    monkeypatch.setattr(engine, "broadcast", _noop_async)
    monkeypatch.setattr(engine, "broadcast_session_completed", _noop_async)
    monkeypatch.setattr(engine, "_session_turn_busy", lambda sid: False)

    handle = types.SimpleNamespace(
        agent_id=3, turn_id=9, task_title="T", status="done",
        findings="结论", summary="", error="", files_touched=[], risks=[],
    )
    manager = types.SimpleNamespace(
        pending_count=lambda: 0, has_unclaimed_notes=lambda: False,
    )

    engine._on_subagent_finished(manager, 5, handle)
    for _ in range(3):
        await asyncio.sleep(0)

    assert calls and calls[0]["kind"] == mailbox.MailKind.SUBAGENT_COMPLETION
    assert calls[0]["turn_id"] == 9

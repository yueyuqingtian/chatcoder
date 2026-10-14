"""工作流唤醒状态事件 workflow.wakeup（plan-20261014-actions-wait）。

工作流进入终态后，服务端要稍等再唤醒主代理送达通知。这段空档里既没有运行中的程序、也没有运行中的 turn，
前端的各处「执行中」指示若只看这两项就会回到空闲，唤醒轮启动后又恢复。本组用例验证服务端把空档接上的契约：

- 入队即发 pending=true；唤醒轮创建后、**启动之前**发 pending=false + turn_id（前端据此立即接管运行态）；
- 放弃（无通知 / 创建失败 / 被取消）一律发 pending=false，前端的待唤醒标记一定收尾；
- 会话忙时等待空闲；同一时段的多条通知合并为一次唤醒；
- 「停止」全量停止取消待送达的唤醒并丢弃通知，创建中的唤醒不被打断；
- 事件名登记（schemas / 全局通道白名单 / 共享事件表 / 前端处理）。
"""
import asyncio
import contextlib
import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import select

from app.gateway import ws as ws_module
from app.gateway.schemas import WS_EVENT_PAYLOAD_MODELS
from app.orchestration import engine
from app.orchestration.workflow import service as workflow_service
from app.persistence.database import async_session_factory
from app.persistence.models.message import Message
from app.services import message_service
from tests.test_workflow_runtime import _session

_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def order(monkeypatch):
    """按先后记录 workflow.wakeup 事件与 start_turn 调用；唤醒延迟置 0 以便快速走完。"""
    seen: list[tuple] = []

    async def fake_broadcast(session_id, event):
        if event.get("event") == "workflow.wakeup":
            payload = event["payload"]
            seen.append(("wakeup", session_id, payload["pending"], payload.get("turn_id")))

    async def fake_start_turn(db, *, turn_id, **kwargs):
        seen.append(("start_turn", turn_id))

    monkeypatch.setattr(engine, "broadcast", fake_broadcast)
    monkeypatch.setattr(engine, "start_turn", fake_start_turn)
    monkeypatch.setattr(engine, "WORKFLOW_WAKEUP_DELAY_SEC", 0.0)
    monkeypatch.setattr(engine, "WORKFLOW_WAKEUP_POLL_SEC", 0.01)
    return seen


async def _wakeup_messages(sid: int) -> list[Message]:
    async with async_session_factory() as db:
        rows = await db.execute(select(Message).where(Message.session_id == sid).order_by(Message.id))
        return list(rows.scalars().all())


def _forget(sid: int) -> None:
    engine._workflow_notes.pop(sid, None)
    engine._workflow_wakeups.pop(sid, None)
    engine._workflow_wakeup_creating.discard(sid)


async def test_pending_then_handover_before_the_turn_starts(order):
    sid = await _session()
    await engine.notify_workflow(sid, "工作流已完成")
    await asyncio.wait_for(engine._workflow_wakeups[sid], 5)

    msgs = await _wakeup_messages(sid)
    assert len(msgs) == 1
    assert msgs[0].content.get("workflow_wakeup") is True
    turn_id = msgs[0].turn_id
    assert turn_id is not None  # 唤醒消息已回填 turn_id

    # 事件先后：pending=true → 接管（带 turn_id）→ 之后才启动该轮
    assert order[0] == ("wakeup", sid, True, None)
    assert order[1] == ("wakeup", sid, False, turn_id)
    await asyncio.gather(engine._turn_tasks[turn_id], return_exceptions=True)
    assert order[2] == ("start_turn", turn_id)
    assert len(order) == 3

    assert sid not in engine._workflow_wakeups
    assert sid not in engine._workflow_wakeup_creating
    engine._turn_tasks.pop(turn_id, None)


async def test_notices_in_one_window_share_a_single_wakeup(order):
    sid = await _session()
    await engine.notify_workflow(sid, "第一条")
    await engine.notify_workflow(sid, "第二条")  # 已有唤醒在途：并入同一次，不再重复宣告
    await asyncio.wait_for(engine._workflow_wakeups[sid], 5)

    msgs = await _wakeup_messages(sid)
    assert len(msgs) == 1
    assert msgs[0].content["text"] == "第一条\n\n第二条"
    pendings = [e for e in order if e[0] == "wakeup" and e[2] is True]
    handovers = [e for e in order if e[0] == "wakeup" and e[2] is False]
    assert len(pendings) == 1 and len(handovers) == 1

    turn_id = msgs[0].turn_id
    await asyncio.gather(engine._turn_tasks[turn_id], return_exceptions=True)
    engine._turn_tasks.pop(turn_id, None)


async def test_waits_while_the_session_is_busy(order):
    sid = await _session()
    engine._session_turn_enter(sid)
    try:
        await engine.notify_workflow(sid, "工作流已完成")
        task = engine._workflow_wakeups[sid]
        await asyncio.sleep(0.1)
        # 会话仍在运行：只宣告了待唤醒，没有建消息、没有接管
        assert order == [("wakeup", sid, True, None)]
        assert not task.done()
        assert await _wakeup_messages(sid) == []
    finally:
        engine._session_turn_leave(sid)

    await asyncio.wait_for(task, 5)
    msgs = await _wakeup_messages(sid)
    assert len(msgs) == 1
    assert order[1] == ("wakeup", sid, False, msgs[0].turn_id)
    await asyncio.gather(engine._turn_tasks[msgs[0].turn_id], return_exceptions=True)
    engine._turn_tasks.pop(msgs[0].turn_id, None)


async def test_abandon_when_notes_are_gone(order, monkeypatch):
    sid = await _session()
    monkeypatch.setattr(engine, "WORKFLOW_WAKEUP_DELAY_SEC", 0.05)
    await engine.notify_workflow(sid, "工作流已完成")
    task = engine._workflow_wakeups[sid]
    await asyncio.sleep(0)
    engine._workflow_notes.pop(sid, None)  # 通知被别处取走
    await asyncio.wait_for(task, 5)

    assert order == [("wakeup", sid, True, None), ("wakeup", sid, False, None)]
    assert await _wakeup_messages(sid) == []
    assert sid not in engine._workflow_wakeups


async def test_abandon_when_creation_fails_and_notes_are_requeued(order, monkeypatch):
    sid = await _session()

    async def boom(*args, **kwargs):
        raise RuntimeError("模拟：建消息失败")

    monkeypatch.setattr(message_service, "create_message", boom)
    await engine.notify_workflow(sid, "工作流已完成")
    await asyncio.wait_for(engine._workflow_wakeups[sid], 5)

    assert order == [("wakeup", sid, True, None), ("wakeup", sid, False, None)]
    assert engine._workflow_notes[sid] == ["工作流已完成"]  # 通知退回队列，下一次通知到来时一并送达
    assert sid not in engine._workflow_wakeup_creating
    _forget(sid)


async def test_cancel_wakeup_drops_notes_and_announces_abandon(order, monkeypatch):
    sid = await _session()
    monkeypatch.setattr(engine, "WORKFLOW_WAKEUP_DELAY_SEC", 60.0)
    await engine.notify_workflow(sid, "工作流已完成")
    await asyncio.sleep(0)  # 让任务走到 sleep

    assert await engine.cancel_workflow_wakeup(sid) is True
    assert order == [("wakeup", sid, True, None), ("wakeup", sid, False, None)]
    assert sid not in engine._workflow_notes
    assert sid not in engine._workflow_wakeups
    assert await engine.cancel_workflow_wakeup(sid) is False  # 没有在途唤醒：无事可做


async def test_cancel_wakeup_refused_while_the_turn_is_being_created():
    sid = 424201
    pending = asyncio.ensure_future(asyncio.sleep(60))
    engine._workflow_wakeups[sid] = pending
    engine._workflow_notes[sid] = ["x"]
    engine._workflow_wakeup_creating.add(sid)
    try:
        assert await engine.cancel_workflow_wakeup(sid) is False  # 消息与轮次之间不能打断
        assert not pending.cancelled() and not pending.done()
        assert engine._workflow_notes[sid] == ["x"]
    finally:
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        _forget(sid)


async def test_stop_session_execution_cancels_the_pending_wakeup(order, monkeypatch):
    sid = await _session()
    monkeypatch.setattr(engine, "WORKFLOW_WAKEUP_DELAY_SEC", 60.0)

    async def no_workflows(session_id: int) -> int:
        return 0

    monkeypatch.setattr(workflow_service, "on_turn_stopped", no_workflows)

    # 没有待送达的唤醒：照常返回，计数为 0
    idle = await engine.stop_session_execution(sid)
    assert idle["wakeup_cancelled"] == 0

    await engine.notify_workflow(sid, "工作流已完成")
    await asyncio.sleep(0)
    stopped = await engine.stop_session_execution(sid)
    assert stopped["wakeup_cancelled"] == 1
    assert order == [("wakeup", sid, True, None), ("wakeup", sid, False, None)]
    assert sid not in engine._workflow_notes


# ── 契约登记 ───────────────────────────────────────────────


def test_event_is_registered_in_schemas_and_global_channel():
    assert "workflow.wakeup" in WS_EVENT_PAYLOAD_MODELS
    assert "workflow.wakeup" in ws_module._GLOBAL_FORWARD_EVENTS


async def test_broadcast_forwards_to_global_channel_without_unregistered_warning(caplog):
    sid = 424202

    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_text(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    fake = _FakeWs()
    ws_module.manager._global_conns.add(fake)
    try:
        with caplog.at_level(logging.WARNING, logger=ws_module.logger.name):
            await ws_module.manager.broadcast(
                sid, {"event": "workflow.wakeup", "payload": {"session_id": sid, "pending": True, "turn_id": None}},
            )
    finally:
        ws_module.manager._global_conns.discard(fake)
        ws_module.manager._buffers.pop(sid, None)
        ws_module.manager._seqs.pop(sid, None)

    assert not [r for r in caplog.records if "未登记" in r.getMessage()]
    assert fake.sent and fake.sent[0]["event"] == "workflow.wakeup"
    assert fake.sent[0]["payload"]["session_id"] == sid  # 全局通道注入 session_id，侧栏据此定位会话


def test_shared_events_and_client_handle_the_event():
    events = (_ROOT / "packages" / "shared" / "src" / "events.ts").read_text(encoding="utf-8")
    assert '{ event: "workflow.wakeup"; payload: { session_id: number; pending: boolean; turn_id?: number | null } }' in events
    ordered = events.split("export const ORDERED_EVENTS", 1)[1]
    assert '"workflow.wakeup"' in ordered  # 有序事件：断线补发时按序处理，待唤醒与接管不能颠倒
    store = (_ROOT / "client" / "src" / "store" / "chat.ts").read_text(encoding="utf-8")
    assert '"workflow.wakeup"' in store

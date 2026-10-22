"""plan-20261022-release-ci：子会话结论回报何时叫醒空闲的主代理。

规则：主代理（或同级子会话）派发的那一轮结束、没有被中断、且同一主会话下没有别的子会话还在跑，
结论才带 wake=True（主代理空闲时拉起一轮汇总）；否则只排队，等最后一个结束时一起带走。
用户在子会话里自己发起的轮次不叫醒主会话。
"""
import pytest

from app.core.enums import SideKind
from app.persistence.database import async_session_factory
from app.persistence.models.message import Session as SessionModel
from app.persistence.models.turn import Turn
from app.services import subsession_service


async def _setup(statuses: dict[int, str]) -> dict[int, int]:
    """主会话 #1 + 子会话 #11 / #12 …，每个子会话一条最近的 turn（给定状态）。返回 子会话 id → turn id。"""
    turns: dict[int, int] = {}
    async with async_session_factory() as db:
        db.add(SessionModel(id=1, title="主"))
        for sid in statuses:
            db.add(SessionModel(id=sid, title=f"子{sid}", side_parent_id=1, side_kind=SideKind.SUB.value))
        await db.flush()
        for sid, st in statuses.items():
            t = Turn(session_id=sid, status=st)
            db.add(t)
            await db.flush()
            turns[sid] = t.id
        await db.commit()
    return turns


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    async def fake_notify_main(db, **kw):
        calls.append(kw)
        return {"delivered": "queued"}

    monkeypatch.setattr(subsession_service, "notify_main", fake_notify_main)
    subsession_service._REPORTED_TURNS.clear()
    subsession_service._DISPATCHED_TURNS.clear()
    yield calls
    subsession_service._DISPATCHED_TURNS.clear()


async def test_last_dispatched_subsession_wakes_main(captured):
    turns = await _setup({11: "completed", 12: "completed"})
    subsession_service._mark_dispatched(turns[12])
    await subsession_service._report_finish(12, turns[12], "completed", "结论")
    assert captured[-1]["kind"] == "result"
    assert captured[-1]["wake"] is True
    assert turns[12] not in subsession_service._DISPATCHED_TURNS  # 用过即清


async def test_sibling_still_running_only_queues(captured):
    turns = await _setup({11: "running", 12: "completed"})
    subsession_service._mark_dispatched(turns[12])
    await subsession_service._report_finish(12, turns[12], "completed", "结论")
    assert captured[-1]["wake"] is False


async def test_user_driven_turn_does_not_wake(captured):
    turns = await _setup({11: "completed", 12: "completed"})
    # 未登记为派发：用户自己在子会话里发的消息
    await subsession_service._report_finish(12, turns[12], "completed", "结论")
    assert captured[-1]["wake"] is False


async def test_interrupted_turn_does_not_wake(captured):
    turns = await _setup({11: "completed", 12: "interrupted"})
    subsession_service._mark_dispatched(turns[12])
    await subsession_service._report_finish(12, turns[12], "interrupted", "")
    assert captured[-1]["wake"] is False


async def test_awaiting_confirmation_is_reported_as_its_own_status():
    turns = await _setup({11: "awaiting_confirmation"})
    async with async_session_factory() as db:
        brief = await subsession_service.describe(db, 11)
    assert brief.status == subsession_service.STATUS_AWAITING
    assert turns[11] > 0

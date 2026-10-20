"""会话邮箱的优先级、合并与投递决策单测（plan-474-2105）。

覆盖：
- P0 控制面消息在会话运行中 → 注入并请求打断；
- P2 汇报在会话空闲时不主动唤醒（避免白烧 token），排队等下一轮；
- P0 提问在会话空闲时拉起一轮；
- 同来源同类型在合并窗口内合并成一条；
- take_pending 取走排队项。
"""
import asyncio

import pytest

from app.orchestration import engine, mailbox


@pytest.fixture(autouse=True)
def _clean():
    mailbox.reset_for_tests()
    yield
    mailbox.reset_for_tests()


def test_priority_ordering():
    mailbox._push(mailbox.MailItem(kind="subsession_report", session_id=1, text="报告", priority=2))
    mailbox._push(mailbox.MailItem(kind="subsession_question", session_id=1, text="提问", priority=0))
    items = mailbox._sorted_queue(1)
    assert [it.text for it in items] == ["提问", "报告"]


def test_merge_same_source_within_window():
    first = mailbox.MailItem(kind="subsession_report", session_id=3, text="A",
                             source_session_id=9, priority=2)
    mailbox._push(first)
    second = mailbox.MailItem(kind="subsession_report", session_id=3, text="B",
                              source_session_id=9, priority=2)
    mailbox._push(second)
    items = mailbox._sorted_queue(3)
    assert len(items) == 1
    assert items[0].text == "A\nB"


def test_merge_skips_different_source():
    mailbox._push(mailbox.MailItem(kind="subsession_report", session_id=4, text="A",
                                   source_session_id=1, priority=2))
    mailbox._push(mailbox.MailItem(kind="subsession_report", session_id=4, text="B",
                                   source_session_id=2, priority=2))
    assert len(mailbox._sorted_queue(4)) == 2


@pytest.mark.asyncio
async def test_report_does_not_wake_idle_session(monkeypatch):
    woke: list[int] = []

    async def fake_wakeup(session_id, *, prompt, reason=""):
        woke.append(session_id)
        return 99

    monkeypatch.setattr(engine, "running_turn_id", lambda sid: None)
    monkeypatch.setattr(engine, "wakeup_session", fake_wakeup)

    res = await mailbox.publish(
        mailbox.MailKind.SUBSESSION_REPORT, session_id=7, text="进度", persist=False,
    )
    assert res["delivered"] == "queued"
    assert woke == []
    # 排队项仍可取走（下一轮带走）
    assert mailbox.pending_count(7) == 1
    assert mailbox.take_pending(7) == [{"content": "进度"}]


@pytest.mark.asyncio
async def test_question_wakes_idle_session(monkeypatch):
    async def fake_wakeup(session_id, *, prompt, reason=""):
        return 123

    monkeypatch.setattr(engine, "running_turn_id", lambda sid: None)
    monkeypatch.setattr(engine, "wakeup_session", fake_wakeup)

    res = await mailbox.publish(
        mailbox.MailKind.SUBSESSION_QUESTION, session_id=8, text="要不要改？",
        question_id="q_1_abcd", persist=False,
    )
    assert res["delivered"] == "woken"
    assert res["turn_id"] == 123
    assert mailbox.pending_count(8) == 0


@pytest.mark.asyncio
async def test_question_injects_and_requests_interrupt(monkeypatch):
    injected: list[tuple[int, dict]] = []
    interrupts: list[tuple[int, str]] = []

    monkeypatch.setattr(engine, "running_turn_id", lambda sid: 55)
    monkeypatch.setattr(engine, "inject_input", lambda tid, item: injected.append((tid, item)) or True)
    monkeypatch.setattr(engine, "request_interrupt",
                        lambda tid, reason="", source="": interrupts.append((tid, reason)) or True)

    res = await mailbox.publish(
        mailbox.MailKind.SUBSESSION_QUESTION, session_id=9, text="阻塞问题", persist=False,
    )
    assert res["delivered"] == "injected"
    assert injected and injected[0][0] == 55
    assert interrupts == [(55, "mailbox")]


@pytest.mark.asyncio
async def test_inject_failure_puts_item_back(monkeypatch):
    monkeypatch.setattr(engine, "running_turn_id", lambda sid: 66)
    monkeypatch.setattr(engine, "inject_input", lambda tid, item: False)
    monkeypatch.setattr(engine, "request_interrupt", lambda tid, reason="", source="": True)

    res = await mailbox.publish(
        mailbox.MailKind.BG_COMMAND_DONE, session_id=10, text="命令完成", persist=False,
    )
    assert res["delivered"] == "injected"
    # 注入失败 → 回队，不丢
    assert mailbox.pending_count(10) == 1

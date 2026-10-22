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
    # 排队项仍可取走（下一轮带走）；注入项带来源标记，不会被附上给用户消息的并行提醒
    assert mailbox.pending_count(7) == 1
    taken = mailbox.take_pending(7)
    assert [it["content"] for it in taken] == ["进度"]
    assert taken[0]["source"] == "mailbox"


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


# ── plan-20261022-release-ci：子会话结论唤醒与回放去重 ──


@pytest.mark.asyncio
async def test_result_without_wake_is_queued(monkeypatch):
    """同组还有子会话在跑时结论只排队（P1，不叫醒空闲主代理）。"""
    woke: list[int] = []

    async def fake_wakeup(session_id, *, prompt, reason=""):
        woke.append(session_id)
        return 1

    monkeypatch.setattr(engine, "running_turn_id", lambda sid: None)
    monkeypatch.setattr(engine, "wakeup_session", fake_wakeup)

    res = await mailbox.publish(
        mailbox.MailKind.SUBSESSION_RESULT, session_id=21, text="A 结束", persist=False,
        source_session_id=31, message_id=500, wake=False,
    )
    assert res["delivered"] == "queued"
    assert woke == []


@pytest.mark.asyncio
async def test_last_result_wakes_with_pointer_prompt(monkeypatch):
    """最后一个子会话结束（wake=True）：拉起一轮，排队中的结论一起带走；
    已落库会被历史回放的原文不再贴进唤醒提示词。"""
    prompts: list[tuple[str, str]] = []

    async def fake_wakeup(session_id, *, prompt, reason=""):
        prompts.append((prompt, reason))
        return 77

    monkeypatch.setattr(engine, "running_turn_id", lambda sid: None)
    monkeypatch.setattr(engine, "wakeup_session", fake_wakeup)

    await mailbox.publish(
        mailbox.MailKind.SUBSESSION_RESULT, session_id=22, text="A 的结论原文" * 50, persist=False,
        source_session_id=31, message_id=500, wake=False,
    )
    res = await mailbox.publish(
        mailbox.MailKind.SUBSESSION_RESULT, session_id=22, text="B 的结论原文" * 50, persist=False,
        source_session_id=32, message_id=501, wake=True,
    )
    assert res["delivered"] == "woken" and res["count"] == 2
    prompt, reason = prompts[0]
    assert reason == mailbox.MailKind.SUBSESSION_RESULT.value
    assert "A 的结论原文" not in prompt and "B 的结论原文" not in prompt
    assert "2 条子会话消息" in prompt and "整合" in prompt
    assert mailbox.pending_count(22) == 0


@pytest.mark.asyncio
async def test_unpersisted_items_keep_full_text_in_wake_prompt(monkeypatch):
    """没有可回放落库副本的消息（如后台命令完成）仍把原文放进唤醒提示词。"""
    prompts: list[str] = []

    async def fake_wakeup(session_id, *, prompt, reason=""):
        prompts.append(prompt)
        return 78

    monkeypatch.setattr(engine, "running_turn_id", lambda sid: None)
    monkeypatch.setattr(engine, "wakeup_session", fake_wakeup)

    await mailbox.publish(mailbox.MailKind.BG_COMMAND_DONE, session_id=23, text="npm test 退出码 0", persist=False)
    assert prompts == ["npm test 退出码 0"]


def test_take_pending_drops_items_replayed_by_history():
    """轮次开始时：落库副本已被历史回放（id ≤ 判定线）的条目丢弃，之后才落库的照常注入。"""
    mailbox._push(mailbox.MailItem(kind="subsession_report", session_id=24, text="旧汇报", message_id=100))
    mailbox._push(mailbox.MailItem(kind="subsession_report", session_id=24, text="新汇报",
                                   source_session_id=9, message_id=205))
    mailbox._push(mailbox.MailItem(kind="bg_command_done", session_id=24, text="命令完成", priority=0))
    taken = mailbox.take_pending(24, replayed_upto=200)
    assert [it["content"] for it in taken] == ["命令完成", "新汇报"]
    assert all(it["source"] == "mailbox" for it in taken)


def test_engine_filters_injected_items_replayed_by_history():
    """注入队列里同样按判定线去重；没有 message_id 的（用户追加）一律保留。"""
    items = [
        {"content": "用户追加"},
        {"content": "已回放的汇报", "source": "mailbox", "message_id": 90},
        {"content": "新的提问", "source": "mailbox", "message_id": 120, "priority": 0},
    ]
    kept = engine._not_replayed(items, 100)
    assert [it["content"] for it in kept] == ["用户追加", "新的提问"]
    assert engine._not_replayed(items, None) == items

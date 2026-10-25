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


# ── plan-549-2281 修复：合并不得污染可回放性（message_id 身份） ──


def test_merge_keeps_known_message_id():
    """两条都带 message_id → 合并成一条，并保留已知的最大 id（回放判定线不能丢）。"""
    mailbox._push(mailbox.MailItem(kind="subagent_report", session_id=51, text="A",
                                   source_session_id=51, priority=2, message_id=100))
    mailbox._push(mailbox.MailItem(kind="subagent_report", session_id=51, text="B",
                                   source_session_id=51, priority=2, message_id=101))
    items = mailbox._sorted_queue(51)
    assert len(items) == 1
    assert items[0].text == "A\nB"
    assert items[0].message_id == 101


def test_merge_refuses_mixed_identity_items():
    """一条有 message_id、一条没有 → 不合并，各自独立入队。

    旧实现在这种情况下把合并项的 message_id 清成 None，使 take_pending 的
    `message_id is None or ...` 条件无条件保留它——已被历史回放的那条会被再注入一次。
    """
    mailbox._push(mailbox.MailItem(kind="subagent_report", session_id=52, text="已落库",
                                   source_session_id=52, priority=2, message_id=200))
    mailbox._push(mailbox.MailItem(kind="subagent_report", session_id=52, text="落库失败",
                                   source_session_id=52, priority=2, message_id=None))
    items = mailbox._sorted_queue(52)
    assert len(items) == 2, "身份不同的两条不能合并，否则可回放性被污染"
    assert [it.text for it in items] == ["已落库", "落库失败"]
    assert items[0].message_id == 200
    assert items[1].message_id is None


def test_merge_both_none_still_merges():
    """两条都没有 message_id（都只能靠注入送达）→ 仍可合并，message_id 保持 None。"""
    mailbox._push(mailbox.MailItem(kind="bg_command_done", session_id=53, text="A",
                                   priority=0))
    mailbox._push(mailbox.MailItem(kind="bg_command_done", session_id=53, text="B",
                                   priority=0))
    items = mailbox._sorted_queue(53)
    assert len(items) == 1
    assert items[0].message_id is None


def test_take_pending_keeps_mixed_identity_items_apart():
    """混合身份的两条在 take_pending 中各按自己的 id 判定：已回放的丢弃，未落库的保留。"""
    mailbox._push(mailbox.MailItem(kind="subagent_report", session_id=54, text="已回放",
                                   source_session_id=54, priority=2, message_id=300))
    mailbox._push(mailbox.MailItem(kind="subagent_report", session_id=54, text="待注入",
                                   source_session_id=54, priority=2, message_id=None))
    taken = mailbox.take_pending(54, replayed_upto=300)
    assert [it["content"] for it in taken] == ["待注入"]
    assert taken[0]["message_id"] is None


def test_merged_item_is_not_reinjected_when_replayed():
    """合并后的条目带最大 id：判定线覆盖它时整条丢弃，不会「回放 + 注入」双份。"""
    mailbox._push(mailbox.MailItem(kind="subagent_report", session_id=55, text="A",
                                   source_session_id=55, priority=2, message_id=400))
    mailbox._push(mailbox.MailItem(kind="subagent_report", session_id=55, text="B",
                                   source_session_id=55, priority=2, message_id=401))
    assert mailbox.take_pending(55, replayed_upto=401) == []


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
    # plan-549-2281：同一条唤醒通道现在同时服务子会话历史与子代理，文案已中性化为「任务消息」
    assert "2 条任务消息" in prompt and "整合" in prompt
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


# ── plan-549-2281 复盘补测：子代理新 kind 走通「注入/打断/排队」全流程 ──


@pytest.mark.asyncio
async def test_subagent_question_injects_and_interrupts(monkeypatch):
    """SUBAGENT_QUESTION 与旧 subsession_question 同一条 P0 打断链路（此前只有常量断言）。"""
    injected: list[tuple[int, dict]] = []
    interrupts: list[tuple[int, str]] = []

    monkeypatch.setattr(engine, "running_turn_id", lambda sid: 55)
    monkeypatch.setattr(engine, "inject_input", lambda tid, item: injected.append((tid, item)) or True)
    monkeypatch.setattr(engine, "request_interrupt",
                        lambda tid, reason="", source="": interrupts.append((tid, source)) or True)

    res = await mailbox.publish(
        mailbox.MailKind.SUBAGENT_QUESTION, session_id=31, text="【子代理提问 subagent#3】A 还是 B？",
        persist=False,
    )
    assert res["delivered"] == "injected"
    assert injected[0][1]["content"].startswith("【子代理提问")
    assert interrupts == [(55, mailbox.MailKind.SUBAGENT_QUESTION.value)]


@pytest.mark.asyncio
async def test_subagent_report_queues_without_waking_idle(monkeypatch):
    """进度上报 P2：会话空闲时只排队（不叫醒），等下一轮带走——不再绕开邮箱直写注入队列。"""
    woke: list[int] = []

    async def fake_wakeup(session_id, *, prompt, reason=""):
        woke.append(session_id)
        return 99

    monkeypatch.setattr(engine, "running_turn_id", lambda sid: None)
    monkeypatch.setattr(engine, "wakeup_session", fake_wakeup)

    res = await mailbox.publish(
        mailbox.MailKind.SUBAGENT_REPORT, session_id=32, text="【子代理进度上报 subagent#3】过半", persist=False,
    )
    assert res["delivered"] == "queued"
    assert woke == []
    assert mailbox.pending_count(32) == 1


def test_has_injected_inputs_urgent_filter():
    """plan-549-2281 守卫承接：等待类工具据 urgent_only 判定「有控制面消息该让出等待」。

    原用例随 test_subsession_wait_and_guards.py 删除而丢失，这里补回（engine 的
    has_injected_inputs 仍在 agent_loop 的等待让出点被使用，行为必须保持）。"""
    engine._pending_inputs.clear()
    try:
        assert engine.has_injected_inputs(1) is False
        engine._pending_inputs[1] = [{"content": "进度上报", "priority": 2}]
        assert engine.has_injected_inputs(1) is True                      # 有任何注入项
        assert engine.has_injected_inputs(1, urgent_only=True) is False   # 汇报不值得打断
        engine._pending_inputs[1].append({"content": "子代理提问", "priority": 0})
        assert engine.has_injected_inputs(1, urgent_only=True) is True    # 提问必须尽快读到
        engine._pending_inputs[2] = [{"content": "用户追加"}]             # 无 priority 字段按紧急
        assert engine.has_injected_inputs(2, urgent_only=True) is True
    finally:
        engine._pending_inputs.clear()

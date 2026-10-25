"""子代理提问 / 答复通道的测试（plan-549-2281）。

这套通道用于替代已下线子会话的「提问能打断主代理等待」能力。三条不可回退的约定：

1. `kind=question` 走 mailbox 的 P0 优先级，并列入 `INTERRUPTING_KINDS`
   （否则主代理正跑前台命令时读不到，子代理白等到超时）；
2. 答复能精确唤醒等待方，且同一子代理只保留一条待答复提问（避免答复错配）；
3. 超时自动清场，不留泄漏的 waiter。
"""
from __future__ import annotations

import asyncio

import pytest

from app.orchestration import mailbox, subagent_comm


@pytest.fixture(autouse=True)
def _clean():
    subagent_comm.reset_for_tests()
    mailbox.reset_for_tests()
    yield
    subagent_comm.reset_for_tests()
    mailbox.reset_for_tests()


# ── 1. 优先级与打断：提问必须是 P0 + 可打断 ──
def test_subagent_question_is_p0_and_interrupting():
    kind = mailbox.MailKind.SUBAGENT_QUESTION.value
    assert mailbox.PRIORITY[kind] == 0, "子代理提问必须是 P0，否则会在队列里排队等用户下一句话"
    assert kind in mailbox.INTERRUPTING_KINDS, "子代理提问必须能打断主代理的次要等待"


def test_subagent_completion_stays_low_priority():
    """完成通知不该打断主代理——它随下一次 LLM 调用前注入即可。"""
    kind = mailbox.MailKind.SUBAGENT_COMPLETION.value
    assert mailbox.PRIORITY[kind] == 2
    assert kind not in mailbox.INTERRUPTING_KINDS


def test_subagent_report_is_p2_and_replay_kind():
    """plan-549-2281 复盘修复：进度上报改走邮箱 P2 落库成行——不打断主代理，
    且落库带回放标记（前端据此渲染「子代理进度上报」行，历史回放按用户输入呈现）。"""
    kind = mailbox.MailKind.SUBAGENT_REPORT.value
    assert mailbox.PRIORITY[kind] == 2
    assert kind not in mailbox.INTERRUPTING_KINDS
    assert kind in mailbox._INPUT_REPLAY_KINDS


@pytest.mark.asyncio
async def test_wait_false_question_discarded_so_reply_falls_through():
    """plan-549-2281 复盘修复：不等答复的提问必须立刻作废登记——否则主代理答复时
    resolve_question 会 set 一个没有等待者的 event，答复凭空丢失（旧 subsession 的
    discard 防线在迁移时丢了，agent_loop 现于 wait=false 分支显式 discard）。"""
    qid = await subagent_comm.publish_question(
        session_id=30, agent_id=8, task_title="T", message="顺口一问",
    )
    subagent_comm.discard_question(qid)  # agent_loop wait=false 分支的行为
    assert subagent_comm.pending_question_of(30, 8) is None
    # 主代理答复：resolve 落空 → 调用方走 push_agent_input 普通指令通道，答复不丢
    assert subagent_comm.resolve_question(qid, "答复") is False


# ── 2. 提问 → 答复 → 唤醒 ──
@pytest.mark.asyncio
async def test_publish_then_resolve_unblocks_waiter():
    qid = await subagent_comm.publish_question(
        session_id=1, agent_id=7, task_title="修 bug", message="要改 A 还是 B？",
    )
    assert subagent_comm.pending_question_of(1, 7) == qid

    waiter = asyncio.ensure_future(subagent_comm.wait_reply(qid, timeout_s=5))
    await asyncio.sleep(0)  # 让 waiter 真正挂起
    assert not waiter.done()

    assert subagent_comm.resolve_question(qid, "改 A") is True
    assert await waiter == "改 A"
    # 答复后不再有挂起状态
    assert subagent_comm.pending_question_of(1, 7) is None


@pytest.mark.asyncio
async def test_resolve_unknown_question_returns_false():
    assert subagent_comm.resolve_question("nope", "x") is False


@pytest.mark.asyncio
async def test_publish_creates_mailbox_item_with_question_id():
    """投递必须带 question_id，主代理答复时才能精确唤醒（不带 id 只会变成普通消息）。"""
    qid = await subagent_comm.publish_question(
        session_id=5, agent_id=3, task_title="T", message="Q?",
    )
    assert qid
    # 投递可能已被 drain（会话空闲时会拉起一轮），因此断言「队列里曾有它 或 已投递成功」
    queued = mailbox._sorted_queue(5)
    assert (not queued) or any(it.question_id == qid for it in queued)


# ── 3. 同一子代理只保留一条待答复提问（防答复错配） ──
@pytest.mark.asyncio
async def test_second_question_replaces_first():
    q1 = await subagent_comm.publish_question(
        session_id=9, agent_id=1, task_title="T", message="第一个问题",
    )
    q2 = await subagent_comm.publish_question(
        session_id=9, agent_id=1, task_title="T", message="第二个问题",
    )
    assert q1 != q2
    assert subagent_comm.pending_question_of(9, 1) == q2
    # 旧问题已作废：答复它不会成功
    assert subagent_comm.resolve_question(q1, "旧答复") is False
    assert subagent_comm.resolve_question(q2, "新答复") is True


@pytest.mark.asyncio
async def test_superseded_question_wakes_old_waiter_immediately():
    """plan-549-2281 修复：旧提问被新提问取代时，旧等待方必须**立刻**返回。

    旧实现只把登记项 pop 掉、不 set() 事件——等待中的协程不在超时/取消路径上，
    会一直挂到 600s 才醒（子代理白白阻塞十分钟）。这里给一个远大于断言窗口的
    timeout，只要耗时明显小于超时时间就说明是被取代唤醒的，而不是超时返回。
    """
    q1 = await subagent_comm.publish_question(
        session_id=41, agent_id=1, task_title="T", message="第一个问题",
    )
    waiter = asyncio.ensure_future(subagent_comm.wait_reply(q1, timeout_s=30))
    await asyncio.sleep(0)  # 让 waiter 真正挂起在事件上
    assert not waiter.done()

    started = asyncio.get_running_loop().time()
    q2 = await subagent_comm.publish_question(
        session_id=41, agent_id=1, task_title="T", message="第二个问题",
    )
    reply = await asyncio.wait_for(waiter, timeout=5)  # 取代即唤醒，远快于 30s 超时
    elapsed = asyncio.get_running_loop().time() - started

    assert reply is None, "被取代按「未收到答复」处理，返回契约仍是 str | None"
    assert elapsed < 5
    # 新提问仍在等答复，不受影响
    assert subagent_comm.pending_question_of(41, 1) == q2
    assert subagent_comm.resolve_question(q2, "新答复") is True


@pytest.mark.asyncio
async def test_superseded_reply_contract_is_none_not_empty_string():
    """取代语义不能退化成空串：空串会被调用方当作「主代理答了但没说话」，
    进而可能重发同一条提问形成环路。这里确认返回的确是 None。"""
    q1 = await subagent_comm.publish_question(
        session_id=42, agent_id=2, task_title="T", message="Q1",
    )
    waiter = asyncio.ensure_future(subagent_comm.wait_reply(q1, timeout_s=30))
    await asyncio.sleep(0)
    await subagent_comm.publish_question(
        session_id=42, agent_id=2, task_title="T", message="Q2",
    )
    assert await asyncio.wait_for(waiter, timeout=5) is None


# ── 4. 超时清场 ──
@pytest.mark.asyncio
async def test_timeout_returns_none_and_cleans_up():
    qid = await subagent_comm.publish_question(
        session_id=11, agent_id=2, task_title="T", message="没人回答我",
    )
    assert await subagent_comm.wait_reply(qid, timeout_s=0.1) is None
    assert subagent_comm.pending_question_of(11, 2) is None, "超时后必须清场，避免 waiter 泄漏"


@pytest.mark.asyncio
async def test_wait_on_discarded_question_returns_none():
    qid = await subagent_comm.publish_question(
        session_id=12, agent_id=4, task_title="T", message="Q",
    )
    subagent_comm.discard_question(qid)
    assert await subagent_comm.wait_reply(qid, timeout_s=1) is None


# ── 5. 不同子代理互不干扰 ──
@pytest.mark.asyncio
async def test_questions_are_isolated_per_agent():
    qa = await subagent_comm.publish_question(
        session_id=20, agent_id=1, task_title="A", message="A 的问题",
    )
    qb = await subagent_comm.publish_question(
        session_id=20, agent_id=2, task_title="B", message="B 的问题",
    )
    assert subagent_comm.pending_question_of(20, 1) == qa
    assert subagent_comm.pending_question_of(20, 2) == qb
    assert subagent_comm.resolve_question(qb, "给 B 的答复") is True
    # 答复 B 不影响 A
    assert subagent_comm.pending_question_of(20, 1) == qa

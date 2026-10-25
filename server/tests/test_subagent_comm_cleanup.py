"""子代理提问登记的清理与泄漏回归用例（plan-560-2311 B4）。

缺陷原貌（三处，都指向「主代理从未收到的提问留在登记表里」）：

- `publish_question` 先写 `_questions` / `_by_agent`，再 `await mailbox.publish`，而它的
  `except` 只捕 `Exception`——`asyncio.CancelledError` 在 Python 3.8+ 继承 BaseException，
  捕不到。取消路径上登记项永久残留，`send_to_subagent` 随后 `resolve_question` 仍返回成功
  并声称「子代理已恢复」（假唤醒）。
- 子代理进入终态时无人清理登记表；`subagent_comm` 也没有按 session / agent 批量清理的 API。
- `resolve_question` pop 前不校验登记是否仍指向该 qid（防御性缺口）。

本文件的用例把「取消后不残留」「终态能清干净」「旧 qid 不能误删新提问」三件事固定下来。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestration import mailbox, subagent_comm  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """清登记表，并把 `mailbox.publish` 换成不落库的轻量桩。

    本文件测的是登记表语义（残留 / 清理 / 索引指向），真实落库只会让每个用例多花 1~4 秒；
    publish 的落库行为由 `test_subagent_completion_channel.py` 覆盖。用例自身还要替换
    publish 时会覆盖这里的桩（monkeypatch 由用例后设置、先生效）。
    """
    subagent_comm.reset_for_tests()
    mailbox.reset_for_tests()

    async def _queued_publish(kind, **kwargs):
        return {"delivered": "queued", "kind": getattr(kind, "value", kind)}

    monkeypatch.setattr(mailbox, "publish", _queued_publish)
    yield
    subagent_comm.reset_for_tests()
    mailbox.reset_for_tests()


def _ask(session_id: int, agent_id: int, message: str = "问题"):
    return subagent_comm.publish_question(
        session_id=session_id, agent_id=agent_id, task_title="T", message=message,
    )


# ── 1. 投递被取消后不得残留登记 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancelled_publish_rolls_back_registration(monkeypatch):
    """plan-560-2311 B4：`mailbox.publish` 抛 CancelledError 时登记必须回滚。

    旧实现只捕 `Exception`，`CancelledError` 直接穿透，`_by_agent` 留着一条主代理从未
    收到的提问；之后 `send_to_subagent` 的 `resolve_question` 会「成功」并回复
    「it resumes immediately」，实际没有任何等待方被唤醒。
    """

    async def _cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mailbox, "publish", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await _ask(1, 7, "这条投递会被取消")

    assert subagent_comm.pending_question_of(1, 7) is None, "取消路径不得留下待答复登记"


@pytest.mark.asyncio
async def test_cancelled_publish_leaves_no_questions_and_no_false_wakeup(monkeypatch):
    """回滚后该 qid 不可被「答复」——否则就是一条假的成功唤醒。"""

    async def _cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mailbox, "publish", _cancelled)

    qid = None
    with pytest.raises(asyncio.CancelledError):
        qid = await _ask(2, 9)
    assert qid is None  # 取消向上传递，调用方拿不到 qid

    # 任意 qid 的答复都落空（登记已回滚）
    assert subagent_comm.resolve_question("whatever", "答复") is False
    assert subagent_comm.pending_question_of(2, 9) is None


@pytest.mark.asyncio
async def test_ordinary_publish_failure_also_rolls_back(monkeypatch):
    """普通异常路径（旧实现已捕 Exception）同样要回滚，避免修复只覆盖一半。"""

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("mailbox 落库失败")

    monkeypatch.setattr(mailbox, "publish", _boom)

    qid = await _ask(3, 5)  # 不抛出：投递失败不让子代理崩，它仍会等到超时
    assert qid
    assert subagent_comm.pending_question_of(3, 5) is None


@pytest.mark.asyncio
async def test_cancel_keeps_newer_question_intact(monkeypatch):
    """回滚只删仍指向自己的登记：投递期间已发出新提问时，不能把新登记误删。"""
    real_publish = mailbox.publish
    state = {"calls": 0}

    async def _cancel_second(kind, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            # 第二次投递「正在进行时」子代理又发了第三条：模拟并发提问
            subagent_comm._questions["manual-new"] = subagent_comm._PendingQuestion(
                question_id="manual-new", session_id=4, agent_id=1,
                task_title="T", message="更新的问题",
            )
            subagent_comm._by_agent[(4, 1)] = "manual-new"
            raise asyncio.CancelledError()
        return await real_publish(kind, **kwargs)

    monkeypatch.setattr(mailbox, "publish", _cancel_second)

    await _ask(4, 1, "第一条")
    with pytest.raises(asyncio.CancelledError):
        await _ask(4, 1, "第二条")

    assert subagent_comm.pending_question_of(4, 1) == "manual-new", (
        "回滚不能误删投递期间产生的新登记"
    )
    subagent_comm.reset_for_tests()


# ── 2. 终态批量清理 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_agent_removes_only_that_agent():
    """plan-560-2311 B4：`clear_agent(session_id, agent_id)` 清该子代理，不误伤同会话其他子代理。"""
    qa = await _ask(10, 1, "A 的问题")
    qb = await _ask(10, 2, "B 的问题")

    subagent_comm.clear_agent(10, 1)

    assert subagent_comm.pending_question_of(10, 1) is None
    assert subagent_comm.pending_question_of(10, 2) == qb
    assert qa not in subagent_comm._questions
    assert qb in subagent_comm._questions


@pytest.mark.asyncio
async def test_clear_agent_wakes_waiter_immediately():
    """被清掉的等待方按 superseded 语义立刻返回 None，不空等到 600s 超时。"""
    qid = await _ask(11, 3, "子代理已终止")
    waiter = asyncio.ensure_future(subagent_comm.wait_reply(qid, timeout_s=30))
    await asyncio.sleep(0)
    assert not waiter.done()

    subagent_comm.clear_agent(11, 3)

    assert await asyncio.wait_for(waiter, timeout=5) is None


@pytest.mark.asyncio
async def test_clear_agent_is_noop_for_unknown_agent():
    """清一个没有待答复提问的子代理不能抛错（终态函数会无条件调用）。"""
    subagent_comm.clear_agent(12, 99)
    assert subagent_comm.pending_question_of(12, 99) is None


@pytest.mark.asyncio
async def test_clear_session_removes_all_questions_of_that_session():
    """plan-560-2311 B4：`clear_session(session_id)` 清该会话全部提问，其他会话不受影响。"""
    q1 = await _ask(20, 1, "会话 20 的问题一")
    q2 = await _ask(20, 2, "会话 20 的问题二")
    other = await _ask(21, 1, "会话 21 的问题")

    subagent_comm.clear_session(20)

    assert subagent_comm.pending_question_of(20, 1) is None
    assert subagent_comm.pending_question_of(20, 2) is None
    assert subagent_comm.pending_question_of(21, 1) == other
    for qid in (q1, q2):
        assert qid not in subagent_comm._questions


@pytest.mark.asyncio
async def test_clear_session_accepts_string_id():
    """会话 id 可能以字符串形态传入（前端 / 网关路径），清理必须同样生效。"""
    await _ask(30, 1, "问题")
    subagent_comm.clear_session("30")
    assert subagent_comm.pending_question_of(30, 1) is None


# ── 3. 旧 qid 不得误删新提问的索引 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_superseded_qid_keeps_new_question():
    """plan-560-2311 B4：答复已被取代的旧 qid 成功后，新提问的 `_by_agent` 索引必须还在。"""
    old_qid = await _ask(40, 1, "旧问题")
    new_qid = await _ask(40, 1, "新问题")
    assert subagent_comm.pending_question_of(40, 1) == new_qid

    assert subagent_comm.resolve_question(old_qid, "给旧问题的答复") is False
    assert subagent_comm.pending_question_of(40, 1) == new_qid, (
        "旧 qid 的答复不能把新提问的索引一起 pop 掉"
    )
    assert subagent_comm.resolve_question(new_qid, "给新问题的答复") is True


@pytest.mark.asyncio
async def test_discard_superseded_qid_keeps_new_question():
    """丢弃旧 qid 同样不能误删新提问索引（`discard_question` 与 resolve 同口径）。"""
    old_qid = await _ask(41, 1, "旧问题")
    new_qid = await _ask(41, 1, "新问题")

    subagent_comm.discard_question(old_qid)

    assert subagent_comm.pending_question_of(41, 1) == new_qid


@pytest.mark.asyncio
async def test_resolve_returns_false_when_index_points_elsewhere():
    """索引已指向别处时，resolve 必须先归位再返回 False，不能把登记吞掉。"""
    qid = await _ask(42, 1, "问题")
    # 人为把索引指向另一个 qid：模拟「同一子代理的新提问已覆盖登记」
    subagent_comm._by_agent[(42, 1)] = "another"

    assert subagent_comm.resolve_question(qid, "答复") is False
    assert qid in subagent_comm._questions, "未被本函数消费的登记必须归位，否则等待方永远收不到答复"

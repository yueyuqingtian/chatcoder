"""子会话提问注册表单测（plan-474-2105）。

只测进程内的等待/答复语义（不碰数据库）：
- register → wait → resolve 能唤醒等待方并拿到答复原文；
- 超时返回 None 并清理登记；
- 对不存在或已超时的 question_id 调 resolve 返回 False（调用方退回普通发消息）。
"""
import asyncio

import pytest

from app.services import subsession_service as ss


@pytest.fixture(autouse=True)
def _clean():
    ss._PENDING_QUESTIONS.clear()
    yield
    ss._PENDING_QUESTIONS.clear()


def test_question_id_unique_per_subsession():
    a = ss.new_question_id(12)
    b = ss.new_question_id(12)
    assert a != b
    assert a.startswith("q_12_") and b.startswith("q_12_")


@pytest.mark.asyncio
async def test_resolve_wakes_waiter():
    qid = ss.new_question_id(5)
    ss.register_question(qid)
    waiter = asyncio.ensure_future(ss.wait_for_question_reply(qid, 2.0))
    await asyncio.sleep(0)  # 让等待方挂起

    assert ss.resolve_question(qid, "按方案 A 做") is True
    assert await waiter == "按方案 A 做"
    # 已消费，登记被清理
    assert ss.pending_question_count() == 0


@pytest.mark.asyncio
async def test_timeout_returns_none_and_cleans_up():
    qid = ss.new_question_id(6)
    ss.register_question(qid)
    assert await ss.wait_for_question_reply(qid, 0.05) is None
    assert ss.pending_question_count() == 0


def test_resolve_unknown_question_returns_false():
    assert ss.resolve_question("q_404_none", "答案") is False


def test_register_is_idempotent():
    qid = ss.new_question_id(7)
    ss.register_question(qid)
    first = ss._PENDING_QUESTIONS[qid]
    ss.register_question(qid)
    assert ss._PENDING_QUESTIONS[qid] is first
    assert ss.pending_question_count() == 1

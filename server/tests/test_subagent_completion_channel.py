"""子代理完成通知「单一模型通道」回归用例（plan-560-2311 B2）。

缺陷原貌：同一条完成通知走了两条路——`subagent._completion_queue`（面向模型的
`_format_completion_note`）与 `mailbox.publish(SUBAGENT_COMPLETION)`（面向消息流的
`format_completion_row`，且带 `SUBSESSION_REPORT_FLAG`）。后者当时在
`_INPUT_REPLAY_KINDS` 内，于是运行中经 `deliver` 注入一次、重建历史时又作为 user 消息
回放一次，模型对同一个子代理收到两份。去重对它无效——`_history_cutoff` 只比对带
`message_id` 的项，而 note 没有 `message_id`。

plan-560-2311 B2 的收敛口径：邮箱这一份降级为**纯展示**（落库成行给前端渲染，不进模型
上下文），模型通道只保留 `_completion_queue`。下面的用例钉住四个不变量：

1. `_INPUT_REPLAY_KINDS` 不含 `SUBAGENT_COMPLETION`（不再参与注入与历史回放）；
2. 存在「仅展示」集合且包含它（落库仍成行，前端行不能丢）；
3. 无生产者的历史子会话枚举即使还留在 `MailKind` 里，也不得被当作可回放类型消费；
4. 落库内容不带 `SUBSESSION_REPORT_FLAG`——否则 `context_manager` 仍会把它当用户输入回放。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestration import mailbox  # noqa: E402
from app.persistence.database import async_session_factory  # noqa: E402
from app.services import session_service  # noqa: E402

COMPLETION = mailbox.MailKind.SUBAGENT_COMPLETION.value


@pytest.fixture(autouse=True)
def _clean():
    mailbox.reset_for_tests()
    yield
    mailbox.reset_for_tests()


# ── 1. 不再参与模型注入与历史回放 ──────────────────────────────────────────────

def test_completion_not_in_input_replay_kinds():
    """plan-560-2311 B2：完成通知移出回放集合——这是双份通知的根因所在。"""
    assert COMPLETION not in mailbox._INPUT_REPLAY_KINDS


def test_replay_kinds_only_hold_subagent_messages_that_need_reply():
    """回放集合收缩到「需要主代理回应」的两类；多出来的类型就是重复注入的来源。"""
    assert mailbox._INPUT_REPLAY_KINDS == frozenset({
        mailbox.MailKind.SUBAGENT_QUESTION.value,
        mailbox.MailKind.SUBAGENT_REPORT.value,
    })


# ── 2. 纯展示集合存在且包含完成通知 ────────────────────────────────────────────

def test_display_only_kinds_contains_completion():
    """plan-560-2311 B2：完成通知改由「仅展示」集合表达——不注入、不唤醒、不带进下一轮。"""
    assert hasattr(mailbox, "_DISPLAY_ONLY_KINDS"), "缺少纯展示集合，完成通知的去向就不明确"
    assert COMPLETION in mailbox._DISPLAY_ONLY_KINDS


def test_display_only_and_replay_kinds_are_disjoint():
    """两个集合必须互斥：同时可回放又可仅展示，等于既注入一次又回放一次。"""
    assert not (mailbox._DISPLAY_ONLY_KINDS & mailbox._INPUT_REPLAY_KINDS)


# ── 3. 已下线的历史子会话枚举不得被当成可回放类型 ───────────────────────────────

def test_legacy_subsession_kinds_are_not_replay_consumed():
    """子会话能力已下线，这些枚举即便还留在 `MailKind` 里也不能参与回放。

    plan-560-2311 B2：用 `getattr` 容错取值——将来彻底删掉这些枚举时本用例应当仍然通过，
    不会因为「属性不存在」而变成需要跟着改的脆弱断言。
    """
    for name in ("SUBSESSION_QUESTION", "SUBSESSION_REPORT",
                 "SUBSESSION_RESULT", "MAIN_REPLY"):
        member = getattr(mailbox.MailKind, name, None)
        if member is None:
            continue  # 已删除：符合 B2 的清理方向
        assert member.value not in mailbox._INPUT_REPLAY_KINDS, (
            f"{name} 已无生产者，不得再进入模型回放集合"
        )


# ── 4. 落库内容不带「按用户输入回放」标记 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_published_completion_row_has_no_replay_flag():
    """plan-560-2311 B2：落库副本不带 `SUBSESSION_REPORT_FLAG`。

    该标记决定 `context_manager` 重建历史时是否把它当作用户输入回放。带标记 = 模型
    在历史里再读一遍，与 `_completion_queue` 的注入叠加成两份。
    """
    from app.orchestration.subsession_report import SUBSESSION_REPORT_FLAG
    from app.persistence.models.message import Message
    from sqlalchemy import select

    async with async_session_factory() as db:
        sid = await session_service.create_session(db, project_id=None, title="[测试] 完成通知通道")
        try:
            res = await mailbox.publish(
                mailbox.MailKind.SUBAGENT_COMPLETION, session_id=sid,
                text="【子代理完成 subagent#3】已完成", wake=True,
            )
            # 纯展示：投递阶段被摘掉，既没注入也没唤醒。
            # plan-562-2318 M16b：返回值为 display_only（消息已落库、会出现在消息流里，
            # 报“empty”会让调用方误判没送达）——此处的断言随之对齐语义。
            assert res["delivered"] == "display_only"
            assert mailbox.pending_count(sid) == 0

            row = (
                await db.execute(select(Message).where(Message.session_id == sid))
            ).scalars().first()
            assert row is not None, "纯展示不等于不落库——前端消息流要靠这一行渲染"
            assert SUBSESSION_REPORT_FLAG not in (row.content or {})
            # 前端按 mailbox.kind 选渲染分支，这个字段不能丢
            assert row.content["mailbox"]["kind"] == COMPLETION
        finally:
            await session_service.delete_session_permanent(db, sid)


@pytest.mark.asyncio
async def test_published_question_row_keeps_replay_flag():
    """对照组：提问仍必须带回放标记——否则就问不出「哪条需要主代理答复」了。

    没有这条对照，上一条「不带标记」可能因为 publish 整体不写标记而恒真。
    """
    from app.orchestration.subsession_report import SUBSESSION_REPORT_FLAG
    from app.persistence.models.message import Message
    from sqlalchemy import select

    async with async_session_factory() as db:
        sid = await session_service.create_session(db, project_id=None, title="[测试] 提问通道")
        try:
            await mailbox.publish(
                mailbox.MailKind.SUBAGENT_QUESTION, session_id=sid,
                text="【子代理提问 subagent#3】改 A 还是 B？", question_id="q-1", wake=False,
            )
            row = (
                await db.execute(select(Message).where(Message.session_id == sid))
            ).scalars().first()
            assert row is not None
            assert row.content.get(SUBSESSION_REPORT_FLAG) is True
        finally:
            await session_service.delete_session_permanent(db, sid)


# ── 5. 纯展示项不随 take_pending 进入下一轮 ────────────────────────────────────

@pytest.mark.asyncio
async def test_take_pending_drops_display_only_items():
    """即使完成通知因某种原因留在队列里，take_pending 也不得把它带进模型上下文。"""
    mailbox._push(mailbox.MailItem(
        kind=COMPLETION, session_id=77, text="子代理已完成", priority=2,
    ))
    mailbox._push(mailbox.MailItem(
        kind=mailbox.MailKind.SUBAGENT_REPORT.value, session_id=77, text="进度上报", priority=2,
    ))

    taken = mailbox.take_pending(77)

    assert [it["content"] for it in taken] == ["进度上报"]

"""子代理「等答复」不设上限 + 完成通知不覆盖主代理文本（plan-565-2323 G16）。

两个缺陷的原貌：

**A. 等待被误杀。** 子代理 `report_to_leader(kind="question", wait=true)` 之后，旧实现
`wait_reply` 用 `wait_for(..., timeout=600)`，而停滞看门狗阈值同样是 600s。两者同时到点，
看门狗必然先 `task.cancel()`，子代理落 `failed`「长时间无活动」——设计上「没等到答复就按
最佳判断继续」的分支永远走不到。用户口径：主代理可能正在问用户，用户可能很久才回答，
所以**等待没有时间上限**；兜底不靠时间，靠取消传播 + 会话存活判定。

**B. 完成通知覆盖主代理刚说的话。** `context_manager` 装配历史时，非 user 文本会写进
`_pending_agent_text`——那是**覆盖写**。子代理完成通知 / 后台命令完成行没有回放标记，
落进这条分支后把主代理刚说的一段话整段吞掉，模型随后以为自己说过「子代理已结束」。

本文件钉住四件事：
1. 看门狗在 `handle.waiting` 为真时**永不**判停滞（等再久也不杀）；
2. `wait_reply` 缺省没有总时长上限，且存活判定失效 / 取消时立刻结束并清场；
3. `handle.waiting` 在收到答复 / 取消 / 异常三条出口上都被复位；
4. 系统事件落库带标记，装配历史时按 user 侧回放或跳过，绝不复用 `_pending_agent_text`。

全部用例不依赖真实时间等待：用假的 `alive` 回调、注入小时级 `timeout_s`、或直接把
`last_activity_at` 拨到很久以前。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestration import mailbox, subagent_comm  # noqa: E402
from app.orchestration.context_manager import (  # noqa: E402
    HISTORY_ROLE_AGENT_TEXT,
    HISTORY_ROLE_DISPLAY_ONLY,
    HISTORY_ROLE_SYSTEM_INPUT,
    HISTORY_ROLE_USER_INPUT,
    resolved_history_role,
)
from app.orchestration.subagent import SubagentHandle, _stall_verdict  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    subagent_comm.reset_for_tests()
    mailbox.reset_for_tests()
    yield
    subagent_comm.reset_for_tests()
    mailbox.reset_for_tests()


# ── 1. 看门狗：等答复期间永不判停滞 ────────────────────────────────────────────

def test_stall_verdict_never_kills_waiting_handle():
    """plan-565-2323 G16：`handle.waiting` 为真 → 无论多久都只算「正常等待」。

    旧实现虽已豁免 `waiting`，但等答复这条路从不置真（只有瞬时故障保活会），
    于是看门狗照常按 600s 杀。这里把 `last_activity_at` 拨到一小时前，确认仍不判停滞。
    """
    h = SubagentHandle(agent_id=1, status="running")
    h.waiting = True
    h.last_activity_at = time.monotonic() - 3600

    assert _stall_verdict(h, timeout=600.0) == "waiting"


def test_stall_verdict_still_detects_real_stall():
    """对照组：不处于等待时，超过阈值就该判停滞——修复不能把看门狗整体关掉。"""
    h = SubagentHandle(agent_id=2, status="running")
    h.waiting = False
    h.last_activity_at = time.monotonic() - 3600

    assert _stall_verdict(h, timeout=600.0) == "stall"


def test_stall_verdict_idle_within_timeout():
    h = SubagentHandle(agent_id=3, status="running")
    h.last_activity_at = time.monotonic() - 10

    assert _stall_verdict(h, timeout=600.0) == "idle"


# ── 2. wait_reply：缺省无上限，靠 alive / 取消兜底 ─────────────────────────────

@pytest.mark.asyncio
async def test_wait_reply_has_no_default_timeout(monkeypatch):
    """plan-565-2323 G16：不传 timeout_s 时，等待不得被任何默认值掐断。

    做法：把 `_ALIVE_POLL_S` 调到很小，用一个「永不失效」的 alive 回调跑若干轮采样，
    确认等待仍在挂着（若还留着旧的 600s 默认值，这里也不会通过——因为旧实现会把它
    当成 wait_for 的硬超时，而本用例显式不传，走的是无上限分支）。
    """
    monkeypatch.setattr(subagent_comm, "_ALIVE_POLL_S", 0.01)
    qid = await subagent_comm.publish_question(
        session_id=1, agent_id=7, task_title="T", message="要改 A 还是 B？",
    )
    waiter = asyncio.ensure_future(
        subagent_comm.wait_reply(qid, alive=lambda: True),
    )
    # 跑够多个采样周期，确认它没有因为「超时」而提前返回
    await asyncio.sleep(0.1)
    assert not waiter.done(), "缺省等待不得有上限，不应自行结束"

    # 答复后立刻返回，值原样带回
    assert subagent_comm.resolve_question(qid, "改 A") is True
    assert await asyncio.wait_for(waiter, timeout=5) == "改 A"


@pytest.mark.asyncio
async def test_wait_reply_ends_when_session_gone(monkeypatch):
    """会话/管理器已结束（alive 返回 False）→ 立刻返回 None 并清场，不悬挂成僵尸。"""
    monkeypatch.setattr(subagent_comm, "_ALIVE_POLL_S", 0.01)
    qid = await subagent_comm.publish_question(
        session_id=2, agent_id=8, task_title="T", message="还在吗？",
    )
    started = time.monotonic()
    reply = await asyncio.wait_for(
        subagent_comm.wait_reply(qid, alive=lambda: False), timeout=5,
    )
    elapsed = time.monotonic() - started

    assert reply is None
    assert elapsed < 5
    assert subagent_comm.pending_question_of(2, 8) is None, "兜底结束后必须清掉登记"


@pytest.mark.asyncio
async def test_wait_reply_cancel_propagates_and_cleans_up():
    """显式终止（右面板「终止」→ task.cancel()）必须能打断等待：CancelledError 正常上传。"""
    qid = await subagent_comm.publish_question(
        session_id=3, agent_id=9, task_title="T", message="这条会被取消",
    )
    waiter = asyncio.ensure_future(subagent_comm.wait_reply(qid))
    await asyncio.sleep(0)
    assert not waiter.done()

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert subagent_comm.pending_question_of(3, 9) is None, "取消路径不得留下残留登记"


@pytest.mark.asyncio
async def test_explicit_limit_is_still_honoured():
    """模型自己显式给了 timeout_s 时，仍按该上限返回 None（保留既有「限时提问」语义）。"""
    qid = await subagent_comm.publish_question(
        session_id=4, agent_id=10, task_title="T", message="限时提问",
    )
    assert await subagent_comm.wait_reply(qid, 1.0) is None
    assert subagent_comm.pending_question_of(4, 10) is None


@pytest.mark.asyncio
async def test_question_without_wait_is_discarded():
    """wait=false 的提问不登记等待：答复走普通指令注入通道，不能凭空丢失。"""
    qid = await subagent_comm.publish_question(
        session_id=5, agent_id=11, task_title="T", message="顺口一问",
    )
    subagent_comm.discard_question(qid)
    assert subagent_comm.resolve_question(qid, "答复") is False


# ── 3. handle.waiting 在所有出口复位 ──────────────────────────────────────────

class _StubManager:
    """只为 `_run_report_to_leader` 提供 handle 的最小 manager 桩。

    `session_id=0` 让 `_question_waiter_alive` 退回「不做存活判定」（读不到状态就不误杀），
    于是用例只依赖回复/取消本身，不牵扯真实 manager 注册表。
    """

    def __init__(self, handle: SubagentHandle) -> None:
        self.session_id = 0
        self._handle = handle
        self.owner_active = True
        self.leader_notes: list[tuple[int, str, str]] = []

    def get(self, agent_id: int):
        return self._handle if agent_id == self._handle.agent_id else None

    def notify_leader(self, agent_id: int, message: str, kind: str) -> None:
        self.leader_notes.append((agent_id, message, kind))


def _msg(*, text: str, sender_type: str, msg_type: str = "text", content: dict | None = None):
    """构造满足装配判定读取口径的最小消息对象。"""
    from types import SimpleNamespace

    payload = {"text": text} if content is None else content
    return SimpleNamespace(
        id=1, sender_type=sender_type, msg_type=msg_type, content=payload,
    )


async def _report(comm: dict, args: dict) -> str:
    from app.orchestration import agent_loop as agent_loop_mod

    return await agent_loop_mod._run_report_to_leader(
        comm, args, session_id=1, turn_id=1, thread_id=1, agent_id=1,
        agent_name="sub", db=None,
    )


@pytest.mark.asyncio
async def test_waiting_reset_after_reply(monkeypatch):
    """收到答复这条出口：`handle.waiting` 必须复位。"""
    monkeypatch.setattr(subagent_comm, "_ALIVE_POLL_S", 0.01)
    handle = SubagentHandle(agent_id=1, status="running")
    mgr = _StubManager(handle)
    comm = {"manager": mgr, "agent_id": 1, "task_title": "T"}

    async def _resolve_soon() -> None:
        # 等到 waiting 置真后立刻答复
        for _ in range(200):
            if handle.waiting:
                break
            await asyncio.sleep(0.005)
        qid = subagent_comm.pending_question_of(1, 1)
        assert qid, "提问必须先登记，否则答复无处配对"
        assert subagent_comm.resolve_question(qid, "改 A") is True

    task = asyncio.ensure_future(_resolve_soon())
    out = await _report(comm, {"message": "A 还是 B？", "kind": "question", "wait": True})
    await task

    assert "改 A" in out
    assert handle.waiting is False, "收到答复后 waiting 必须复位"


@pytest.mark.asyncio
async def test_waiting_reset_after_cancel(monkeypatch):
    """取消这条出口：`handle.waiting` 同样必须复位（否则看门狗被永久豁免）。"""
    monkeypatch.setattr(subagent_comm, "_ALIVE_POLL_S", 0.01)
    handle = SubagentHandle(agent_id=1, status="running")
    mgr = _StubManager(handle)
    comm = {"manager": mgr, "agent_id": 1, "task_title": "T"}

    task = asyncio.ensure_future(
        _report(comm, {"message": "A 还是 B？", "kind": "question", "wait": True}),
    )
    for _ in range(200):
        if handle.waiting:
            break
        await asyncio.sleep(0.005)
    assert handle.waiting is True, "等待期间必须置真，看门狗才会豁免"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handle.waiting is False, "取消路径必须复位 waiting"


@pytest.mark.asyncio
async def test_waiting_reset_after_session_gone(monkeypatch):
    """会话结束这条出口（alive 判定失效 → wait_reply 返回 None）：`handle.waiting` 必须复位。

    这里用**真实的** `SubagentManager` 注册表，让 `_question_waiter_alive` 真正参与判定：
    先让它判定存活（等待挂着），再把 `owner_active` 置假模拟 turn 收尾，
    确认等待立刻结束且 `finally` 复位了 `waiting`。
    """
    from app.orchestration.subagent import cleanup, get_subagent_manager

    monkeypatch.setattr(subagent_comm, "_ALIVE_POLL_S", 0.01)

    sid = 987654
    mgr = get_subagent_manager(sid)
    handle = SubagentHandle(agent_id=1, status="running")
    handle.task = None  # 非 asyncio.Task：跳过 done 检查，只依赖 status / owner_active
    mgr._handles[1] = handle
    mgr.owner_active = True
    try:
        comm = {"manager": mgr, "agent_id": 1, "task_title": "T"}
        task = asyncio.ensure_future(
            _report(comm, {"message": "A 还是 B？", "kind": "question", "wait": True}),
        )
        for _ in range(200):
            if handle.waiting:
                break
            await asyncio.sleep(0.005)
        assert handle.waiting is True

        # 模拟 turn 收尾：存活判定从此为假
        mgr.owner_active = False
        out = await asyncio.wait_for(task, timeout=10)

        assert "No reply from the main agent" in out
        assert handle.waiting is False, "会话结束出口必须复位 waiting"
    finally:
        cleanup(sid)


# ── 4. 系统事件不覆盖主代理文本 ───────────────────────────────────────────────

def test_classify_persisted_covers_defect_kinds():
    """落库标记 / mailbox.kind 两条判据都要能认出缺陷涉及的几类事件。

    老行没有新标记，只有 `mailbox.kind`——它必须同样判对，否则历史数据里的问题照旧。
    """
    # 带标记（本改动之后落库的新行）
    assert mailbox.classify_persisted({mailbox.SYSTEM_EVENT_FLAG: True}) == "system_event"
    assert mailbox.classify_persisted({mailbox._DISPLAY_ONLY_FLAG: True}) == "display_only"
    # 只有 mailbox.kind（本改动之前落库的老行，兜底判据）
    for kind in (mailbox.MailKind.BG_COMMAND_DONE.value,
                 mailbox.MailKind.BG_COMMAND_STOPPED.value):
        assert mailbox.classify_persisted({"mailbox": {"kind": kind}}) == "system_event"
    assert mailbox.classify_persisted(
        {"mailbox": {"kind": mailbox.MailKind.SUBAGENT_COMPLETION.value}}
    ) == "display_only"
    # 普通消息不参与判定
    assert mailbox.classify_persisted({"text": "普通系统提示"}) == ""
    assert mailbox.classify_persisted(None) == ""


def test_resolved_history_role_never_routes_system_events_to_agent_text():
    """系统事件 / 完成通知绝不能落到 agent_text（那条分支是覆盖写，会吞掉主代理的话）。"""
    for kind in (mailbox.MailKind.BG_COMMAND_DONE.value,
                 mailbox.MailKind.BG_COMMAND_STOPPED.value,
                 mailbox.MailKind.SUBAGENT_QUESTION.value,
                 mailbox.MailKind.SUBAGENT_REPORT.value):
        role = resolved_history_role(
            sender_type="system", content={"mailbox": {"kind": kind}},
            replay_kind=mailbox.classify_persisted({"mailbox": {"kind": kind}}),
        )
        assert role == HISTORY_ROLE_SYSTEM_INPUT, f"{kind} 必须按 user 侧注入回放"

    completion = mailbox.MailKind.SUBAGENT_COMPLETION.value
    completion_role = resolved_history_role(
        sender_type="system", content={"mailbox": {"kind": completion}},
        replay_kind=mailbox.classify_persisted({"mailbox": {"kind": completion}}),
    )
    assert completion_role == HISTORY_ROLE_DISPLAY_ONLY, "完成通知不进模型上下文"


def test_resolved_history_role_keeps_other_system_text_as_before():
    """其余 system 文本（模型切换分隔线等）保持改前行为：仍归 agent_text。

    一刀切会把「模型已切换」这类提示也变成 user 输入，改变既有语义；
    本改动的范围只收敛缺陷涉及的三类事件。
    """
    role = resolved_history_role(
        sender_type="system", content={"text": "模型已切换为 X"}, replay_kind="",
    )
    assert role == HISTORY_ROLE_AGENT_TEXT


def test_resolved_history_role_user_and_legacy_report_flag():
    assert resolved_history_role(
        sender_type="user", content={"text": "hi"}, replay_kind="",
    ) == HISTORY_ROLE_USER_INPUT
    # plan-156-620 的历史遗留标记：子会话汇报仍按 user 侧回放
    role = resolved_history_role(
        sender_type="system",
        content={"text": "汇报", "subsession_report": True}, replay_kind="",
    )
    assert role == HISTORY_ROLE_SYSTEM_INPUT


@pytest.mark.asyncio
async def test_publish_marks_system_events_for_replay():
    """落库那一刻就要打标记：只改装配侧等于把判定依赖在「有没有 mailbox.kind」上。

    用 `persist=False` 无法验证标记，所以这里替换 `message_service.create_message` 抓参数，
    避免依赖真实库（也能覆盖三种 kind 的标记分支）。
    """
    captured: list[dict] = []

    class _Row:
        id = 1

    async def _fake_create(db, **kwargs):
        captured.append(kwargs)
        return _Row()

    from app.services import message_service

    orig = message_service.create_message
    message_service.create_message = _fake_create
    try:
        for kind in (mailbox.MailKind.BG_COMMAND_DONE,
                     mailbox.MailKind.BG_COMMAND_STOPPED,
                     mailbox.MailKind.SUBAGENT_COMPLETION,
                     mailbox.MailKind.SUBAGENT_QUESTION):
            captured.clear()
            await mailbox.publish(kind, session_id=9, text="事件正文")
            content = captured[0]["content"]
            if kind is mailbox.MailKind.SUBAGENT_COMPLETION:
                assert content.get(mailbox._DISPLAY_ONLY_FLAG) is True, "完成通知必须标纯展示"
                assert content.get(mailbox.SYSTEM_EVENT_FLAG) is None
            elif kind is mailbox.MailKind.SUBAGENT_QUESTION:
                assert content.get("subsession_report") is True, "提问仍带走原有的回放标记"
                assert content.get(mailbox.SYSTEM_EVENT_FLAG) is None
            else:
                assert content.get(mailbox.SYSTEM_EVENT_FLAG) is True, "后台命令事件必须是系统事件"
                assert content.get(mailbox._DISPLAY_ONLY_FLAG) is None
    finally:
        message_service.create_message = orig


def test_defect_messages_do_not_share_agent_text_bucket():
    """把缺陷现场还原一遍：主代理说完话、紧接一条系统事件——两者不得共用同一个桶。

    这里直接断言归属类别不同：`agent_text` 是覆盖写的那个桶，系统事件必须不进去。
    """
    agent_line = _msg(text="我已经把 A 改好了，接下来跑测试。", sender_type="agent")
    done_line = _msg(
        text="【后台命令完成】pytest 退出码 0", sender_type="system",
        content={"text": "【后台命令完成】pytest 退出码 0",
                 "mailbox": {"kind": mailbox.MailKind.BG_COMMAND_DONE.value}},
    )
    done_kind = mailbox.classify_persisted(done_line.content)

    assert resolved_history_role(
        sender_type=agent_line.sender_type, content=agent_line.content, replay_kind="",
    ) == HISTORY_ROLE_AGENT_TEXT
    assert resolved_history_role(
        sender_type=done_line.sender_type, content=done_line.content, replay_kind=done_kind,
    ) != HISTORY_ROLE_AGENT_TEXT

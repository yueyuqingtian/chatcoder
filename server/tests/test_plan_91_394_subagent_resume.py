"""plan-91-394：子代理断点续跑 / 保活重试 / 主代理中断暂停 / 续做清单。

用假的 run_agent_loop 驱动 SubagentManager._drive，避免依赖真实模型与数据库。
"""
import asyncio

import pytest

from app.models.schemas import ChatMessage
from app.orchestration import subagent as subagent_mod
from app.orchestration.agent_loop import AgentOutput, _is_transient_failure_text
from app.orchestration.context_manager import _build_paused_subagents_block
from app.orchestration.subagent import SubagentHandle, SubagentManager, _checkpoint_payload, _sanitize_messages


# ── 公共桩 ──────────────────────────────────────────


class _FakeDbSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, pk):
        return None


class _FakeFactory:
    def __call__(self):
        return _FakeDbSession()


class _FakeWriteBuffer:
    async def flush(self):
        return None


class _FakeWriteBehind:
    def get(self, session_id, turn_id):
        return _FakeWriteBuffer()


@pytest.fixture
def stubs(monkeypatch):
    """替换所有落库/广播副作用，并记录子代理状态落库调用。"""
    persisted: list[tuple[int, str, dict | None]] = []

    async def _persist(agent_id, status, checkpoint):
        persisted.append((agent_id, status, checkpoint))

    async def _noop(*args, **kwargs):
        return None

    async def _no_wait(cancel_event, seconds=None):
        return True

    monkeypatch.setattr(subagent_mod, "async_session_factory", _FakeFactory())
    monkeypatch.setattr(subagent_mod, "_persist_agent_state", _persist)
    monkeypatch.setattr(subagent_mod, "_sync_task", _noop)
    monkeypatch.setattr(subagent_mod, "_broadcast_subagent_failed", _noop)
    monkeypatch.setattr(subagent_mod, "_wait_retry_interval", _no_wait)
    monkeypatch.setattr(subagent_mod, "_retry_wait_seconds", lambda attempt: 0.0)
    monkeypatch.setattr("app.persistence.write_behind.write_behind", _FakeWriteBehind())
    return persisted


def _base_messages() -> list[ChatMessage]:
    return [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="请调研 X")]


def _new_handle(agent_id: int = 7) -> SubagentHandle:
    return SubagentHandle(agent_id=agent_id, turn_id=3, task_title="调研 X",
                          run_spec={"workspace": "", "tool_names": ["fs_read"]}, tool_schemas=[])


# ── 瞬时故障判定 ─────────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    ("模型响应因网关空闲超时中断（已自动重试 3 次仍失败）", True),
    ("模型思考超时，未生成任何内容", True),
    ("模型返回空响应 (finish_reason=stop)，未生成内容或工具调用", True),
    ("网关响应异常 (finish_reason=tool_calls 但无工具调用数据，流丢帧)", True),
    ("Connection reset by peer", True),
    ("输出达到 token 上限且未生成正文（推理可能已耗尽输出预算）", False),
    ("模型仅生成思考未输出正文 (finish_reason=stop)，任务可能未完成", False),
    ("401 Unauthorized: invalid api key", False),
    ("model not found", False),
])
def test_transient_classification(text, expected):
    assert _is_transient_failure_text(text) is expected


# ── 保活重试：续上已完成的进度 ─────────────────────────────


def test_transient_failure_keeps_progress_and_recovers(monkeypatch, stubs):
    monkeypatch.setattr(subagent_mod, "_retry_plan", lambda: [10.0])
    calls: list[list[ChatMessage]] = []

    async def fake_run(db, **kwargs):
        calls.append(list(kwargs["context_messages"]))
        messages = list(kwargs["context_messages"])
        kwargs["checkpoint_sink"](messages)
        if len(calls) == 1:
            # 第一次：已完成一步工具调用，随后网关中断
            messages.append(ChatMessage(role="assistant", content=None,
                                        tool_calls=[{"id": "c1", "type": "function",
                                                     "function": {"name": "fs_read", "arguments": "{}"}}]))
            messages.append(ChatMessage(role="tool", tool_call_id="c1", content="文件内容"))
            return AgentOutput(kind="error", error="模型响应因网关空闲超时中断", transient=True)
        return AgentOutput(kind="message", text="## 结果\n已完成调研")

    monkeypatch.setattr(subagent_mod, "run_agent_loop", fake_run)
    mgr = SubagentManager(session_id=1)
    mgr.owner_active = True
    handle = _new_handle()
    asyncio.run(mgr._drive(handle, agent_id=7, task_id=None, turn_id=3,
                           messages=_base_messages(), cancel_event=None))

    assert handle.status == "done"
    assert len(calls) == 2
    # 第二次调用从检查点续跑：第一步的工具结果仍在上下文里，未从头开始
    roles = [m.role for m in calls[1]]
    assert "tool" in roles
    assert any(m.tool_call_id == "c1" for m in calls[1] if m.role == "tool")
    # 成功后检查点清空
    assert stubs[-1][1] == "done" and stubs[-1][2] is None


def test_transient_without_owner_turn_pauses_instead_of_keepalive(monkeypatch, stubs):
    monkeypatch.setattr(subagent_mod, "_retry_plan", lambda: [10.0])
    calls = []

    async def fake_run(db, **kwargs):
        calls.append(1)
        kwargs["checkpoint_sink"](list(kwargs["context_messages"]))
        return AgentOutput(kind="error", error="模型响应因网关空闲超时中断", transient=True)

    monkeypatch.setattr(subagent_mod, "run_agent_loop", fake_run)
    mgr = SubagentManager(session_id=1)  # owner_active 默认 False：主代理 turn 已不存活
    handle = _new_handle()
    asyncio.run(mgr._drive(handle, agent_id=7, task_id=None, turn_id=3,
                           messages=_base_messages(), cancel_event=None))

    assert len(calls) == 1  # 不保活、不重复
    assert handle.status == "paused" and handle.resumable is True
    assert stubs[-1][1] == "paused"
    assert stubs[-1][2]["messages"], "暂停时必须保存检查点"


def test_non_transient_failure_is_failed_but_resumable(monkeypatch, stubs):
    async def fake_run(db, **kwargs):
        kwargs["checkpoint_sink"](list(kwargs["context_messages"]))
        return AgentOutput(kind="error", error="401 Unauthorized: invalid api key", transient=False)

    monkeypatch.setattr(subagent_mod, "run_agent_loop", fake_run)
    mgr = SubagentManager(session_id=1)
    mgr.owner_active = True
    handle = _new_handle()
    asyncio.run(mgr._drive(handle, agent_id=7, task_id=None, turn_id=3,
                           messages=_base_messages(), cancel_event=None))

    assert handle.status == "failed"
    assert handle.resumable is True
    assert stubs[-1][1] == "failed" and stubs[-1][2]["messages"]


# ── 主代理中断：暂停并保存检查点，不唤醒主代理 ─────────────────


def test_interrupt_pauses_with_checkpoint_and_does_not_notify(monkeypatch, stubs):
    notified = []

    async def fake_run(db, **kwargs):
        messages = list(kwargs["context_messages"])
        kwargs["checkpoint_sink"](messages)
        messages.append(ChatMessage(role="assistant", content="已读取 3 个文件"))
        raise asyncio.CancelledError()  # 模拟用户停止：主代理取消子代理任务

    monkeypatch.setattr(subagent_mod, "run_agent_loop", fake_run)
    mgr = SubagentManager(session_id=1)
    mgr.owner_active = True
    mgr.set_finish_notify(lambda h: notified.append(h))
    handle = _new_handle()
    asyncio.run(mgr._drive(handle, agent_id=7, task_id=None, turn_id=3,
                           messages=_base_messages(), cancel_event=None))

    assert handle.status == "paused"
    assert notified == [], "暂停不得唤醒主代理（由用户“继续”决定）"
    agent_id, status, checkpoint = stubs[-1]
    assert status == "paused"
    assert checkpoint["messages"][-1]["content"] == "已读取 3 个文件"


def test_explicit_cancel_is_terminal_and_clears_checkpoint(monkeypatch, stubs):
    async def fake_run(db, **kwargs):
        kwargs["checkpoint_sink"](list(kwargs["context_messages"]))
        raise asyncio.CancelledError()

    monkeypatch.setattr(subagent_mod, "run_agent_loop", fake_run)
    mgr = SubagentManager(session_id=1)
    mgr.owner_active = True
    handle = _new_handle()
    handle.cancel_requested = True  # cancel_subagent 工具路径
    asyncio.run(mgr._drive(handle, agent_id=7, task_id=None, turn_id=3,
                           messages=_base_messages(), cancel_event=None))

    assert handle.status == "cancelled"
    assert stubs[-1] == (7, "cancelled", None)


# ── 暂停范围：正常结束只暂停保活中的，中断则全部暂停 ─────────────


def test_pause_scope_on_turn_end():
    async def scenario():
        mgr = SubagentManager(session_id=1)
        mgr.owner_active = True

        running = SubagentHandle(agent_id=1)
        running.task = asyncio.create_task(asyncio.sleep(60))
        waiting = SubagentHandle(agent_id=2)
        waiting.waiting = True
        waiting.task = asyncio.create_task(asyncio.sleep(60))
        mgr._handles = {1: running, 2: waiting}

        # 正常结束：仅保活等待中的被暂停，正常运行者继续
        assert mgr.pause_for_turn_end(interrupted=False) == 1
        await asyncio.gather(waiting.task, return_exceptions=True)
        assert waiting.task.cancelled()
        assert running.task.done() is False
        assert mgr.owner_active is False

        # 主代理中断：运行中的全部暂停
        mgr.owner_active = True
        assert mgr.pause_for_turn_end(interrupted=True) == 1
        await asyncio.gather(running.task, return_exceptions=True)
        assert running.task.cancelled()
        assert running.pause_reason == "主代理已中断"

    asyncio.run(scenario())


def test_resume_refuses_writable_subagent_in_readonly_mode(monkeypatch):
    """只读/计划模式不得借续跑恢复可写子代理（沿用原 subagent_mode=agent）。"""

    class _Row:
        session_id = 1
        kind = "sub"
        name = "改代码"
        status = "paused"
        checkpoint_json = {"messages": [{"role": "user", "content": "x"}],
                           "spec": {"subagent_mode": "agent", "task_title": "改代码"}}

    class _Session(_FakeDbSession):
        async def get(self, model, pk):
            return _Row()

    class _Factory:
        def __call__(self):
            return _Session()

    monkeypatch.setattr(subagent_mod, "async_session_factory", _Factory())
    state, detail = asyncio.run(subagent_mod.resume_subagent(1, 7, turn_id=5, readonly_only=True))
    assert state == "not_resumable"
    assert "只读" in detail


# ── 续做清单与提示词 ────────────────────────────────────────


def test_paused_block_lists_resumable_subagents_with_rule():
    block = _build_paused_subagents_block([
        {"agent_id": 140, "title": "调研右侧面板", "steps": 12,
         "reason": "网关空闲超时", "saved_at": "2026-10-12T01:23:45+00:00"},
    ])
    assert "## Paused Subagents" in block
    assert "agent_id=140" in block and "已完成 12 步" in block
    assert "resume_subagent(agent_id=" in block
    assert "严禁用 spawn_subagent 重新派发" in block


def test_main_prompt_contains_resume_rule_and_is_stripped_without_subagents():
    from app.orchestration.prompts.main import build_main_system_prompt

    with_sub = build_main_system_prompt(enable_subagents=True)
    assert "Resuming interrupted subagents" in with_sub
    assert "resume_subagent" in with_sub
    without_sub = build_main_system_prompt(enable_subagents=False)
    assert "Resuming interrupted subagents" not in without_sub


# ── 检查点工具配对与序列化 ──────────────────────────────────


def test_sanitize_repairs_dangling_tool_calls():
    dangling = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="任务"),
        ChatMessage(role="assistant", content=None,
                    tool_calls=[{"id": "x1", "type": "function",
                                 "function": {"name": "fs_read", "arguments": "{}"}}]),
    ]
    fixed = _sanitize_messages(dangling)
    for i, m in enumerate(fixed):
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                assert any(n.role == "tool" and n.tool_call_id == tc["id"] for n in fixed[i + 1:])


def test_checkpoint_payload_roundtrips_to_chat_messages():
    handle = _new_handle()
    handle.live_messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content=None,
                    tool_calls=[{"id": "c9", "type": "function",
                                 "function": {"name": "fs_read", "arguments": "{}"}}]),
        ChatMessage(role="tool", tool_call_id="c9", content="ok"),
    ]
    payload = _checkpoint_payload(handle, {"task_title": "t"}, "网关超时")
    restored = [ChatMessage(**m) for m in payload["messages"]]
    assert [m.role for m in restored] == ["user", "assistant", "tool"]
    assert restored[1].tool_calls[0]["id"] == "c9"
    assert payload["reason"] == "网关超时"

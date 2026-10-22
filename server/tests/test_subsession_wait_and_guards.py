"""0.9.1 走查补充：子会话 / 子代理等待可让出、提问登记撤销、官方输出上限自学、读缓存写入登记。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.providers import anthropic as anthropic_mod
from app.models.providers.anthropic import AnthropicProvider, _degrade_thinking_payload
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration import engine
from app.orchestration.output_paging import present_output_bytes
from app.orchestration.subsession_tool_runner import _YIELD_NOTE, _yielded
from app.orchestration.tools.read_cache import ReadWindowCache
from app.services import subsession_service


# ───────────────── 子会话等待：有新消息即让出 ─────────────────


async def test_wait_until_idle_returns_immediately_when_interrupted():
    """interrupt_check 为真时不进入轮询（不触库、不睡眠），否则会和等答复的子会话互相等到超时。"""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await subsession_service._wait_until_idle([1, 2, 3], 300.0, lambda: True)
    assert loop.time() - t0 < 1.0


def test_yielded_requires_running_subsession():
    running = SimpleNamespace(status="running")
    done = SimpleNamespace(status="done")
    assert _yielded([running, done], lambda: True) is True
    assert _yielded([done], lambda: True) is False  # 全部已结束：不是让出，是正常等完
    assert _yielded([running], lambda: False) is False
    assert _yielded([running], None) is False
    assert "已让出等待" in _YIELD_NOTE


def test_has_injected_inputs_reflects_queue():
    turn = 987_654_321
    engine._pending_inputs.pop(turn, None)
    assert engine.has_injected_inputs(turn) is False
    engine._pending_inputs.setdefault(turn, []).append({"content": "x"})
    try:
        assert engine.has_injected_inputs(turn) is True
    finally:
        engine._pending_inputs.pop(turn, None)


# ───────────────── 提问登记：不等待的提问不留登记 ─────────────────


def test_discard_question_prevents_swallowed_reply():
    qid = subsession_service.new_question_id(5)
    subsession_service.register_question(qid)
    subsession_service.discard_question(qid)
    # 撤销后答复走普通消息送达，而不是被一个没有等待方的登记吞掉
    assert subsession_service.resolve_question(qid, "答复") is False
    subsession_service.discard_question(None)  # 容错


# ───────────────── Anthropic：官方 128K 兜底遇到型号上限时自学 ─────────────────


def test_degrade_learns_model_output_cap(monkeypatch):
    monkeypatch.setattr(anthropic_mod, "_LEARNED_OUTPUT_CAPS", {})
    payload = {
        "model": "claude-sonnet-4-x", "max_tokens": 128_000,
        "thinking": {"type": "enabled", "budget_tokens": 70_000},
    }
    msg = "max_tokens: 128000 > 64000, which is the maximum allowed number of output tokens for claude-sonnet-4-x"
    degraded = _degrade_thinking_payload(payload, msg)
    assert degraded is not None
    assert degraded["max_tokens"] == 64_000
    assert degraded["thinking"]["budget_tokens"] < 64_000  # 思考预算必须小于 max_tokens
    assert anthropic_mod._LEARNED_OUTPUT_CAPS["claude-sonnet-4-x"] == 64_000
    # 再次遇到同一报错、已无可收紧空间时不再重试，避免死循环
    assert _degrade_thinking_payload(degraded, msg) is None


def test_learned_cap_applied_to_next_request(monkeypatch):
    monkeypatch.setattr(anthropic_mod, "_LEARNED_OUTPUT_CAPS", {"claude-sonnet-4-x": 64_000})
    provider = AnthropicProvider(api_key="k", base_url="https://api.anthropic.com/v1", model="claude-sonnet-4-x")
    req = ChatRequest(model="claude-sonnet-4-x", messages=[ChatMessage(role="user", content="hi")])
    assert provider._build_payload(req, stream=True)["max_tokens"] == 64_000
    # 其它型号不受影响，仍是统一兜底
    other = ChatRequest(model="claude-opus-5-5", messages=[ChatMessage(role="user", content="hi")])
    assert provider._build_payload(other, stream=True)["max_tokens"] == 128_000


# ───────────────── 读缓存：自己写过的文件可整体重写 ─────────────────


def test_read_cache_mark_known_allows_rewrite(tmp_path):
    cache = ReadWindowCache()
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    assert cache.has_read(target) is False
    cache.mark_known(target)
    assert cache.has_read(target) is True


# ───────────────── 字节预算呈现：二次呈现不改写续读指引 ─────────────────


def test_present_output_bytes_is_idempotent_for_same_budget(tmp_path, monkeypatch):
    """终端工具先呈现一次、agent_loop 再按同一预算呈现一次：第二次必须原样返回。"""
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path), raising=False)
    text = "错误" * 30000
    first = present_output_bytes(text, 30720, session_id=1, tag="terminal", tail_bytes=22528)
    second = present_output_bytes(first, 30720, session_id=1, tag="terminal", tail_bytes=22528)
    assert second == first


async def test_wait_for_question_reply_yields_to_new_message():
    """子会话等答复期间收到新消息：放弃等待并撤销登记，主代理之后的答复走普通消息。"""
    qid = subsession_service.new_question_id(9)
    subsession_service.register_question(qid)
    flag = {"v": False}

    async def _flip():
        await asyncio.sleep(0.2)
        flag["v"] = True

    flipper = asyncio.create_task(_flip())
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    reply = await subsession_service.wait_for_question_reply(qid, 30.0, lambda: flag["v"])
    await flipper
    assert reply is None
    assert loop.time() - t0 < 5.0
    assert subsession_service.resolve_question(qid, "晚到的答复") is False


async def test_wait_for_question_reply_still_returns_reply():
    qid = subsession_service.new_question_id(10)
    subsession_service.register_question(qid)

    async def _answer():
        await asyncio.sleep(0.1)
        subsession_service.resolve_question(qid, "好的")

    answerer = asyncio.create_task(_answer())
    reply = await subsession_service.wait_for_question_reply(qid, 30.0, lambda: False)
    await answerer
    assert reply == "好的"


class _FakeManager:
    """只提供等待逻辑用到的接口：一个永远在跑的子代理。"""

    def pending_count(self) -> int:
        return 1

    async def wait_one(self, timeout=None) -> bool:
        await asyncio.sleep(timeout or 0.01)
        return False

    def drain_completions(self) -> list[str]:
        return []


async def test_await_background_subagents_yields_on_injected_input():
    """主代理收工等待后台子代理时，队列里有新消息（子代理提问 / 用户追加）要立刻让出，而不是等到子代理结束。"""
    from app.orchestration import agent_loop

    turn = 987_654_322
    engine._pending_inputs[turn] = [{"content": "【子代理提问 subagent#1】要不要加索引？"}]
    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        notes = await agent_loop._await_background_subagents(
            _FakeManager(), session_id=1, turn_id=turn, thread_id=None, cancel_event=None,
        )
        assert notes == [agent_loop._WAIT_WOKEN_NOTE]
        assert loop.time() - t0 < 3.0
    finally:
        engine._pending_inputs.pop(turn, None)


async def test_subagent_pending_count_ignores_dead_handles():
    from app.orchestration.subagent import SubagentHandle, SubagentManager

    mgr = SubagentManager(session_id=77)

    async def _noop():
        return None

    finished = asyncio.ensure_future(_noop())
    await finished
    mgr._handles[1] = SubagentHandle(agent_id=1, status="running", task=finished)  # 任务已结束却仍标 running
    mgr._handles[2] = SubagentHandle(agent_id=2, status="running")  # 尚未挂任务：按运行中算
    assert mgr.pending_count() == 1
    # 无可等任务时也要让出事件循环，调用方 while 循环才不会空转
    assert await mgr.wait_one(timeout=0.05) is True


def test_urgent_only_ignores_progress_reports():
    """兄弟子会话的进度 / 完成汇报（priority=2）不打断等待；提问、用户追加（无 priority）才打断。"""
    turn = 987_654_323
    engine._pending_inputs.pop(turn, None)
    try:
        engine._pending_inputs[turn] = [{"content": "进度", "priority": 2}]
        assert engine.has_injected_inputs(turn) is True
        assert engine.has_injected_inputs(turn, urgent_only=True) is False
        engine._pending_inputs[turn].append({"content": "提问", "priority": 0})
        assert engine.has_injected_inputs(turn, urgent_only=True) is True
        engine._pending_inputs[turn] = [{"content": "用户追加一句"}]
        assert engine.has_injected_inputs(turn, urgent_only=True) is True
    finally:
        engine._pending_inputs.pop(turn, None)

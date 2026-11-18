"""plan-20261109-compact-structure：真实压缩、重建和最终出站的回归。"""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.models.schemas import ChatMessage as M, ChatRequest, ChatResponse, Usage
from app.orchestration.checkpoint_projection import (
    bind_runtime_sources, load_checkpoints, project_checkpoints,
)
from app.orchestration.context_manager import ContextBundle
from app.orchestration.history_sequence import HistorySequenceError, structure_diagnostic
from app.orchestration.prompts import get_checkpoint_preamble
from tests import test_compact_target as fixtures

_mk_session = fixtures._mk_session
_mk_long_history = fixtures._mk_long_history


@pytest.fixture
async def db(tmp_path):
    async for session in fixtures.db.__wrapped__(tmp_path):
        yield session


def checkpoint(key="cp-test", text="历史摘要"):
    return M(role="developer", content=get_checkpoint_preamble("zh") + "\n\n" + text,
             context_kind="checkpoint", checkpoint_id=key)


def history():
    return [M(role="system", content="规则"),
            M(role="user", content="保留原始限制", context_kind="current_task"),
            M(role="assistant", reasoning_content="真实的推理过程", tool_calls=[
                {"id": "a", "name": "fs_read", "arguments": {"path": "a.py"}},
                {"id": "b", "name": "fs_read", "arguments": {"path": "b.py"}}]),
            M(role="tool", tool_call_id="b", name="fs_read", content="文件B"),
            M(role="tool", tool_call_id="a", name="fs_read", content="文件A")]


def test_projection_uses_exact_sources_and_preserves_uncommitted_input():
    rows = history()
    rows[0].source_message_ids = []
    rows[2].source_message_ids = [2, 3]
    rows[3].source_message_ids = [4]
    rows[4].source_message_ids = [5]
    rows += [M(role="user", content="运行时的新要求", content_blocks=[
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
        skill_load_snapshots=[{"name": "x", "fingerprint": "f", "content": "规则"}])]
    before = [m.model_copy(deep=True) for m in rows]
    out = project_checkpoints(rows, [checkpoint()], [2, 3, 4, 5])
    assert not any(m.tool_calls or m.role == "tool" for m in out)
    assert any("保留原始限制" in (m.content or "") for m in out)
    user = out[-1]
    assert user.content_blocks and user.skill_load_snapshots
    assert rows == before
    assert project_checkpoints(out, [checkpoint()], [2, 3, 4, 5]) == out
    assert "source_message_ids" not in user.model_dump()


def test_unknown_and_partial_sources_are_never_silently_deleted():
    rows = history()
    rows[2].source_message_ids = [10, 11]
    out = project_checkpoints(rows, [checkpoint()], [10])
    assert sum(len(m.tool_calls or []) for m in out) == 2
    assert {m.content for m in out if m.role == "tool"} == {"文件A", "文件B"}


def test_runtime_binding_preserves_uncommitted_parallel_batch():
    rows = history()
    source = [SimpleNamespace(id=2, msg_type="tool_call", content={"call_key": "a"})]
    assert not bind_runtime_sources(rows, source)[2].source_message_ids


@pytest.mark.parametrize("trigger", ["pressure", "context-overflow", "manual"])
async def test_real_compaction_and_three_rebuilds_keep_complete_frame(db, trigger):
    from app.orchestration.context_compressor import compact_session, emergency_compact_session
    from app.orchestration.context_memory import _fetch_main_messages
    from app.orchestration.token_counter import messages_token_total
    session = await _mk_session(db)
    await _mk_long_history(db, session.id, rounds=20)
    await db.commit()
    source = await _fetch_main_messages(db, session.id, limit=None)
    used = messages_token_total(source)
    fn = emergency_compact_session if trigger == "context-overflow" else compact_session
    kwargs = {"trigger": trigger} if trigger != "context-overflow" else {}
    result = await fn(db, session=session, provider=None, context_window=max(10000, used),
                      used_tokens=used, language="zh", **kwargs)
    assert result and result["checkpoint_text"] and result["retained_ids"]
    assert result["summary_source"] == "fallback"
    await db.refresh(session)
    ctx = dict(session.shared_context)
    ctx["injected_compactions"] = [result["compaction_id"]]
    expected = None
    for _ in range(3):
        # 新数据库会话模拟下一轮与重启，不依赖 ORM 缓存。
        async with type(db)(bind=db.bind, expire_on_commit=False) as fresh:
            cps = await load_checkpoints(fresh, ctx)
            assert len(cps) == 1
            assert cps[0].content == result["checkpoint_text"]
            out = ContextBundle(system="规则", checkpoints=cps, instruction="继续").to_messages()
            assert sum(m.context_kind == "checkpoint" for m in out) == 1
            dumps = [m.model_dump() for m in out]
            assert expected is None or expected == dumps
            expected = dumps


async def test_persistent_live_projection_matches_database_range(db):
    from app.orchestration.agent_loop import _compact_persistent_or_fallback
    from app.orchestration.context_memory import _fetch_main_messages
    from app.orchestration.token_counter import messages_token_total
    session = await _mk_session(db)
    await _mk_long_history(db, session.id, rounds=20)
    await db.commit()
    source = await _fetch_main_messages(db, session.id, limit=None)
    messages = [M(role="system", content="规则")]
    for row in source:
        c = row.content
        if row.msg_type == "tool_call":
            messages.append(M(role="assistant", tool_calls=[{
                "id": c["call_key"], "name": c["tool"], "arguments": c["args"]}]))
        elif row.msg_type == "tool_result":
            messages.append(M(role="tool", tool_call_id=c["call_key"], content=c["output"]))
        else:
            messages.append(M(role="user" if row.sender_type == "user" else "assistant", content=c["text"]))
    messages.append(M(role="user", content="尚未落库的新要求", context_kind="current_task"))
    used = messages_token_total(source)
    result, live = await _compact_persistent_or_fallback(
        db, session_id=session.id, provider=None, agent_window=max(10000, used), used_tokens=used,
        agent_id=1, agent_name="主代理", turn_id=1, messages=messages, reply_language="zh")
    assert result
    live_ids = {i for m in live for i in m.source_message_ids if m.context_kind != "checkpoint"}
    assert not live_ids.intersection(result["shadowed_ids"])
    assert set(result["retained_ids"]) <= live_ids
    assert live[-1].content == "尚未落库的新要求"
    assert sum(m.context_kind == "checkpoint" for m in live) == 1


async def test_cancelled_tool_compaction_survives_new_turns(db, tmp_path, monkeypatch):
    """plan-20261117-compact-trigger：512k 窗口压缩后连续新消息不能重新载入旧历史。"""
    from unittest.mock import AsyncMock
    from app.orchestration import context_manager as cm, compaction, token_counter
    from app.orchestration.agent_loop import _compact_persistent_or_fallback, resolve_compact_thresholds
    from app.orchestration.context_memory import _fetch_main_messages
    from app.orchestration.token_counter import estimate_messages_tokens, messages_token_total
    from app.persistence.models.message import Message, Session

    session = await _mk_session(db)
    interrupted = Message(session_id=session.id, turn_id=1, sender_type="agent", msg_type="tool_call",
                          content={"tool": "ask_user_question", "args": {}, "call_key": "cancelled"})
    db.add(interrupted)
    await db.flush()
    await _mk_long_history(db, session.id, rounds=44, tool_chars=45000)
    await db.commit()
    source = await _fetch_main_messages(db, session.id, limit=None)
    window = 512000
    threshold = resolve_compact_thresholds(window, 0.9)["pre_threshold"]
    assert messages_token_total(source) >= threshold
    fallback = AsyncMock(side_effect=AssertionError("不应退回临时内存压缩"))
    monkeypatch.setattr(compaction, "auto_compact", fallback)
    result, _ = await _compact_persistent_or_fallback(
        db, session_id=session.id, provider=None, agent_window=window,
        used_tokens=messages_token_total(source), agent_id=1, agent_name="主代理", turn_id=1,
        messages=[M(role="system", content="规则"),
                  M(role="user", content="继续处理", context_kind="current_task")], reply_language="zh")
    assert result and interrupted.id in result["shadowed_ids"]
    fallback.assert_not_awaited()
    monkeypatch.setattr(cm, "_resolve_ta3_model_meta", AsyncMock(return_value={"provider": "deepseek"}))
    monkeypatch.setattr(cm, "_symbol_index_hint", AsyncMock(return_value=""))
    monkeypatch.setattr(cm, "project_structure_brief", AsyncMock(return_value=""))
    monkeypatch.setattr(token_counter, "get_agent_context_window", AsyncMock(return_value=window))
    for turn_id in (10, 11, 12):
        async with type(db)(bind=db.bind, expire_on_commit=False) as fresh:
            saved = await fresh.get(Session, session.id)
            bundle = await cm.build_main_context(
                fresh, agent=SimpleNamespace(id=1, model_id=None, name="测试"), session=saved,
                project=SimpleNamespace(id=None, path=str(tmp_path), rules_docs=None),
                turn=SimpleNamespace(id=turn_id, user_message_id=None), user_message="调整文案")
            messages = bundle.to_messages()
            assert [m.checkpoint_id for m in messages if m.context_kind == "checkpoint"] == [result["compaction_id"]]
            history_ids = {i for m in bundle.history for i in m.source_message_ids}
            assert not history_ids.intersection(result["shadowed_ids"])
            assert estimate_messages_tokens(messages) < threshold


async def test_rollup_preserves_previous_summary_and_attachment(db):
    from app.orchestration.context_compressor import _roll_checkpoint_frame
    from app.persistence.models.message import Message
    session = await _mk_session(db)
    text = get_checkpoint_preamble("zh") + "\n<compacted-summary>" + "旧信息" * 1200 + "</compacted-summary>\n附件路径：/docs/spec.docx"
    row = Message(session_id=session.id, sender_type="system", msg_type="summary", content={"text": text})
    db.add(row)
    await db.flush()
    ctx = {"compactions": [{"compaction_id": "old", "summary_message_id": row.id}],
           "injected_compactions": ["old"]}
    frame = await _roll_checkpoint_frame(db, ctx, "新摘要", "zh", [])
    assert "旧信息" * 1200 in frame and "/docs/spec.docx" in frame and "新摘要" in frame
    assert frame.count("<compacted-summary>") == 1


async def test_missing_active_checkpoint_degrades_instead_of_failing(db):
    """plan-20261115-checkpoint-degrade：缺一条摘要只跳过并报告，不能把整轮请求打死。"""
    missing: list[str] = []
    cps = await load_checkpoints(
        db,
        {"compactions": [{"compaction_id": "missing", "summary_message_id": 999}]},
        missing=missing,
    )
    assert cps == []
    assert missing == ["missing"]


async def test_partial_missing_checkpoint_keeps_valid_ones(db):
    """同一会话里一条摘要被删，其余有效检查点仍要正常注入（只降级缺失项）。"""
    from app.persistence.models.message import Message
    session = await _mk_session(db)
    row = Message(session_id=session.id, sender_type="system", msg_type="summary",
                  content={"text": "有效摘要"})
    db.add(row)
    await db.flush()
    missing: list[str] = []
    cps = await load_checkpoints(
        db,
        {"compactions": [
            {"compaction_id": "ok", "summary_message_id": row.id},
            {"compaction_id": "gone", "summary_message_id": 999},
        ]},
        missing=missing,
    )
    assert [c.checkpoint_id for c in cps] == ["ok"]
    assert cps[0].source_message_ids == [row.id]
    assert missing == ["gone"]


async def test_summary_request_contains_inert_tools_and_original_arguments():
    from app.orchestration.context_compressor import _summarize_with_llm
    seen = []
    class Capture:
        name = "capture"
        def supports_thinking(self):
            return False
        async def chat(self, request):
            seen.append(request)
            return ChatResponse(content="完成摘要")
    rows = [SimpleNamespace(id=1, msg_type="tool_call", sender_type="agent",
                            content={"tool": "fs_read", "args": {"path": "a.py"}, "call_key": "a"}),
            SimpleNamespace(id=2, msg_type="tool_result", sender_type="agent",
                            content={"output": "文件内容", "call_key": "a"})]
    assert (await _summarize_with_llm(None, Capture(), rows, language="zh"))[0] == "完成摘要"
    req = seen[0]
    assert not req.tools and not any(m.tool_calls or m.role == "tool" for m in req.messages)
    assert '"name": "fs_read"' in req.messages[1].content
    assert '"path": "a.py"' in req.messages[1].content
    assert "工具结果 id=a" in req.messages[1].content


def _wire(channel, request):
    from app.models.providers.openai_compatible import OpenAICompatibleProvider
    from app.models.providers.anthropic import AnthropicProvider
    from app.models.providers.ta3 import Ta3Provider
    from app.models.providers.workbuddy import WorkBuddyProvider
    from app.models.providers.trae import TraeProvider
    from app.models.providers.kiro import KiroProvider
    from app.models.providers.commandcode import CommandCodeProvider
    classes = {"openai": OpenAICompatibleProvider, "anthropic": AnthropicProvider,
               "ta3-openai": Ta3Provider, "ta3-anthropic": Ta3Provider,
               "workbuddy": WorkBuddyProvider, "trae": TraeProvider, "trae-work": TraeProvider,
               "kiro": KiroProvider, "commandcode": CommandCodeProvider}
    meta = {"provider": "anthropic" if channel == "ta3-anthropic" else "deepseek",
            "config_name": "test", "model_name": "test", "model_id": "test",
            "completionOptions": {"maxTokens": 8192, "thinkingEnabled": True}}
    cls = classes[channel]
    provider = cls(api_key="test", base_url="https://example.invalid", model="test", **(
        {"meta": meta} if cls in (Ta3Provider, WorkBuddyProvider, TraeProvider, KiroProvider) else {}))
    if channel == "openai":
        payload = {"messages": provider._convert_messages(request.messages)}
    elif channel == "anthropic":
        payload = provider._build_payload(request, stream=False)
    elif channel.startswith("ta3"):
        payload = (provider._build_anthropic_body if channel.endswith("anthropic") else provider._build_openai_body)(request, [])
    elif channel in ("workbuddy", "trae"):
        payload = provider._build_body(request)
    elif channel == "trae-work":
        payload = provider._build_work_body(request)
    else:
        payload = provider._build_payload(request)
    return provider, payload


def _strict_upstream(channel, body):
    """独立检查接收的 JSON，不调用产品校验器。"""
    if channel == "kiro":
        state = body["conversationState"]
        assert "userInputMessage" in state["currentMessage"]
        assert not state.get("history") or "assistantResponseMessage" in state["history"][-1]
        return
    rows = body.get("message_content", body.get("messages", body.get("params", {}).get("messages")))
    assert rows
    pending, seen = set(), set()
    for row in rows:
        blocks = row.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        calls = [(tc["id"], tc["function"]["name"]) for tc in row.get("tool_calls") or []]
        calls += [(b.get("id", b.get("toolCallId")), b.get("name", b.get("toolName")))
                  for b in blocks if b.get("type") in ("tool_use", "tool-call")]
        results = [row["tool_call_id"]] if row.get("role") == "tool" and "tool_call_id" in row else []
        results += [b.get("tool_use_id", b.get("toolCallId")) for b in blocks
                    if b.get("type") in ("tool_result", "tool-result")]
        if results:
            for key in results:
                assert key in pending
                pending.remove(key)
        else:
            assert not pending
        for key, name in calls:
            assert key and name and key not in seen
            seen.add(key)
            pending.add(key)
    assert not pending
    # 摘要不是模型应续写的末尾助手预填。
    assert "conversation_summary" not in str(rows[-1])


@pytest.mark.parametrize("channel", ["openai", "anthropic", "ta3-openai", "ta3-anthropic",
                                     "workbuddy", "trae", "trae-work", "kiro", "commandcode"])
@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.parametrize("ending", ["tool", "assistant"])
async def test_final_payload_accepted_and_next_tool_round_completes(channel, thinking, ending):
    rows = history()
    if ending == "assistant":
        rows.append(M(role="assistant", content="已经读取"))
    rows = project_checkpoints(rows, [checkpoint()])
    request = ChatRequest(messages=rows, model="test", thinking=thinking,
                          reasoning_effort="high" if thinking else "none", max_tokens=8192)
    providers = []
    accepted = []
    def receive(req):
        body = json.loads(req.content)
        _strict_upstream(channel, body)
        accepted.append(body)
        return httpx.Response(200, json={"content": "完成"})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as upstream:
            for step in range(2):
                provider, payload = _wire(channel, request)
                providers.append(provider)
                response = await upstream.post("https://local.invalid/model", json=payload)
                assert response.status_code == 200
                request.messages.extend([
                    M(role="assistant", tool_calls=[{"id": f"next{step}", "name": "fs_read", "arguments": {}}]),
                    M(role="tool", tool_call_id=f"next{step}", name="fs_read", content="后续执行结果")])
        assert len(accepted) == 2
    finally:
        for provider in providers:
            client = getattr(provider, "_client", None)
            if client is not None:
                await (client.aclose() if hasattr(client, "aclose") else client.close())


def test_structure_diagnostic_contains_no_sensitive_values():
    rows = history()
    diagnostic = json.dumps(structure_diagnostic(rows, protocol="test"), ensure_ascii=False)
    for text in ("文件A", "文件B", "a.py", "真实的推理过程", "保留原始限制"):
        assert text not in diagnostic


async def test_agent_loop_compacts_after_complete_tool_execution_and_continues(db, tmp_path, monkeypatch):
    from app.orchestration import agent_loop as loop
    from app.orchestration import token_counter
    from app.persistence.write_behind import write_behind
    from app.models.providers.openai_compatible import OpenAICompatibleProvider
    from app.orchestration.tools.registry import tool_registry
    session = await _mk_session(db)
    session.approval_mode = "full"
    await _mk_long_history(db, session.id, rounds=20)
    await db.commit()
    (tmp_path / "once.txt").write_text("只应读取一次", encoding="utf-8")
    events, requests, executions = [], [], []
    tool = tool_registry.get("fs_read")
    original_run = tool.run
    async def counted(args, ctx):
        executions.append(args)
        return await original_run(args, ctx)
    async def emit(_session, event):
        events.append(event["event"])
    async def no_hooks(*args, **kwargs):
        return []
    async def window(*args, **kwargs):
        return 100000
    class Model:
        name = "openai"
        def supports_thinking(self):
            return False
        async def chat(self, request):
            assert not request.tools
            return ChatResponse(content="已完成历史步骤，继续当前任务。")
        async def stream_structured(self, request):
            wire = OpenAICompatibleProvider._convert_messages(self, request.messages)
            _strict_upstream("openai", {"messages": wire})
            requests.append(request)
            if len(requests) == 1:
                yield {"type": "done", "usage": Usage(prompt_tokens=91000), "finish_reason": "tool_calls",
                       "tool_calls": [{"id": "once", "name": "fs_read", "arguments": {"path": "once.txt"}}]}
            else:
                assert "compact.completed" in events
                assert any(m.context_kind == "checkpoint" for m in request.messages)
                assert any(m.role == "tool" and "只应读取一次" in (m.content or "") for m in request.messages)
                yield {"type": "done", "usage": Usage(prompt_tokens=1000), "content": "完成", "finish_reason": "stop"}
    model = Model()
    class Registry:
        async def get_provider_for_agent(self, *args):
            return model, None
    monkeypatch.setattr(loop, "get_model_registry", Registry)
    monkeypatch.setattr(loop, "broadcast", emit)
    monkeypatch.setattr(loop.hook_service, "run_hooks", no_hooks)
    monkeypatch.setattr(tool, "run", counted)
    monkeypatch.setattr(token_counter, "get_agent_context_window", window)
    monkeypatch.setattr(token_counter, "get_calibration_factor", lambda *a: 1.0)
    monkeypatch.setattr(token_counter, "update_calibration_factor", lambda *a, **kw: 1.0)
    try:
        out = await loop.run_agent_loop(
            db, session_id=session.id, turn_id=1,
            agent=SimpleNamespace(id=1, kind="main", name="测试代理", model_id=None),
            context_messages=[M(role="system", content="读取文件"),
                              M(role="user", content="只读取一次", context_kind="current_task")],
            tool_schemas=[tool.function_schema()], workspace=str(tmp_path), max_steps=3)
        assert out.kind == "message", out.error
        assert out.text == "完成" and len(requests) == 2 and len(executions) == 1
        assert events.index("tool.result") < events.index("compact.started")
    finally:
        await write_behind.get(session.id, 1).flush()


@pytest.mark.parametrize("channel", ["anthropic", "ta3-anthropic"])
async def test_signed_thinking_roundtrip_preserves_exact_signature(channel):
    rows = history()
    blocks = [{"type": "thinking", "thinking": "真实推理", "signature": "original-signature"},
              {"type": "redacted_thinking", "data": "opaque-data"}]
    rows[2].thinking_blocks = blocks
    from app.orchestration.subagent import _dump_message
    restored = M(**_dump_message(rows[2]))
    assert restored.thinking_blocks == blocks
    rows[2] = restored
    provider, payload = _wire(channel, ChatRequest(messages=rows, model="test", thinking=True,
                                                  reasoning_effort="high", max_tokens=8192))
    try:
        assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
        assert assistant["content"][:2] == blocks
        assert rows[2].thinking_blocks == blocks
    finally:
        if getattr(provider, "_client", None):
            await provider._client.aclose()


def test_thinking_collector_preserves_streamed_signature_without_fabrication():
    from app.models.providers.thinking_blocks import collect_thinking, replay_thinking
    slots = {}
    collect_thinking(slots, "content_block_start", {"index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    collect_thinking(slots, "content_block_delta", {"index": 0, "delta": {"type": "thinking_delta", "thinking": "推理"}})
    assert replay_thinking(list(slots.values())) == []
    collect_thinking(slots, "content_block_delta", {"index": 0, "delta": {"type": "signature_delta", "signature": "part1"}})
    collect_thinking(slots, "content_block_delta", {"index": 0, "delta": {"type": "signature_delta", "signature": "part2"}})
    assert replay_thinking(list(slots.values())) == [{"type": "thinking", "thinking": "推理", "signature": "part1part2"}]


@pytest.mark.parametrize("arguments", ["not-json", "[]", [], None])
def test_final_request_rejects_invalid_function_arguments(arguments):
    from app.orchestration.history_sequence import validate_openai_wire
    with pytest.raises(HistorySequenceError):
        validate_openai_wire([{"role": "assistant", "tool_calls": [{"id": "x", "function": {
            "name": "read", "arguments": arguments}}]}, {"role": "tool", "tool_call_id": "x"}])


@pytest.mark.parametrize("trigger", ["pressure", "context-overflow"])
async def test_memory_fallback_keeps_original_task_and_avoids_main_thread(db, trigger, monkeypatch):
    from app.orchestration.agent_loop import _compact_persistent_or_fallback
    from app.core.config import settings
    monkeypatch.setattr(settings, "auto_compact_min_reclaim_tokens", 1)
    monkeypatch.setattr(settings, "auto_compact_keep_rounds", 2)
    rows = history()[:2]
    for i in range(12):
        rows.extend([M(role="assistant", tool_calls=[{"id": f"c{i}", "name": "fs_read", "arguments": {}}]),
                     M(role="tool", tool_call_id=f"c{i}", name="fs_read", content="内容" * 100)])
    before = [m.model_copy(deep=True) for m in rows]
    result, out = await _compact_persistent_or_fallback(
        db, session_id=999999, provider=None, agent_window=100000, used_tokens=90000,
        agent_id=5, agent_name="子代理", turn_id=10, messages=rows, thread_id=5, agent_kind="sub",
        reply_language="zh", trigger=trigger)
    assert result is None
    assert any(m.context_kind == "current_task" and "保留原始限制" in (m.content or "") for m in out)
    assert rows == before
    from app.orchestration.history_sequence import validate_tool_sequence
    validate_tool_sequence(out)


async def test_cancel_during_summary_does_not_commit_compaction(db):
    import asyncio
    from app.orchestration.context_compressor import compact_session
    session = await _mk_session(db)
    await _mk_long_history(db, session.id, rounds=20)
    await db.commit()
    class Cancel:
        name = "cancel"
        def supports_thinking(self):
            return False
        async def chat(self, request):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await compact_session(db, session=session, provider=Cancel(), context_window=10000, language="zh")
    await db.refresh(session)
    assert not (session.shared_context or {}).get("compactions")


async def test_structure_export_is_filtered_and_contains_no_body():
    from app.gateway.routers.diagnostics import context_structure_snapshot
    from app.orchestration.history_sequence import request_diagnostic_context, validate_openai_wire
    token = request_diagnostic_context.set({"session_id": 123456, "turn_id": 9})
    try:
        validate_openai_wire([{"role": "user", "content": "敏感用户正文"}])
        result = await context_structure_snapshot(session_id=123456)
        assert result["events"] and all(e["session_id"] == 123456 for e in result["events"])
        assert "敏感用户正文" not in json.dumps(result, ensure_ascii=False)
    finally:
        request_diagnostic_context.reset(token)


@pytest.mark.parametrize("ta3", [False, True])
async def test_main_context_rebuild_reinjects_old_marked_checkpoint(db, tmp_path, monkeypatch, ta3):
    from unittest.mock import AsyncMock
    from app.orchestration import context_manager as cm
    from app.persistence.models.message import Message
    session = await _mk_session(db)
    frame = checkpoint(text="旧摘要" * 1200 + "\n附件路径：/docs/keep.pdf").content
    row = Message(session_id=session.id, sender_type="system", msg_type="summary", content={"text": frame})
    db.add(row)
    await db.flush()
    session.shared_context = {"compactions": [{"compaction_id": "old", "summary_message_id": row.id}],
                              "injected_compactions": ["old"]}
    await db.commit()
    monkeypatch.setattr(cm, "_resolve_ta3_model_meta", AsyncMock(return_value={"provider": "deepseek"} if ta3 else None))
    monkeypatch.setattr(cm, "_symbol_index_hint", AsyncMock(return_value=""))
    monkeypatch.setattr(cm, "project_structure_brief", AsyncMock(return_value=""))
    for turn_id in (10, 11, 12):
        bundle = await cm.build_main_context(
            db, agent=SimpleNamespace(id=1, model_id=None, name="测试"), session=session,
            project=SimpleNamespace(id=None, path=str(tmp_path), rules_docs=None),
            turn=SimpleNamespace(id=turn_id, user_message_id=None), user_message="继续")
        messages = bundle.to_messages()
        cps = [m for m in messages if m.context_kind == "checkpoint"]
        assert len(cps) == 1 and cps[0].content == frame
        assert "/docs/keep.pdf" in cps[0].content


async def test_openai_adapter_http_roundtrip_after_compaction():
    from openai import AsyncOpenAI
    from app.models.providers.openai_compatible import OpenAICompatibleProvider
    requests = []
    def receive(req):
        body = json.loads(req.content)
        _strict_upstream("openai", body)
        requests.append(body)
        if len(requests) == 1:
            chunk = {"id": "response1", "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "new-call", "type": "function", "function": {
                    "name": "fs_read", "arguments": '{"path":"c.py"}'}}]}, "finish_reason": "tool_calls"}]}
        else:
            chunk = {"id": "response2", "choices": [{"index": 0, "delta": {"content": "执行完成"}, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    provider = OpenAICompatibleProvider(api_key="test", base_url="https://local.invalid/v1", model="test")
    await provider._client.close()
    provider._client = AsyncOpenAI(api_key="test", base_url="https://local.invalid/v1",
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(receive)))
    try:
        req = ChatRequest(model="test", messages=project_checkpoints(history(), [checkpoint()]))
        first = await provider.chat(req)
        assert first.tool_calls[0]["name"] == "fs_read"
        assert first.tool_calls[0]["arguments"] == {"path": "c.py"}
        req.messages.extend([M(role="assistant", tool_calls=first.tool_calls),
                             M(role="tool", tool_call_id="new-call", content="新结果")])
        assert (await provider.chat(req)).content == "执行完成"
        assert len(requests) == 2
    finally:
        await provider._client.close()


async def test_restoring_latest_checkpoint_reactivates_inherited_summary(db):
    from app.orchestration.context_compressor import compact_session
    from app.services.compression_service import restore_compaction
    session = await _mk_session(db)
    results = []
    for _ in range(2):
        await _mk_long_history(db, session.id, rounds=20)
        await db.commit()
        result = await compact_session(db, session=session, provider=None, context_window=10000, language="zh")
        assert result
        results.append(result)
        await db.refresh(session)
    await restore_compaction(db, session.id, results[-1]["compaction_id"])
    await db.refresh(session)
    active = await load_checkpoints(db, session.shared_context)
    assert len(active) == 1
    assert active[0].checkpoint_id == results[0]["compaction_id"]
    assert active[0].content == results[0]["checkpoint_text"]


@pytest.mark.parametrize("local", [False, True])
async def test_structure_failure_never_resends_same_payload_as_nonstream(local):
    from app.orchestration.agent_loop import _stream_chat_and_broadcast
    class Reject:
        name = "reject"
        chat_calls = 0
        async def stream_structured(self, request):
            if local:
                raise HistorySequenceError("请求工具名称缺失")
            raise RuntimeError("unexpected tool_use_id in tool_result blocks")
            yield  # pragma: no cover
        async def chat(self, request):
            self.chat_calls += 1
            raise AssertionError("不能原样再次发送")
    provider = Reject()
    with pytest.raises((HistorySequenceError, RuntimeError)):
        await _stream_chat_and_broadcast(provider, ChatRequest(messages=history(), model="test"),
                                         session_id=123, turn_id=10, agent_id=1, agent_name="测试")
    assert provider.chat_calls == 0


def test_signed_thinking_overhead_is_counted_in_live_and_database_budgets():
    from app.orchestration.token_counter import estimate_message_tokens, estimate_message_tokens_from_model
    block = {"type": "thinking", "thinking": "推理", "signature": "a" * 4000}
    plain = M(role="assistant", reasoning_content="推理")
    signed = plain.model_copy(update={"thinking_blocks": [block]})
    assert estimate_message_tokens(signed) > estimate_message_tokens(plain) + 500
    row = SimpleNamespace(msg_type="thinking", content={"text": "推理"})
    original = estimate_message_tokens_from_model(row)
    row.content["thinking_blocks"] = [block]
    assert estimate_message_tokens_from_model(row) > original + 500

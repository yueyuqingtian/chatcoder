# -*- coding: utf-8 -*-
"""压缩后首次请求即报错：工具调用与结果的配对必须由构造保证（plan-599-2520）。

真实事故（会话 630 / turn=2596，deepseek-flash / ta3 网关）：
    用户在新一轮发送第一条消息 → 前置检查判定超阈值 → 压缩成功（694 条消息折成
    checkpoint，占用降到 16.1%）→ **紧接着第一次模型调用就失败**，网关返回通用错误帧
    「AI服务异常，请稍后重试」，重试 3 次同样失败。压缩前同一会话请求一直正常。
    日志：`发送给API的消息数=137 角色分布={'system':1,'user':2,'assistant':67,'tool':66}`

根因（三处，都会让出站序列非法）：
1. ta3 的出站清洗按「紧随其后的连续 tool」整体判断，只要匹配到**任意一条**结果，
   就原样保留 assistant 的全部 tool_calls——并行调用缺一条结果（压缩把该结果划进
   摘要区就会这样）就留下悬空 tool_calls，上游直接判非法；
2. `auto_compact` 把 pre_boundary 拼在 repair/ensure **之后**，边界尾部可能悬空；
3. 落库式与 `emergency_compact` 结果没有统一收尾（无 user 承接 / 未规范化顺序），
   溢出恢复路径还漏了 ensure，导致 normalize 成批丢弃 tool 结果。

本文件锁死：任何压缩产物、任何出站清洗结果，assistant(tool_calls) 与 tool 必须严格一一对应。
"""
import pytest

from app.models.schemas import ChatMessage
from app.models.providers.ta3 import Ta3Provider
from app.orchestration.compaction import (
    emergency_compact,
    ensure_conversation_lead,
    finalize_compacted_messages,
)


def _assert_pairing_valid(messages: list[ChatMessage]) -> None:
    """断言序列严格合法：每个 tool_calls 都有紧随其后的对应结果，无孤儿 tool。"""
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m.role == "assistant" and m.tool_calls:
            ids = [tc.get("id") for tc in m.tool_calls if tc.get("id")]
            assert ids, "assistant(tool_calls) 不得带空 id"
            j = i + 1
            got = []
            while j < n and messages[j].role == "tool":
                got.append(messages[j].tool_call_id)
                j += 1
            assert set(ids) <= set(got), f"悬空 tool_calls: {set(ids) - set(got)}"
            i = j
            continue
        assert m.role != "tool", f"出现孤儿 tool 消息: {m.tool_call_id}"
        i += 1


# ─────────────────── 一、ta3 出站清洗：部分结果缺失不得留悬空 ───────────────────

def test_sanitize_drops_unmatched_tool_calls():
    """assistant 并行两个调用、只回了一个结果 → 必须剔除没有结果的那个调用。"""
    msgs = [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "fs_read", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "fs_grep", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "结果 A"},
    ]
    out = Ta3Provider._sanitize_openai_tool_pairing(msgs)
    assistant = next(m for m in out if m.get("role") == "assistant" and m.get("tool_calls"))
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["a"], \
        "没有结果的 tool_call 必须被剔除，否则上游直接 400"
    assert [m.get("tool_call_id") for m in out if m.get("role") == "tool"] == ["a"]


def test_sanitize_keeps_all_matched_results_in_order():
    """结果顺序与 tool_calls 不一致时，按 tool_calls 的顺序重排。"""
    msgs = [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "function": {"name": "t1", "arguments": "{}"}},
            {"id": "b", "function": {"name": "t2", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "b", "content": "B"},
        {"role": "tool", "tool_call_id": "a", "content": "A"},
    ]
    out = Ta3Provider._sanitize_openai_tool_pairing(msgs)
    ids = [m.get("tool_call_id") for m in out if m.get("role") == "tool"]
    assert ids == ["a", "b"]


def test_sanitize_all_missing_degrades_to_text():
    """一个结果都没有时整条降级为纯文本，不能留 tool_calls。"""
    msgs = [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "user", "content": "继续"},
    ]
    out = Ta3Provider._sanitize_openai_tool_pairing(msgs)
    assert all(not m.get("tool_calls") for m in out)


def test_sanitize_orphan_tool_becomes_user():
    """孤儿 tool 消息降级为 user 文本，不得原样下发。"""
    msgs = [
        {"role": "user", "content": "开始"},
        {"role": "tool", "tool_call_id": "x", "content": "孤立结果"},
    ]
    out = Ta3Provider._sanitize_openai_tool_pairing(msgs)
    assert [m.get("role") for m in out] == ["user", "user"]


# ─────────────────── 二、压缩产物统一收尾 ───────────────────

def test_finalize_fills_missing_results():
    """压缩切断了某个结果 → finalize 必须补占位，而不是留下悬空调用。"""
    msgs = [
        ChatMessage(role="system", content="系统"),
        ChatMessage(role="assistant", tool_calls=[
            {"id": "a", "name": "fs_read", "arguments": {}},
            {"id": "b", "name": "fs_grep", "arguments": {}},
        ]),
        ChatMessage(role="tool", content="A", tool_call_id="a", name="fs_read"),
        ChatMessage(role="user", content="继续"),
    ]
    out = finalize_compacted_messages(msgs, "zh")
    _assert_pairing_valid(out)


def test_finalize_ensures_user_lead():
    """保留区从 assistant(tool_calls) 起切、整段没有 user 时，必须补一条 user 承接。"""
    msgs = [
        ChatMessage(role="system", content="系统"),
        ChatMessage(role="assistant", tool_calls=[{"id": "a", "name": "t", "arguments": {}}]),
        ChatMessage(role="tool", content="A", tool_call_id="a", name="t"),
    ]
    out = finalize_compacted_messages(msgs, "zh")
    non_system = [m for m in out if m.role not in ("system", "developer")]
    assert non_system and non_system[0].role == "user", "压缩产物必须以 user 开场"


def test_finalize_is_idempotent():
    """重复收尾不得继续增删消息（压缩后可能被多路径各处理一次）。"""
    msgs = [
        ChatMessage(role="system", content="系统"),
        ChatMessage(role="user", content="开始"),
        ChatMessage(role="assistant", tool_calls=[{"id": "a", "name": "t", "arguments": {}}]),
        ChatMessage(role="tool", content="A", tool_call_id="a", name="t"),
    ]
    once = finalize_compacted_messages(msgs, "zh")
    twice = finalize_compacted_messages(once, "zh")
    assert [m.role for m in once] == [m.role for m in twice]


def test_conversation_lead_noop_when_user_first():
    """已有 user 打头时不得再插一条。"""
    msgs = [
        ChatMessage(role="system", content="系统"),
        ChatMessage(role="user", content="开始"),
        ChatMessage(role="assistant", content="回应"),
    ]
    assert ensure_conversation_lead(msgs, "zh") is msgs


# ─────────────────── 三、emergency_compact 产物合法 ───────────────────

def test_emergency_compact_output_is_valid():
    """紧急压缩（溢出兜底）产物同样必须配对完好 + user 开场。"""
    msgs = [ChatMessage(role="system", content="系统")]
    msgs.append(ChatMessage(role="user", content="开始"))
    for r in range(12):
        msgs.append(ChatMessage(role="assistant", tool_calls=[
            {"id": f"c{r}", "name": "fs_read", "arguments": {"path": f"f{r}.py"}},
        ]))
        msgs.append(ChatMessage(role="tool", content=f"内容{r}", tool_call_id=f"c{r}", name="fs_read"))
    out = emergency_compact(msgs, 200000, "zh")
    _assert_pairing_valid(out)
    non_system = [m for m in out if m.role not in ("system", "developer")]
    assert non_system and non_system[0].role == "user"


# ─────────────────── 四、事故现场复现 ───────────────────

def test_incident_sequence_is_legal_after_finalize():
    """事故序列：67 assistant / 66 tool（缺一条结果）+ 无 user 开场。

    修复前：ta3 清洗保留含两个 tool_calls 的 assistant，只跟一条结果 → 上游 400。
    修复后：清洗剔除没有结果的调用，finalize 补占位，序列严格合法。
    """
    msgs = [ChatMessage(role="developer", content="checkpoint 摘要")]
    for i in range(3):
        msgs.append(ChatMessage(role="assistant", tool_calls=[
            {"id": f"p{i}", "name": "t", "arguments": {}},
            {"id": f"q{i}", "name": "t", "arguments": {}},
        ]))
        # 每轮只保留第一个调用的结果：模拟压缩把另一半结果划进摘要区
        msgs.append(ChatMessage(role="tool", content=f"R{i}", tool_call_id=f"p{i}", name="t"))
    fixed = finalize_compacted_messages(msgs, "zh")
    _assert_pairing_valid(fixed)
    payload = Ta3Provider._sanitize_openai_tool_pairing([
        {"role": m.role, "content": m.content,
         "tool_calls": [{"id": tc.get("id"), "function": {"name": tc.get("name")}} for tc in (m.tool_calls or [])] or None,
         "tool_call_id": m.tool_call_id}
        for m in fixed
    ])
    # 出站后再校验一次：assistant 的 tool_calls 必须都有紧随的 tool 结果
    i = 0
    while i < len(payload):
        m = payload[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            got = []
            j = i + 1
            while j < len(payload) and payload[j].get("role") == "tool":
                got.append(payload[j].get("tool_call_id"))
                j += 1
            assert set(ids) <= set(got)
            i = j
            continue
        assert m.get("role") != "tool"
        i += 1

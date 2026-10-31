# -*- coding: utf-8 -*-
"""压缩后消息序列首条必须是 user（plan-598-2517）。

真实事故（会话 605 / turn=2520 / deepseek-flash）：
    step=46 触发前置压缩，629 条压到 154 条；紧接着流式调用异常 →
    「模型请求失败：AI服务异常，请稍后重试」（重试 3 次仍失败）。
    用户重发「继续」后正常。

日志证据：
    失败 turn=2520：压缩后 发送给API的消息数=... 角色分布={'system': 1, 'user': 0, ...}
    成功 turn=2524：发送给API的消息数=156 角色分布={'system': 1, 'user': 4, ...}

根因：压缩保留区从 assistant(tool_calls) 起切（compaction.py 的 keep_rounds 切点），
压缩产物里一条 user 都没有；ta3 侧又把摘要（developer）改写成 assistant、把
[compact-boundary] 丢弃，最终消息序列变成 `system → assistant → assistant`。
网关要求对话以 user 开场，于是直接判非法。

本文件锁死三条约定：
1. `_ensure_user_lead` 在首条非 system 不是 user 时补一条 user；
2. OpenAI/DeepSeek 通道与 Anthropic 通道的出站消息都以 user 开场；
3. 正常序列（已有 user 打头）不被改动——不能为了修 bug 给每轮都插一条假 user。
"""
import asyncio

import pytest

from app.models.schemas import ChatMessage, ChatRequest
from app.models.providers.ta3 import (
    Ta3Provider,
    _ensure_user_lead,
)
from app.orchestration.compaction import auto_compact


# ─────────────────────── 一、_ensure_user_lead 单元行为 ───────────────────────

def test_user_lead_inserted_when_sequence_starts_with_assistant():
    """压缩产物（system → assistant → assistant）必须补出 user 开场。"""
    msgs = [
        ChatMessage(role="system", content="主 system"),
        ChatMessage(role="assistant", content="<conversation_summary>…</conversation_summary>"),
        ChatMessage(role="assistant", content="接着干"),
    ]
    out = _ensure_user_lead(msgs)
    roles = [m.role for m in out]
    assert roles[0] == "system"
    assert roles[1] == "user", f"首条非 system 必须是 user，实际 {roles}"
    assert len(out) == len(msgs) + 1


def test_user_lead_inserted_when_sequence_starts_with_tool():
    """以 tool 打头（孤儿工具结果）同样要补 user。"""
    msgs = [
        ChatMessage(role="system", content="s"),
        ChatMessage(role="tool", content="结果", tool_call_id="tc_1"),
        ChatMessage(role="assistant", content="a"),
    ]
    out = _ensure_user_lead(msgs)
    assert [m.role for m in out][:2] == ["system", "user"]


def test_no_change_when_user_already_leads():
    """已有 user 打头时不得插入任何消息（否则每轮都多一条假 user）。"""
    msgs = [
        ChatMessage(role="system", content="s"),
        ChatMessage(role="user", content="真实用户消息"),
        ChatMessage(role="assistant", content="a"),
    ]
    out = _ensure_user_lead(msgs)
    assert len(out) == len(msgs)
    assert [m.role for m in out] == ["system", "user", "assistant"]
    assert out[1].content == "真实用户消息"


def test_all_system_sequence_gets_user():
    """极端情况：全是 system 也要补出 user，让请求能成立。"""
    msgs = [ChatMessage(role="system", content="s1"), ChatMessage(role="system", content="s2")]
    out = _ensure_user_lead(msgs)
    assert any(m.role == "user" for m in out)


def test_empty_sequence_unchanged():
    assert _ensure_user_lead([]) == []


# ─────────────────────── 二、两个协议通道出站均为 user 开场 ───────────────────────

def _provider(deepseek: bool = True) -> Ta3Provider:
    meta = {
        "provider": "deepseek" if deepseek else "anthropic",
        "completionOptions": {"temperature": 0.1, "maxTokens": 8192,
                              "thinkingEnabled": True, "thinkingLevels": [{"level": "max"}]},
    }
    prov = Ta3Provider(api_key="k", base_url="https://x/ai/dispatch/v2/openai",
                       model="deepseek-flash", meta=meta)
    prov._anthropic = not deepseek
    prov._deepseek = deepseek
    return prov


def _compressed_sequence() -> list[ChatMessage]:
    """复刻事故现场：system + 压缩摘要 + 保留区 assistant/tool，无 user。"""
    return [
        ChatMessage(role="system", content="主 system"),
        ChatMessage(role="system", content="[compact-boundary id=abc] 以上历史已压缩为摘要。"),
        ChatMessage(role="developer", content="以下是之前对话的摘要：\n已完成大半工作"),
        ChatMessage(role="assistant", content="继续执行",
                    tool_calls=[{"id": "tc_1", "name": "fs_read", "arguments": {"path": "a.py"}}]),
        ChatMessage(role="tool", content="文件内容", name="fs_read", tool_call_id="tc_1"),
    ]


def _first_non_system(msgs: list[dict]) -> str:
    for m in msgs:
        if m.get("role") not in ("system", "developer"):
            return m.get("role")
    return ""


def test_openai_channel_leads_with_user_after_compaction():
    """DeepSeek/OpenAI 通道：压缩后的序列出站必须以 user 开场。"""
    prov = _provider(deepseek=True)
    req = ChatRequest(model="deepseek-flash", messages=_compressed_sequence(), tools=None)
    body = prov._build_openai_body(req, [])
    wire = body.get("messages") or []
    assert _first_non_system(wire) == "user", \
        f"OpenAI 通道首条非 system 必须是 user，实际={_first_non_system(wire)}"


def test_anthropic_channel_leads_with_user_after_compaction():
    """Anthropic 通道：压缩后的序列出站必须以 user 开场。"""
    prov = _provider(deepseek=False)
    system, converted = prov._convert_anthropic_messages(_compressed_sequence())
    assert converted, "转换结果不能为空"
    assert converted[0]["role"] == "user", \
        f"Anthropic 通道首条必须是 user，实际={converted[0]['role']}"
    assert system, "主 system 不能丢"


def test_anthropic_channel_tool_pairing_preserved():
    """补 user 不能破坏 tool_use ↔ tool_result 配对。"""
    prov = _provider(deepseek=False)
    _, converted = prov._convert_anthropic_messages(_compressed_sequence())
    use_ids, res_ids = set(), set()
    for m in converted:
        for b in (m.get("content") or []):
            if isinstance(b, dict):
                if b.get("type") == "tool_use":
                    use_ids.add(b.get("id"))
                if b.get("type") == "tool_result":
                    res_ids.add(b.get("tool_use_id"))
    assert res_ids <= use_ids, f"存在孤儿 tool_result: {res_ids - use_ids}"


# ─────────────────────── 三、压缩层兜底 ───────────────────────

def test_auto_compact_inserts_user_carry_over(monkeypatch):
    """auto_compact 在保留区无 user 时必须补一条承接 user（从源头保证）。"""
    monkeypatch.setattr("app.core.config.settings.auto_compact_keep_rounds", 2)
    monkeypatch.setattr("app.core.config.settings.auto_compact_min_reclaim_tokens", 1)

    # 造 6 个工具回合，全部没有 user 位于尾部保留区
    msgs = [
        ChatMessage(role="system", content="主 system"),
        ChatMessage(role="user", content="最早的请求"),
        ChatMessage(role="assistant", content="做完",
                    tool_calls=[{"id": "tc_0", "name": "fs_read", "arguments": {}}]),
        ChatMessage(role="tool", content="r0" * 50, name="fs_read", tool_call_id="tc_0"),
    ]
    for i in range(1, 6):
        msgs.append(ChatMessage(role="assistant", content=f"步骤{i}",
                                tool_calls=[{"id": f"tc_{i}", "name": "fs_read", "arguments": {}}]))
        msgs.append(ChatMessage(role="tool", content=f"r{i}" * 50, name="fs_read",
                                tool_call_id=f"tc_{i}"))

    out = asyncio.run(auto_compact(msgs, context_window=200000, provider=None, language="zh"))
    roles = [m.role for m in out]
    assert any(r == "user" for r in roles), f"压缩后仍没有 user：{roles}"


# ─────────────────────── 四、正常路径不受影响 ───────────────────────

def test_normal_sequence_not_modified():
    """已有 user 打头的正常历史，出站不得多出额外的 user。"""
    prov = _provider(deepseek=True)
    msgs = [
        ChatMessage(role="system", content="s"),
        ChatMessage(role="user", content="用户问题"),
        ChatMessage(role="assistant", content=None,
                    tool_calls=[{"id": "tc_1", "name": "fs_list", "arguments": {}}]),
        ChatMessage(role="tool", content="结果", name="fs_list", tool_call_id="tc_1"),
    ]
    req = ChatRequest(model="deepseek-flash", messages=msgs, tools=None)
    wire = prov._build_openai_body(req, []).get("messages") or []
    users = [m for m in wire if m.get("role") == "user"]
    assert len(users) == 1, f"不得插入多余 user，实际 user 数={len(users)}"
    assert users[0].get("content") == "用户问题"

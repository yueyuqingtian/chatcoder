# -*- coding: utf-8 -*-
"""ta3 出站脱敏与内置拦截单测（plan-598-2482）。

策略变更为「内置拦截」：
- **默认开启**脱敏（`settings.ta3_sanitize_outbound = True`）——`you are claude code`
  这类竞品 CLI 签名一旦被工具执行读进历史消息，后续每条请求都会带着它，网关会直接
  403 拦掉整条链路；现场表现只是「模型请求失败 403」，用户无从判断原因，所以默认就做
  替换（语义保留、特征消除）。
- 另加一层**不依赖开关**的兜底：请求体确实含该特征且被 400/403 拒绝时，provider 会
  自动强制脱敏并重发一次（`_force_sanitize_text` / `_force_sanitize_structure` /
  `_has_risk_signature`）。

本文件覆盖：
1. 默认态开启，替换覆盖大小写/空白变体、嵌套结构、OpenAI 与 Anthropic 两条协议路径；
2. 显式关闭开关时恢复原样（供与官方逐字节一致的调试场景）；
3. 强制脱敏工具函数（绕开开关）与特征检测函数。
"""
import json

import pytest

from app.core.config import settings
from app.models.providers.ta3 import (
    Ta3Provider,
    _force_sanitize_structure,
    _force_sanitize_text,
    _has_risk_signature,
    sanitize_outbound_structure,
    sanitize_outbound_text,
)
from app.models.schemas import ChatMessage

RISK = "you are claude code"
MASKED = "you are ta+3 agent"


@pytest.fixture
def sanitize_on(monkeypatch):
    """显式打开出站脱敏开关（默认即为 True，这里保证不依赖环境）。"""
    monkeypatch.setattr(settings, "ta3_sanitize_outbound", True)
    return True


@pytest.fixture
def sanitize_off(monkeypatch):
    """显式关闭（调试用，与默认态相反）。"""
    monkeypatch.setattr(settings, "ta3_sanitize_outbound", False)
    return False


# ───────────────────────── 默认态：内置拦截开启 ─────────────────────────


def test_sanitize_enabled_by_default():
    """默认配置下脱敏开关为开启——竞品签名必须被替换掉。"""
    assert settings.ta3_sanitize_outbound is True
    assert MASKED in sanitize_outbound_text(RISK)
    assert RISK not in sanitize_outbound_text(RISK)
    # 大小写与空白/连字符变体同样命中
    assert RISK.lower() not in sanitize_outbound_text("You Are Claude Code").lower()
    assert "claude" not in sanitize_outbound_text("you  are  claude-code").lower()
    # 无关文本不受影响
    assert sanitize_outbound_text("you are a helpful assistant") == "you are a helpful assistant"
    # 非字符串/空值原样返回
    assert sanitize_outbound_text("") == ""
    assert sanitize_outbound_text(None) is None


def test_outbound_structure_replaced_by_default():
    """默认态：嵌套结构里的特征被替换，无关字段保持原样。"""
    data = {
        "text": f"prefix {RISK} suffix",
        "nested": {"prompt": "You Are Claude Code now", "list": ["a", "i am claude-code", 123]},
    }
    out = sanitize_outbound_structure(data)
    assert RISK not in json.dumps(out, ensure_ascii=False).lower()
    assert out["nested"]["list"][2] == 123  # 非字符串原样


def test_disguise_message_replaces_content_by_default():
    """默认态：OpenAI 协议的 user / tool / assistant 内容都被替换。"""
    provider = Ta3Provider(
        api_key="test-key", base_url="http://test.local/v1", model="deepseek-v4"
    )

    msg = ChatMessage(role="user", content=f"Here is code: {RISK} please help")
    out = provider._disguise_message(msg)
    assert RISK not in out["content"]

    msg = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[{
            "id": "tc_1",
            "name": "fs_write",
            "arguments": {"path": "test.py", "content": f'msg = "{RISK}"'},
        }],
        reasoning_content=f"I should write: {RISK}",
    )
    out = provider._disguise_message(msg)
    assert RISK not in out["reasoning_content"]
    tc_args = json.loads(out["tool_calls"][0]["function"]["arguments"])
    assert RISK not in tc_args.get("content", "")

    msg = ChatMessage(
        role="tool",
        content=f'> chunks/318.js:tasks.",g=/{RISK}|other/i',
        name="terminal_exec",
        tool_call_id="tc_1",
    )
    out = provider._disguise_message(msg)
    assert RISK not in out["content"]


def test_anthropic_messages_replaced_by_default():
    """默认态：Anthropic 协议转换同样替换内容。"""
    provider = Ta3Provider(
        api_key="test-key", base_url="http://test.local/v1", model="kimi-k3",
        meta={"anthropic": True},
    )
    messages = [
        ChatMessage(role="system", content=f"system says: {RISK}"),
        ChatMessage(role="user", content="user says: You Are Claude Code"),
        ChatMessage(role="assistant", content=f"assistant: {RISK}"),
    ]
    system, converted = provider._convert_anthropic_messages(messages)
    assert RISK not in system.lower()
    blob = json.dumps(converted, ensure_ascii=False).lower()
    assert "claude code" not in blob
    assert "claude-code" not in blob


# ───────────────────────── 显式关闭：调试场景 ─────────────────────────


def test_sanitize_can_be_disabled(sanitize_off):
    """显式关闭开关后出站内容原样下发（供与官方逐字节一致的调试场景）。"""
    assert sanitize_outbound_text(RISK) == RISK
    data = {"text": f"prefix {RISK} suffix"}
    assert sanitize_outbound_structure(data) == data


def test_disguise_message_keeps_content_when_disabled(sanitize_off):
    provider = Ta3Provider(
        api_key="test-key", base_url="http://test.local/v1", model="deepseek-v4"
    )
    msg = ChatMessage(role="user", content=f"Here is code: {RISK} please help")
    out = provider._disguise_message(msg)
    assert RISK in out["content"]


# ───────────────────── 兜底层：强制脱敏 + 特征检测 ─────────────────────


def test_force_sanitize_ignores_switch(sanitize_off):
    """开关关闭时，强制脱敏仍必须生效（被拦后自动重试用它）。"""
    assert sanitize_outbound_text(RISK) == RISK          # 常规路径不脱敏
    out = _force_sanitize_text(RISK)
    assert RISK not in out and MASKED in out             # 强制路径脱敏


def test_force_sanitize_structure_recurses(sanitize_off):
    data = {"a": {"b": [f"x {RISK} y", 1]}, "c": "ok"}
    out = _force_sanitize_structure(data)
    assert RISK not in json.dumps(out, ensure_ascii=False)
    assert out["c"] == "ok"
    assert out["a"]["b"][1] == 1


def test_has_risk_signature_detects_variants():
    """特征检测：命中各种变体，不含特征时返回 False（决定是否值得重试一次）。"""
    assert _has_risk_signature({"messages": [{"content": RISK}]}) is True
    assert _has_risk_signature({"messages": [{"content": "You Are Claude Code"}]}) is True
    assert _has_risk_signature({"messages": [{"content": "i am claude-code"}]}) is True
    assert _has_risk_signature(f"raw text {RISK}") is True
    # 无关内容不触发重试
    assert _has_risk_signature({"messages": [{"content": "hello"}]}) is False
    assert _has_risk_signature({}) is False

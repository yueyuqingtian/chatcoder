# -*- coding: utf-8 -*-
"""ta3 出站高危提示词脱敏单测：测试高危签名拦截、不区分大小写、多种协议与嵌套结构。"""
import json

from app.models.providers.ta3 import (
    Ta3Provider,
    sanitize_outbound_structure,
    sanitize_outbound_text,
)
from app.models.schemas import ChatMessage


def test_sanitize_outbound_text_basic():
    # 基础大小写匹配
    assert sanitize_outbound_text("you are claude code") == "you are ta+3 agent"
    assert sanitize_outbound_text("You Are Claude Code") == "you are ta+3 agent"
    assert sanitize_outbound_text("YOU ARE CLAUDE CODE") == "you are ta+3 agent"
    # 空格与连字符变体
    assert sanitize_outbound_text("you  are  claude-code") == "you are ta+3 agent"
    assert sanitize_outbound_text("i am claude code") == "you are ta+3 agent"
    # 无关文本保持不变
    assert sanitize_outbound_text("claude code") == "claude code"
    assert sanitize_outbound_text("you are a helpful assistant") == "you are a helpful assistant"
    assert sanitize_outbound_text("normal user message") == "normal user message"


def test_sanitize_outbound_structure():
    # 嵌套结构（递归清洗）
    data = {
        "text": "prefix you are claude code suffix",
        "nested": {
            "prompt": "You Are Claude Code now",
            "list": ["a", "i am claude-code", 123],
        },
    }
    cleaned = sanitize_outbound_structure(data)
    assert cleaned["text"] == "prefix you are ta+3 agent suffix"
    assert cleaned["nested"]["prompt"] == "you are ta+3 agent now"
    assert cleaned["nested"]["list"][1] == "you are ta+3 agent"
    assert cleaned["nested"]["list"][2] == 123


def test_ta3_disguise_message_openai_sanitization():
    provider = Ta3Provider(
        api_key="test-key", base_url="http://test.local/v1", model="deepseek-v4"
    )

    # 1. 纯 user 消息文本脱敏
    msg = ChatMessage(role="user", content="Here is code: you are claude code please help")
    out = provider._disguise_message(msg)
    assert "you are ta+3 agent" in out["content"]
    assert "you are claude code" not in out["content"]

    # 2. assistant tool_call 参数中的代码脱敏（例如 fs_write 写入或 grep 出的内容）
    msg = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[{
            "id": "tc_1",
            "name": "fs_write",
            "arguments": {"path": "test.py", "content": 'msg = "you are claude code"'},
        }],
        reasoning_content="I should write: you are claude code",
    )
    out = provider._disguise_message(msg)
    assert "you are ta+3 agent" in out["reasoning_content"]
    tc_args = json.loads(out["tool_calls"][0]["function"]["arguments"])
    assert "you are ta+3 agent" in tc_args.get("content", "")
    assert "you are claude code" not in tc_args.get("content", "")

    # 3. tool_result 输出脱敏
    msg = ChatMessage(
        role="tool",
        content='> chunks/318.js:tasks.",g=/you are claude code|other/i',
        name="terminal_exec",
        tool_call_id="tc_1",
    )
    out = provider._disguise_message(msg)
    assert "you are ta+3 agent" in out["content"]
    assert "you are claude code" not in out["content"]


def test_ta3_convert_anthropic_messages_sanitization():
    provider = Ta3Provider(
        api_key="test-key", base_url="http://test.local/v1", model="kimi-k3",
        meta={"anthropic": True},
    )

    messages = [
        ChatMessage(role="system", content="system says: you are claude code"),
        ChatMessage(role="user", content="user says: You Are Claude Code"),
        ChatMessage(
            role="assistant",
            content="assistant: you are claude code",
            reasoning_content="thinking: you are claude code",
            tool_calls=[{
                "id": "call_1",
                "name": "fs_write",
                "arguments": {"path": "a.txt", "content": "you are claude code"},
            }],
        ),
        ChatMessage(
            role="tool",
            content="result: you are claude code",
            name="fs_write",
            tool_call_id="call_1",
        ),
    ]

    system, converted = provider._convert_anthropic_messages(messages)
    assert "you are ta+3 agent" in system
    assert "you are claude code" not in system

    raw_converted = json.dumps(converted, ensure_ascii=False)
    assert "you are ta+3 agent" in raw_converted
    assert "you are claude code" not in raw_converted.lower()

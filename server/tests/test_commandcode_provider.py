"""单元测试：CommandCodeProvider 请求转换与响应流解析。"""
import asyncio
import json
import pytest

from app.models.providers.commandcode import CommandCodeProvider
from app.models.schemas import ChatMessage, ChatRequest, Usage


def test_convert_messages_and_tools():
    provider = CommandCodeProvider(api_key="user_test123", model="zai-org/GLM-5.1")

    messages = [
        ChatMessage(role="system", content="System instruction 1"),
        ChatMessage(role="developer", content="System instruction 2"),
        ChatMessage(role="user", content="hello"),
        ChatMessage(
            role="assistant",
            content="let me call tool",
            tool_calls=[{"id": "call_1", "name": "fs_list", "arguments": {"path": "."}}],
        ),
        ChatMessage(
            role="tool",
            tool_call_id="call_1",
            name="fs_list",
            content='{"files": ["a.txt"]}',
        ),
    ]

    system_prompt, converted = provider._convert_messages(messages)
    assert "System instruction 1" in system_prompt
    assert "System instruction 2" in system_prompt

    assert len(converted) == 3
    # user
    assert converted[0]["role"] == "user"
    assert converted[0]["content"] == [{"type": "text", "text": "hello"}]

    # assistant with tool-call
    assert converted[1]["role"] == "assistant"
    assert converted[1]["content"][0] == {"type": "text", "text": "let me call tool"}
    assert converted[1]["content"][1]["type"] == "tool-call"
    assert converted[1]["content"][1]["toolCallId"] == "call_1"
    assert converted[1]["content"][1]["toolName"] == "fs_list"
    assert converted[1]["content"][1]["input"] == {"path": "."}

    # tool result
    assert converted[2]["role"] == "tool"
    assert converted[2]["content"][0]["type"] == "tool-result"
    assert converted[2]["content"][0]["toolCallId"] == "call_1"
    assert converted[2]["content"][0]["output"]["value"] == '{"files": ["a.txt"]}'


def test_build_payload():
    provider = CommandCodeProvider(api_key="user_test123", model="zai-org/GLM-5.1")
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="test")],
        model="deepseek/deepseek-v4-flash",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "fs_read",
                    "description": "Read file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
        temperature=0.7,
        max_tokens=2048,
    )

    payload = provider._build_payload(request)
    assert "threadId" in payload
    assert payload["memory"] == ""
    assert "config" in payload
    assert payload["config"]["environment"]

    params = payload["params"]
    assert params["model"] == "deepseek/deepseek-v4-flash"
    assert params["temperature"] == 0.7
    assert params["max_tokens"] == 2048
    assert params["stream"] is True
    assert len(params["tools"]) == 1
    assert params["tools"][0]["name"] == "fs_read"
    assert "input_schema" in params["tools"][0]


def test_convert_image_block_to_native_image_part():
    """plan-234-1171 R3: image_url 块必须转成 CommandCode 原生 image 块。

    修复前该分支把图片硬编码替换为占位文本 "[image]"，base64 从未进入请求体，
    模型永远看不到图。真实协议（api.commandcode.ai 实测命中）要求：
      {"type": "image", "source": {"type": "url", "url": "<data URI 或 http URL>"}}
    """
    provider = CommandCodeProvider(api_key="user_test123", model="deepseek/deepseek-v4.1-flash")

    data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    messages = [
        ChatMessage(
            role="user",
            content="这张图是什么？",
            content_blocks=[{"type": "image_url", "image_url": {"url": data_uri}}],
        ),
    ]

    _, converted = provider._convert_messages(messages)
    assert len(converted) == 1
    content = converted[0]["content"]

    # 首块是文本，次块是图片
    assert content[0] == {"type": "text", "text": "这张图是什么？"}
    img = content[1]
    assert img["type"] == "image"
    assert img["source"]["type"] == "url"
    assert img["source"]["url"] == data_uri
    # 关键回归：不得再出现占位文本
    assert "[image]" not in str(content)


def test_convert_image_block_remote_url_and_passthrough():
    """远程 http(s) URL 与已是原生 image 形态的块都应正确保留。"""
    provider = CommandCodeProvider(api_key="user_test123", model="deepseek/deepseek-v4.1-flash")

    messages = [
        ChatMessage(
            role="user",
            content=None,
            content_blocks=[
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                {"type": "image", "source": {"type": "url", "url": "https://example.com/b.png"}},
            ],
        ),
    ]

    _, converted = provider._convert_messages(messages)
    content = converted[0]["content"]
    assert content[0] == {
        "type": "image", "source": {"type": "url", "url": "https://example.com/a.png"},
    }
    # 原生块原样透传
    assert content[1] == {
        "type": "image", "source": {"type": "url", "url": "https://example.com/b.png"},
    }


def test_convert_image_block_empty_url_skipped():
    """空 url 的 image 块被安全跳过，不产生非法 content 元素（避免 400）。"""
    provider = CommandCodeProvider(api_key="user_test123", model="deepseek/deepseek-v4.1-flash")
    messages = [
        ChatMessage(
            role="user",
            content="hi",
            content_blocks=[{"type": "image_url", "image_url": {"url": ""}}],
        ),
    ]
    _, converted = provider._convert_messages(messages)
    content = converted[0]["content"]
    assert all(c.get("type") != "image" for c in content)
    assert content == [{"type": "text", "text": "hi"}]


@pytest.mark.asyncio
async def test_response_failure_reason_catches_empty_tool_calls():
    from app.orchestration.agent_loop import _response_failure_reason
    from app.models.schemas import ChatResponse

    # 1. 正常 tool_calls
    resp_ok = ChatResponse(
        content=None,
        thinking="The user wants to list files",
        tool_calls=[{"id": "call_1", "name": "fs_list", "arguments": {}}],
        finish_reason="tool_calls",
        usage=Usage(),
    )
    assert _response_failure_reason(resp_ok) is None

    # 2. 畸形响应：finish=tool_calls 但 tool_calls 为空
    resp_malformed = ChatResponse(
        content=None,
        thinking="The",
        tool_calls=[],
        finish_reason="tool_calls",
        usage=Usage(),
    )
    res = _response_failure_reason(resp_malformed)
    assert res is not None
    msg, fatal = res
    assert fatal is True
    assert "无工具调用数据" in msg or "流丢帧" in msg

    # 3. 截断响应：仅返回极短思考且无 content/tool_calls
    resp_truncated = ChatResponse(
        content=None,
        thinking="The",
        tool_calls=[],
        finish_reason="stop",
        usage=Usage(),
    )
    res_trunc = _response_failure_reason(resp_truncated)
    assert res_trunc is not None
    msg_trunc, fatal_trunc = res_trunc
    assert fatal_trunc is True
    assert "截断" in msg_trunc

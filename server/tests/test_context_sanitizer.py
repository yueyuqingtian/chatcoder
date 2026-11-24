# -*- coding: utf-8 -*-
"""上下文构造统一脱敏（零宽字符）与自定义关键词配置单测（plan-20261124-context-sanitize）。"""
from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.gateway.routers import settings as settings_router
from app.models.providers.anthropic import AnthropicProvider
from app.models.providers.antigravity import AntigravityProvider
from app.models.providers.commandcode import CommandCodeProvider
from app.models.providers.kiro import KiroProvider
from app.models.providers.openai_compatible import OpenAICompatibleProvider
from app.models.providers.ta3 import (
    Ta3Provider,
    _force_sanitize_body,
    _has_risk_signature,
    sanitize_outbound_text,
)
from app.models.providers.trae import TraeProvider
from app.models.providers.workbuddy import WorkBuddyProvider
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.context_sanitizer import (
    DEFAULT_CONTEXT_SANITIZE_KEYWORDS,
    ZERO_WIDTH_CHAR as ZW,
    force_sanitize_context_text,
    has_unsanitized_context_keyword,
    normalize_context_sanitize_keywords,
    sanitize_context_messages,
    sanitize_context_text,
    sanitize_context_tools,
)


@pytest.fixture(autouse=True)
def _reset_sanitize_settings(monkeypatch):
    """每个用例前恢复默认脱敏配置，避免互相污染。"""
    monkeypatch.setattr(settings, "context_sanitize_enabled", True)
    monkeypatch.setattr(settings, "context_sanitize_keywords", None)
    monkeypatch.setattr(settings, "ta3_sanitize_outbound", True)


# ───────────────────────── 1. 默认关键词与匹配边界 ─────────────────────────


@pytest.mark.parametrize("keyword", [
    "zcode",
    "ZCode",
    "ZCODE",
    "deepseek harness",
    "DeepSeek Harness",
    "deepseek-harness",
    "DEEPSEEK_HARNESS",
    "qoder",
    "QODER",
    "workbuddy",
    "WorkBuddy",
    "codebuddy",
    "CODEBUDDY",
    "claude code",
    "Claude Code",
    "claude-code",
    "CLAUDE_CODE",
    "codex",
    "Codex",
    "opencode",
    "OpenCode",
    "pi agent",
    "PI Agent",
    "pi-agent",
    "pi_agent",
    "pi",
    "PI",
    "deepseek",
    "DeepSeek",
    "glm",
    "GLM",
    "kimi",
    "Kimi",
    "xiaomi",
    "XiaoMi",
    "XIAOMI",
])
def test_default_keywords_wrapped_with_zero_width_case_insensitive(keyword):
    """默认 14 个关键词（含大小写与连接符变体）均在左右各自插入零宽字符，且保留原大小写。"""
    out = sanitize_context_text(f"前缀 {keyword} 后缀")
    assert out == f"前缀 {ZW}{keyword}{ZW} 后缀"


def test_default_keywords_in_chinese_and_paths_and_model_suffixes():
    """中文紧贴、目录路径、版本号后缀与驼峰标识符场景均正确命中。"""
    assert sanitize_context_text("对齐zcode和使用pi开发") == f"对齐{ZW}zcode{ZW}和使用{ZW}pi{ZW}开发"
    assert sanitize_context_text("使用GLM4与GLM-4.7与Kimi-K3与DeepSeek-V4") == (
        f"使用{ZW}GLM{ZW}4与{ZW}GLM{ZW}-4.7与{ZW}Kimi{ZW}-K3与{ZW}DeepSeek{ZW}-V4"
    )
    assert sanitize_context_text(".pi/skills 和 .zcode/config 与 CODEBUDDY.md 与 QODER.md") == (
        f".{ZW}pi{ZW}/skills 和 .{ZW}zcode{ZW}/config 与 {ZW}CODEBUDDY{ZW}.md 与 {ZW}QODER{ZW}.md"
    )
    assert sanitize_context_text("WorkBuddyProvider DeepSeekAdapter") == (
        f"{ZW}WorkBuddy{ZW}Provider {ZW}DeepSeek{ZW}Adapter"
    )


def test_longest_phrase_matched_before_sub_keyword():
    """多词短语整体包裹，不会被拆成子关键词分别包裹。"""
    assert sanitize_context_text("use deepseek harness and deepseek") == (
        f"use {ZW}deepseek harness{ZW} and {ZW}deepseek{ZW}"
    )
    assert sanitize_context_text("use pi agent and pi") == (
        f"use {ZW}pi agent{ZW} and {ZW}pi{ZW}"
    )
    # 跨行不应错误把换行吞进短语里
    assert sanitize_context_text("deepseek\nharness") == f"{ZW}deepseek{ZW}\nharness"


@pytest.mark.parametrize("safe_text", [
    "FastAPI server with api_key and openapi spec",
    "python typing pipeline compile pip install pid=123",
    "topic copy spin skip clip rapid epic happy",
    "api agent helper and glmark2 benchmark",
    "ordinary sentence without any target word",
])
def test_short_keywords_do_not_false_positive_common_words(safe_text):
    """短词 pi / glm / pi agent 不误伤普通英文单词与代码标识符。"""
    assert sanitize_context_text(safe_text) == safe_text
    assert ZW not in sanitize_context_text(safe_text)


def test_idempotency_and_adjacent_keywords():
    """多次脱敏严格幂等，相邻关键词不产生双零宽字符，单侧已有零宽字符时只补齐缺侧。"""
    once = sanitize_context_text("zcode deepseek kimi")
    twice = sanitize_context_text(once)
    assert twice == once

    assert sanitize_context_text("zcodekimi") == f"{ZW}zcode{ZW}kimi{ZW}"
    assert sanitize_context_text(f"{ZW}deepseek") == f"{ZW}deepseek{ZW}"
    assert sanitize_context_text(f"deepseek{ZW}") == f"{ZW}deepseek{ZW}"
    assert has_unsanitized_context_keyword(once) is False
    assert has_unsanitized_context_keyword("zcode") is True


# ───────────────────────── 2. 自定义关键词与开关 ─────────────────────────


def test_custom_keywords_override_defaults_and_escape_special_chars(monkeypatch):
    """用户编辑关键词后立即生效：支持自定义词、中文、短词保护及正则元字符安全转义。"""
    monkeypatch.setattr(
        settings,
        "context_sanitize_keywords",
        ["cursor", "c++", "node.js", "豆包", "ai"],
    )
    # 旧默认词不再命中，新自定义词全部命中
    assert sanitize_context_text("zcode and Cursor") == f"zcode and {ZW}Cursor{ZW}"
    assert sanitize_context_text("use C++ and Node.js with 豆包") == (
        f"use {ZW}C++{ZW} and {ZW}Node.js{ZW} with {ZW}豆包{ZW}"
    )
    # 自定义短纯英文词 ai 自动具备非字母边界保护，不误伤 main / email
    assert sanitize_context_text("main email and AI tools") == f"main email and {ZW}AI{ZW} tools"


def test_empty_keywords_or_disabled_switch_is_noop(monkeypatch):
    """用户清空关键词列表或关闭开关时不做脱敏。"""
    monkeypatch.setattr(settings, "context_sanitize_keywords", [])
    assert sanitize_context_text("zcode deepseek kimi") == "zcode deepseek kimi"

    monkeypatch.setattr(settings, "context_sanitize_keywords", None)
    monkeypatch.setattr(settings, "context_sanitize_enabled", False)
    assert sanitize_context_text("zcode deepseek kimi") == "zcode deepseek kimi"
    # 强制路径仍生效
    assert force_sanitize_context_text("zcode") == f"{ZW}zcode{ZW}"


def test_normalize_keywords_cleans_dedupes_and_splits():
    assert normalize_context_sanitize_keywords(
        [f"  {ZW}ZCode{ZW}  ", "zcode", "deepseek   harness", "", "   ", "Kimi"]
    ) == ["ZCode", "deepseek harness", "Kimi"]
    assert normalize_context_sanitize_keywords("cursor, windsurf，trae\nCursor") == [
        "cursor", "windsurf", "trae",
    ]
    assert normalize_context_sanitize_keywords([]) == []


# ───────────────────────── 3. 消息结构与工具描述安全边界 ─────────────────────────


def test_sanitize_messages_preserves_originals_images_and_thinking_signatures():
    """不原地修改原消息；不破坏图片 base64、Anthropic 思考签名及协议字段名。"""
    fake_b64 = "data:image/png;base64,AAAdeepseekBBBpiCCCglmDDDworkbuddyEEE"
    orig_msgs = [
        ChatMessage(role="system", content="遵循 CODEBUDDY.md 和 QODER.md"),
        ChatMessage(
            role="user",
            content="看看 zcode",
            content_blocks=[
                {"type": "text", "text": "附件说明含 kimi"},
                {"type": "image_url", "image_url": {"url": fake_b64}},
            ],
        ),
        ChatMessage(
            role="assistant",
            content="对比 deepseek 和 glm",
            reasoning_content="先查 opencode 和 xiaomi",
            thinking_blocks=[{"type": "thinking", "thinking": "keep deepseek signed", "signature": "sig-zcode-1"}],
            tool_calls=[{
                "id": "call_pi_1",
                "name": "fs_read",
                "thoughtSignature": "sig-kimi",
                "arguments": {"path": "workbuddy.py", "note": "codex", "nested": ["pi agent"]},
            }],
        ),
        ChatMessage(role="tool", tool_call_id="call_pi_1", content="read deepseek harness ok"),
    ]

    sanitized = sanitize_context_messages(orig_msgs)

    # 原始对象零污染
    assert orig_msgs[0].content == "遵循 CODEBUDDY.md 和 QODER.md"
    assert orig_msgs[1].content == "看看 zcode"
    assert orig_msgs[2].tool_calls[0]["arguments"]["path"] == "workbuddy.py"

    # 脱敏副本各上下文文本全部命中
    assert sanitized[0].content == f"遵循 {ZW}CODEBUDDY{ZW}.md 和 {ZW}QODER{ZW}.md"
    assert sanitized[1].content == f"看看 {ZW}zcode{ZW}"
    assert sanitized[1].content_blocks[0]["text"] == f"附件说明含 {ZW}kimi{ZW}"
    # 图片 base64 逐字节无损
    assert sanitized[1].content_blocks[1]["image_url"]["url"] == fake_b64
    assert sanitized[2].content == f"对比 {ZW}deepseek{ZW} 和 {ZW}glm{ZW}"
    assert sanitized[2].reasoning_content == f"先查 {ZW}opencode{ZW} 和 {ZW}xiaomi{ZW}"
    # 带签名的 thinking_blocks 逐字节无损
    assert sanitized[2].thinking_blocks == orig_msgs[2].thinking_blocks
    # tool_call id/name/thoughtSignature 与参数键名无损，参数值已脱敏
    tc = sanitized[2].tool_calls[0]
    assert tc["id"] == "call_pi_1"
    assert tc["name"] == "fs_read"
    assert tc["thoughtSignature"] == "sig-kimi"
    assert tc["arguments"] == {
        "path": f"{ZW}workbuddy{ZW}.py",
        "note": f"{ZW}codex{ZW}",
        "nested": [f"{ZW}pi agent{ZW}"],
    }
    assert sanitized[3].content == f"read {ZW}deepseek harness{ZW} ok"


def test_sanitize_tools_only_touches_descriptions_not_contract_fields():
    """工具脱敏仅处理工具级与参数级 description，保持 name/enum/properties 键名完整。"""
    tools = [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search via workbuddy or claude code engine",
            "parameters": {
                "type": "object",
                "properties": {
                    "engine": {
                        "type": "string",
                        "enum": ["workbuddy", "bing"],
                        "description": "workbuddy=默认(WorkBuddy 账号)",
                    },
                    "pi_mode": {
                        "type": "boolean",
                        "description": "enable pi mode",
                    },
                },
                "required": ["engine"],
            },
        },
    }]

    out = sanitize_context_tools(tools)
    # 原始 schema 未被修改
    assert tools[0]["function"]["description"] == "Search via workbuddy or claude code engine"

    fn = out[0]["function"]
    assert fn["name"] == "web_search"
    assert fn["description"] == f"Search via {ZW}workbuddy{ZW} or {ZW}claude code{ZW} engine"
    props = fn["parameters"]["properties"]
    assert set(props.keys()) == {"engine", "pi_mode"}
    assert props["engine"]["enum"] == ["workbuddy", "bing"]
    assert props["engine"]["description"] == f"{ZW}workbuddy{ZW}=默认({ZW}WorkBuddy{ZW} 账号)"
    assert props["pi_mode"]["description"] == f"enable {ZW}pi{ZW} mode"


# ───────────────────────── 4. 全 8 个 Provider 出站覆盖 ─────────────────────────


def _sample_messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="sys mentions zcode and qoder"),
        ChatMessage(role="user", content="compare deepseek, glm, kimi, xiaomi and pi agent"),
    ]


def _sample_tools() -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "use workbuddy search",
            "parameters": {
                "type": "object",
                "properties": {
                    "engine": {
                        "type": "string",
                        "enum": ["workbuddy", "bing"],
                        "description": "WorkBuddy engine",
                    }
                },
            },
        },
    }]


def test_all_providers_sanitize_outbound_context_without_corrupting_model_names():
    """覆盖全部 8 个 Provider 的请求构造：上下文文本与工具描述脱敏，model 名绝不被加零宽字符。"""
    msgs = _sample_messages()
    tools = _sample_tools()

    # 1. OpenAICompatibleProvider
    oai = OpenAICompatibleProvider(api_key="k", base_url="https://api.example/v1", model="deepseek-v4")
    oai_msgs = oai._convert_messages(msgs)
    assert f"{ZW}zcode{ZW}" in oai_msgs[0]["content"]
    assert f"{ZW}xiaomi{ZW}" in oai_msgs[1]["content"]

    # 2. AnthropicProvider
    ant = AnthropicProvider(api_key="k", base_url="https://api.example", model="claude-sonnet")
    ant_body = ant._build_payload(ChatRequest(messages=msgs, model="kimi-k3", tools=tools), stream=True)
    assert ant_body["model"] == "kimi-k3"
    assert f"{ZW}zcode{ZW}" in ant_body["system"]
    assert f"{ZW}glm{ZW}" in json.dumps(ant_body["messages"], ensure_ascii=False)
    assert ant_body["tools"][0]["description"] == f"use {ZW}workbuddy{ZW} search"

    # 3. WorkBuddyProvider
    wb = WorkBuddyProvider(api_key="k", base_url="https://copilot.tencent.com", model="deepseek-v4-pro")
    wb_body = wb._build_body(ChatRequest(messages=msgs, model="deepseek-v4-pro", tools=tools))
    assert wb_body["model"] == "deepseek-v4-pro"
    assert f"{ZW}zcode{ZW}" in wb_body["messages"][0]["content"]
    assert f"{ZW}pi agent{ZW}" in wb_body["messages"][1]["content"]
    assert wb_body["tools"][0]["function"]["description"] == f"use {ZW}workbuddy{ZW} search"
    assert wb_body["tools"][0]["function"]["parameters"]["properties"]["engine"]["enum"] == ["workbuddy", "bing"]

    # 4. TraeProvider（lite + work）
    trae = TraeProvider(
        api_key="k",
        base_url="https://trae-api-cn.mchost.guru",
        model="DeepSeek-V4-Flash-Official",
        meta={"config_name": "DeepSeek-V4-Flash-Official", "model_name": "DeepSeek-V4-Flash-Official"},
    )
    trae_lite = trae._build_body(ChatRequest(messages=msgs, model="DeepSeek-V4-Flash-Official"))
    trae_work = trae._build_work_body(ChatRequest(messages=msgs, model="DeepSeek-V4-Flash-Official"))
    assert trae_lite["model_name"] == "DeepSeek-V4-Flash-Official"
    assert trae_work["model_name"] == "DeepSeek-V4-Flash-Official"
    assert f"{ZW}kimi{ZW}" in trae_lite["query"]
    assert f"{ZW}deepseek{ZW}" in trae_work["user_input"]["data"]["content"]

    # 5. CommandCodeProvider
    cc = CommandCodeProvider(api_key="k", model="deepseek/deepseek-v4-flash")
    cc_body = cc._build_payload(ChatRequest(messages=msgs, model="deepseek/deepseek-v4-flash", tools=tools))
    assert cc_body["params"]["model"] == "deepseek/deepseek-v4-flash"
    assert f"{ZW}qoder{ZW}" in cc_body["params"]["system"]
    assert f"{ZW}xiaomi{ZW}" in json.dumps(cc_body["params"]["messages"], ensure_ascii=False)
    assert cc_body["params"]["tools"][0]["description"] == f"use {ZW}workbuddy{ZW} search"

    # 6. KiroProvider
    kiro = KiroProvider(api_key="k", base_url="https://kiro.example", model="claude-sonnet-4")
    kiro_body = kiro._build_payload(ChatRequest(messages=msgs, model="claude-sonnet-4", tools=tools))
    cur = kiro_body["conversationState"]["currentMessage"]["userInputMessage"]
    assert f"{ZW}zcode{ZW}" in cur["content"]
    assert f"{ZW}pi agent{ZW}" in cur["content"]
    assert cur["userInputMessageContext"]["tools"][0]["toolSpecification"]["description"] == (
        f"use {ZW}workbuddy{ZW} search"
    )

    # 7. AntigravityProvider
    ag = AntigravityProvider(
        api_key="k",
        base_url="https://daily-cloudcode-pa.googleapis.com",
        model="gemini-3-flash",
        meta={"project": "p1"},
    )
    ag_body = ag._build_payload(ChatRequest(messages=msgs, model="gemini-3-flash", tools=tools))
    inner = ag_body["request"]
    assert ag_body["model"] == "gemini-3-flash"
    assert f"{ZW}zcode{ZW}" in inner["systemInstruction"]["parts"][0]["text"]
    assert f"{ZW}deepseek{ZW}" in inner["contents"][0]["parts"][0]["text"]
    assert inner["tools"][0]["functionDeclarations"][0]["description"] == f"use {ZW}workbuddy{ZW} search"

    # 8. Ta3Provider（OpenAI + Anthropic + 兜底重试保护 model）
    ta3_oai = Ta3Provider(api_key="llm-k", base_url="http://test.local/ai/dispatch/v2", model="deepseek-v4")
    ta3_oai_body = ta3_oai._build_openai_body(ChatRequest(messages=msgs, model="deepseek-v4"), [])
    assert ta3_oai_body["model"] == "deepseek-v4"
    assert f"{ZW}zcode{ZW}" in ta3_oai_body["messages"][0]["content"]
    assert f"{ZW}xiaomi{ZW}" in ta3_oai_body["messages"][1]["content"]

    ta3_ant = Ta3Provider(
        api_key="llm-k",
        base_url="http://test.local/ai/dispatch/v2/anthropic",
        model="kimi-k3",
        meta={"anthropic": True},
    )
    ta3_ant_body = ta3_ant._build_anthropic_body(ChatRequest(messages=msgs, model="kimi-k3"), [])
    assert ta3_ant_body["model"] == "kimi-k3"
    assert f"{ZW}qoder{ZW}" in json.dumps(ta3_ant_body["system"], ensure_ascii=False)
    assert f"{ZW}glm{ZW}" in json.dumps(ta3_ant_body["messages"], ensure_ascii=False)


def test_ta3_force_sanitize_body_and_risk_detection_ignore_model_metadata():
    """ta3 403 兜底重试绝不能破坏 body['model']（如 deepseek-v4），且不把 model 误判为风险特征。"""
    clean_body = {"model": "deepseek-v4", "messages": [{"role": "user", "content": "hello"}]}
    assert _has_risk_signature(clean_body) is False

    already_sanitized = {
        "model": "kimi-k3",
        "messages": [{"role": "user", "content": f"use {ZW}zcode{ZW}"}],
    }
    assert _has_risk_signature(already_sanitized) is False

    raw_body = {
        "model": "deepseek-v4",
        "system": "check qoder",
        "messages": [
            {"role": "user", "content": "you are claude code and use xiaomi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": json.dumps({"path": "workbuddy.py"})},
                }],
            },
        ],
    }
    assert _has_risk_signature(raw_body) is True
    fixed = _force_sanitize_body(raw_body)
    assert fixed["model"] == "deepseek-v4"
    assert fixed["system"] == f"check {ZW}qoder{ZW}"
    assert "you are ta+3 agent" in fixed["messages"][0]["content"]
    assert f"{ZW}xiaomi{ZW}" in fixed["messages"][0]["content"]
    re_args = json.loads(fixed["messages"][1]["tool_calls"][0]["function"]["arguments"])
    assert re_args == {"path": f"{ZW}workbuddy{ZW}.py"}

    # ta3 专有规则优先于零宽脱敏：you are claude code → you are ta+3 agent，同时其他词加零宽
    combined = sanitize_outbound_text("you are claude code, built with zcode and deepseek")
    assert combined == f"you are ta+3 agent, built with {ZW}zcode{ZW} and {ZW}deepseek{ZW}"


# ───────────────────────── 5. 全局设置持久化与启动恢复 ─────────────────────────


async def test_global_settings_persist_and_reload_context_sanitize_keywords(tmp_path, monkeypatch):
    """设置中心 GET/PUT 可读写脱敏开关与自定义词表，支持 null 恢复默认与重启恢复。"""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(settings_router, "_USER_CONFIG_PATH", cfg_path)

    # 默认态
    initial = settings_router._global_settings_out(settings_router._load_config())
    assert initial.context_sanitize_enabled is True
    assert initial.context_sanitize_keywords == list(DEFAULT_CONTEXT_SANITIZE_KEYWORDS)
    assert initial.default_context_sanitize_keywords == list(DEFAULT_CONTEXT_SANITIZE_KEYWORDS)

    # 保存自定义词表与关闭开关
    res = await settings_router.set_global_settings(settings_router.GlobalSettingsIn(
        context_sanitize_enabled=False,
        context_sanitize_keywords=["  Cursor ", "cursor", "豆包", ""],
    ))
    assert res.context_sanitize_enabled is False
    assert res.context_sanitize_keywords == ["Cursor", "豆包"]
    assert settings.context_sanitize_enabled is False
    assert settings.context_sanitize_keywords == ["Cursor", "豆包"]

    # 模拟进程重启后从 config.json 恢复
    settings.context_sanitize_enabled = True
    settings.context_sanitize_keywords = None
    settings_router.load_persisted_workspace()
    assert settings.context_sanitize_enabled is False
    assert settings.context_sanitize_keywords == ["Cursor", "豆包"]

    # 允许显式保存空列表（不脱敏任何关键词）
    empty_res = await settings_router.set_global_settings(settings_router.GlobalSettingsIn(
        context_sanitize_enabled=True,
        context_sanitize_keywords=[],
    ))
    assert empty_res.context_sanitize_keywords == []
    assert settings.context_sanitize_keywords == []

    # 传 null 恢复跟随内置默认词表
    reset_res = await settings_router.set_global_settings(settings_router.GlobalSettingsIn(
        context_sanitize_keywords=None,
    ))
    assert reset_res.context_sanitize_keywords == list(DEFAULT_CONTEXT_SANITIZE_KEYWORDS)
    assert settings.context_sanitize_keywords is None
    persisted = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "context_sanitize_keywords" not in persisted

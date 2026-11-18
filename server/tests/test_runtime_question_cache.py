"""plan-20261118-runtime-question-cache：空提问、回答附件出站与缓存使用回归。"""
import asyncio
import base64
import json

import pytest
from PIL import Image

from app.core.config import settings
from app.models.providers.anthropic import AnthropicProvider
from app.models.providers.commandcode import CommandCodeProvider
from app.models.providers.kiro import KiroProvider
from app.models.providers.openai_compatible import OpenAICompatibleProvider
from app.models.providers.ta3 import Ta3Provider
from app.models.providers.trae import TraeProvider
from app.models.providers.usage_normalization import anthropic_usage, openai_usage
from app.models.providers.workbuddy import WorkBuddyProvider
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.approval import ApprovalManager
from app.orchestration.context_manager import ContextBundle
from app.orchestration.history_sequence import validate_tool_sequence
from app.orchestration.question_attachments import project_answer_images
from app.orchestration.tools.ask_user import AskUserQuestionTool, _normalize_questions, validate_question_reply


@pytest.mark.parametrize("bad", ["  \n ", 123, None, {}])
async def test_mixed_invalid_question_never_enters_approval(bad, monkeypatch):
    from app.orchestration import approval

    async def unexpected(**_):
        pytest.fail("空题干进入等待")

    monkeypatch.setattr(approval.approval_manager, "request", unexpected)
    result = await AskUserQuestionTool().run(
        {"questions": [{"question": "有效开放题"}, {"question": bad}]}, None,
    )
    assert result.error_code == "ARG_INVALID"
    assert "第 2 题" in result.error


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    file_id = "0123456789abcdef"
    folder = tmp_path / file_id
    folder.mkdir()
    Image.new("RGB", (8, 8), "#516489").save(folder / "示例.png")
    text_folder = tmp_path / "abcdef0123456789"
    text_folder.mkdir()
    (text_folder / "说明.txt").write_text("本地测试文本", encoding="utf-8")
    return file_id


def test_attachment_only_is_validated_against_uploads(uploads):
    questions = _normalize_questions([
        {"question": "请附上图片", "options": [" ", None, {}, {"label": " "}], "allow_custom": False},
        {"question": "选项", "options": [" A ", "A", {"label": "B", "description": "补充"}], "multiple": True},
    ])
    assert questions[0]["options"] == [] and questions[0]["allow_custom"]
    assert questions[1]["options"] == ["A", "B — 补充"]
    ref = {"file_id": uploads, "filename": "示例.png", "path": "false", "type": "file"}
    answers, files = validate_question_reply(questions, {"1": ["A", "补充"]}, {"0": [ref, ref]})
    assert answers == {"0": "", "1": ["A", "补充"]}
    assert len(files) == 1 and files[0]["path"] == f"{uploads}/示例.png"
    assert files[0]["question_index"] == 0 and files[0]["type"] == "image"
    with pytest.raises(ValueError, match="文件名无效"):
        validate_question_reply(questions, {"1": ["A"]}, {"0": [{**ref, "filename": "../说明.txt"}]})
    with pytest.raises(ValueError, match="回答第 1 题"):
        validate_question_reply(questions, {"1": ["A"]}, {})


async def test_question_resolution_keeps_first_answer():
    manager = ApprovalManager()
    manager.set_on_request(lambda *_: None)
    detail = {"kind": "question"}
    waiter = asyncio.create_task(manager.request(approval_id="a", detail=detail))
    await asyncio.sleep(0)
    assert manager.resolve("a", True, {"0": "第一次"})
    assert not manager.resolve("a", True, {"0": "第二次"})
    assert await waiter is True
    assert detail["answer"] == {"0": "第一次"}


@pytest.mark.parametrize("protocol", [
    "openai", "gemini", "anthropic", "commandcode", "kiro", "trae", "workbuddy",
    "ta3-openai", "ta3-anthropic", "ta3-deepseek",
])
async def test_answer_attachments_reach_provider_after_complete_tool_batch(protocol, uploads, tmp_path):
    _, files = validate_question_reply([{"question": "请补文件"}], {}, {
        "0": [{"file_id": uploads, "filename": "示例.png"},
              {"file_id": "abcdef0123456789", "filename": "说明.txt"}],
    })
    calls = [{"id": "ask_1", "name": "ask_user_question", "arguments": {"questions": [{"question": "请补文件"}]}},
             {"id": "read_1", "name": "fs_read", "arguments": {"path": "README.md"}}]
    original = [
        ChatMessage(role="system", content="稳定规则"),
        ChatMessage(role="user", content="请检查"),
        ChatMessage(role="assistant", tool_calls=calls),
        ChatMessage(role="tool", tool_call_id="ask_1", name="ask_user_question", answer_attachments=files,
                    content=f"回答附件 {uploads}/示例.png abcdef0123456789/说明.txt"),
        ChatMessage(role="tool", tool_call_id="read_1", content="读取完成"),
    ]
    messages = original if protocol == "trae" else await project_answer_images(original)
    validate_tool_sequence(messages)
    if protocol != "trae":
        assert [m.role for m in messages[-3:]] == ["tool", "tool", "user"]
    request = ChatRequest(messages=messages, model="", session_id="1")
    key = {"api_key": "test-key", "base_url": "http://mock.invalid/v1"}
    if protocol == "anthropic":
        body = AnthropicProvider(model="claude-sonnet", **key)._build_payload(request, stream=True)
    elif protocol == "commandcode":
        body = CommandCodeProvider(model="command", **key)._build_payload(request)
    elif protocol == "kiro":
        body = KiroProvider(model="claude-sonnet", **key)._build_payload(request)
    elif protocol == "trae":
        body = TraeProvider(model="test-model", **key)._build_body(request)
    elif protocol == "workbuddy":
        body = WorkBuddyProvider(model="test-model", **key)._build_body(request)
    elif protocol.startswith("ta3-"):
        provider = Ta3Provider(model="test-model", **key, meta={
            "anthropic": protocol == "ta3-anthropic", "deepseek": protocol == "ta3-deepseek",
        })
        build = provider._build_anthropic_body if protocol == "ta3-anthropic" else provider._build_openai_body
        body = build(request, [])
        await provider._close_client()
    else:
        provider = OpenAICompatibleProvider(model="gemini-2.5-pro" if protocol == "gemini" else "gpt-4o", **key)
        body = provider._convert_messages(messages)
    wire = json.dumps(body, ensure_ascii=False)
    assert "ask_1" in wire and "read_1" in wire
    assert "示例.png" in wire and "说明.txt" in wire
    if protocol == "kiro":
        assert body["conversationState"]["currentMessage"]["userInputMessage"]["images"]
    else:
        image = base64.b64encode((tmp_path / uploads / "示例.png").read_bytes()).decode()
        assert (image in wire) == (protocol != "trae")


def test_turn_context_changes_only_the_tail():
    history = [ChatMessage(role="user", content="历史问题"), ChatMessage(role="assistant", content="历史回答")]
    first = ContextBundle("稳定规则", history=history, turn_parts=["本轮记忆甲"], instruction="请求甲").to_messages()
    second = ContextBundle("稳定规则", history=history, turn_parts=["本轮记忆乙"], instruction="请求乙").to_messages()
    assert first[:-1] == second[:-1]
    assert "本轮记忆乙" in second[-1].content


def test_provider_usage_distinguishes_zero_missing_and_anthropic_cache_write():
    missing = openai_usage({"prompt_tokens": 100})
    zero = openai_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}})
    assert not missing.cache_read_reported and zero.cache_read_reported
    assert openai_usage({"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}).cached_input_tokens == 80
    assert openai_usage({"promptTokenCount": 1200, "cachedContentTokenCount": 1024}).cached_input_tokens == 1024
    usage = anthropic_usage(
        {"input_tokens": 100, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 700, "output_tokens": 20}
    )
    assert (usage.prompt_tokens, usage.total_tokens, usage.cached_input_tokens) == (1000, 1020, 700)


def test_native_cache_controls_follow_protocol_and_session():
    request = ChatRequest(model="", session_id="session-01", messages=[
        ChatMessage(role="system", content="稳定规则"),
        ChatMessage(role="user", content="首问"),
        ChatMessage(role="assistant", content="首答"),
        ChatMessage(role="user", content="追问"),
    ], tools=[AskUserQuestionTool().function_schema()])
    for url, enabled in (("https://api.openai.com/v1", True), ("https://api.openai.com.example/v1", False)):
        provider = OpenAICompatibleProvider(model="gpt-4o", api_key="test-key", base_url=url)
        first, retry = {}, {}
        provider._apply_prompt_cache(first, request)
        provider._apply_prompt_cache(retry, request.model_copy(update={"message_id": "retry"}))
        assert first == retry
        assert ("prompt_cache_key" in first.get("extra_body", {})) is enabled
    native = AnthropicProvider(model="claude-sonnet", api_key="test-key", base_url="https://api.anthropic.com/v1")
    body = native._build_payload(request, stream=True)
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert sum("cache_control" in block for row in body["messages"] for block in row["content"]) == 2


async def test_concurrent_settings_patches_preserve_both_fields(tmp_path, monkeypatch):
    from app.gateway.routers import settings as routes

    path = tmp_path / "config.json"
    monkeypatch.setattr(routes, "_USER_CONFIG_PATH", path)
    await asyncio.gather(
        routes.set_global_settings(routes.GlobalSettingsIn(global_rules="保留全局规则")),
        routes.set_scope_rules("project", "demo", routes.ScopeRulesIn(rules="保留项目规则")),
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["global_rules"] == "保留全局规则"
    assert data["rules_project_demo"]["rules"] == "保留项目规则"


async def test_failed_config_write_does_not_publish_runtime_changes(monkeypatch):
    from app.gateway.routers import settings as routes

    def failed(_):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(settings, "agent_tool_temperature", 0.3)
    monkeypatch.setattr(routes, "_load_config", lambda: {})
    monkeypatch.setattr(routes, "_save_config", failed)
    with pytest.raises(OSError, match="模拟磁盘写入失败"):
        await routes.set_global_settings(routes.GlobalSettingsIn(agent_tool_temperature=0.9))
    assert settings.agent_tool_temperature == 0.3


async def test_new_question_attachments_survive_previous_projection(uploads, tmp_path):
    """历史里已有投影消息时，后一批提问附件仍要注入图片（此前是整段跳过）。"""
    _, files = validate_question_reply([{"question": "请补文件"}], {}, {
        "0": [{"file_id": uploads, "filename": "示例.png"}],
    })
    calls = [
        {"id": "ask_1", "name": "ask_user_question", "arguments": {"questions": [{"question": "请补文件"}]}},
        {"id": "ask_2", "name": "ask_user_question", "arguments": {"questions": [{"question": "再来一张"}]}},
    ]
    messages = [
        ChatMessage(role="system", content="稳定规则"),
        ChatMessage(role="user", content="请检查"),
        ChatMessage(role="assistant", tool_calls=calls),
        ChatMessage(role="tool", tool_call_id="ask_1", name="ask_user_question",
                    answer_attachments=files, content="回答附件 示例.png"),
        ChatMessage(role="user", content="用户提问回答附带的文件（上一轮已投影）",
                    context_kind="question_attachment"),
        ChatMessage(role="tool", tool_call_id="ask_2", name="ask_user_question",
                    answer_attachments=files, content="回答附件 示例.png"),
    ]
    projected = await project_answer_images(messages)
    assert [m.context_kind for m in projected].count("question_attachment") == 2
    last = projected[-1]
    assert last.role == "user" and last.context_kind == "question_attachment"
    assert any(b.get("type") == "image_url" for b in (last.content_blocks or []))

    # 重复投影必须幂等：上一轮已投影的批次不再追加
    again = await project_answer_images(projected)
    assert len(again) == len(projected)
    assert [m.context_kind for m in again].count("question_attachment") == 2

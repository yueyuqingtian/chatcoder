"""plan-20261106-ta3-080：官方构造样本、技能快照及压缩后消息语义。"""
import json
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import select

import pytest

from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.ta3_skill_snapshots import snapshot, replay, reasoning_passthrough, capture
from app.orchestration.token_counter import estimate_messages_tokens
from app.persistence.database import async_session_factory
from app.persistence.models.message import Session, Message
from app.persistence.models.turn import Turn
from app.persistence.models.skill import Skill

FIXTURE = json.loads((Path(__file__).parent / "fixtures/ta3_080/official_contract.json").read_text(encoding="utf-8-sig"))


@pytest.mark.parametrize("name", ["zai", "deepseek", "openai", "anthropic"])
async def test_official_080_request_fixture(name):
    scenario = FIXTURE["scenarios"][name]
    raw = scenario["model"]
    p = Ta3Provider(api_key=raw["apiKey"], base_url=raw["apiBase"], model=raw["model"], meta={
        "provider": raw["provider"], "orgId": "app-fixture", "anthropic": name == "anthropic",
        "completionOptions": raw["completionOptions"],
    })
    req = ChatRequest(messages=[ChatMessage(role="user", content="hello")], model=raw["model"], session_id="session-fixture")
    try:
        with patch("app.models.providers.ta3.get_workspace_fingerprint", return_value="ws-fixture"):
            assert p._base_headers(workspace_dir="fixture") == scenario["headers"]
        if name == "anthropic":
            assert p._build_anthropic_body(req, []) == scenario["stream"]
        else:
            expected = dict(scenario["stream"])
            if name == "deepseek":
                # 官方 DeepSeek 专用适配器在 OpenAI 基础体外追加此字段。
                expected["stream_options"] = {"include_usage": True}
            assert p._build_openai_body(req, []) == expected
            assert p._build_openai_body(req, [], text=True) == scenario["text"]
    finally:
        await p._close_client()


def test_reasoning_matches_official_and_preserves_source():
    expected = FIXTURE["reasoning"]
    original = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in expected]
    assert reasoning_passthrough(original) == expected
    assert "reasoning_content" not in original[1]
    assert reasoning_passthrough([{"role": "user", "content": "u"}, {"role": "assistant", "content": "plain"}])[-1] == {"role": "assistant", "content": "plain"}


def test_snapshot_replay_dedup_change_unavailable_and_trim_budget():
    a = snapshot("docx", "loaded", "OLD RULES " * 300)
    b = snapshot("docx", "loaded", "NEW RULES")
    unavailable = snapshot("docx", "unavailable", "当前技能不可用")
    messages = [ChatMessage(role="user", content=str(i), skill_load_snapshots=[x])
                for i, x in enumerate([a, a, b, unavailable])]
    expanded = replay(messages)
    assert "OLD RULES" in expanded[0].content and "OLD RULES" not in expanded[1].content
    assert "NEW RULES" in expanded[2].content and "unavailable" in expanded[3].content
    assert replay(expanded) == expanded
    assert "OLD RULES" in replay(messages[1:])[0].content
    assert estimate_messages_tokens(messages[1:]) > estimate_messages_tokens([
        m.model_copy(update={"skill_load_snapshots": None}) for m in messages[1:]])
    assert messages[0].content == "0"


async def test_capture_persists_bytes_across_file_change_and_retry():
    async with async_session_factory() as db:
        db.add(Session(id=1))
        await db.flush()
        db.add(Turn(id=1, session_id=1, status="running"))
        db.add(Message(id=1, session_id=1, turn_id=1, sender_type="user", msg_type="text", content={"text": "创建 Word 文档"}))
        db.add(Skill(name="docx", display_name="文档", content="RULES AT FIRST SEND", is_active=True))
        await db.commit()
        turn = await db.get(Turn, 1)
        first = await capture(db, 1, turn, "创建 Word 文档", [])
        assert first[0]["status"] == "loaded" and "RULES AT FIRST SEND" in first[0]["content"]
        skill = (await db.execute(select(Skill).where(Skill.name == "docx"))).scalars().first()
        skill.content = "MODIFIED LATER"
        await db.commit()
        assert await capture(db, 1, turn, "创建 Word 文档", []) == first
        stored = await db.get(Message, 1, populate_existing=True)
        assert stored.content["skill_load_snapshots"] == first
        db.add(Turn(id=2, session_id=1, status="running"))
        db.add(Message(id=2, session_id=1, turn_id=2, sender_type="user", msg_type="text", content={"text": "继续"}))
        skill.is_active = False
        await db.commit()
        second = await capture(db, 1, await db.get(Turn, 2), "继续", [])
        assert second[0]["status"] == "unavailable"


async def test_unbound_project_has_global_memory_only():
    from app.orchestration.context_manager import _build_ta3_memory_injection
    async with async_session_factory() as db:
        text = await _build_ta3_memory_injection(db, None, project_id=None, workspace="")
    assert "当前会话未绑定项目，只能读写全局用户偏好和反馈" in text
    assert "- 项目目录" not in text and "## 项目记忆索引" not in text


def test_merged_user_and_checkpoint_keep_snapshot():
    from app.orchestration.compaction import normalize_tool_sequence
    from app.orchestration.subagent import _dump_message
    a = snapshot("docx", "loaded", "RULES")
    messages = [ChatMessage(role="user", content="u", skill_load_snapshots=[a]), ChatMessage(role="user", content="next")]
    normalized = normalize_tool_sequence(messages)
    assert any(m.skill_load_snapshots for m in normalized)
    assert _dump_message(messages[0])["skill_load_snapshots"] == [a]


async def test_custom_model_base_not_rewritten():
    from app.models.registry import _resolve_ta3_base_url
    from types import SimpleNamespace
    assert _resolve_ta3_base_url(SimpleNamespace(base_url="https://custom.test/openai/v1"),
                                 SimpleNamespace(base_url="https://platform.test/ta3api"), {}) == "https://custom.test/openai/v1"

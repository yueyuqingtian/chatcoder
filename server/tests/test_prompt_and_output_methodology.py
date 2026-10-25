"""本轮体验与质量优化的补充单测：字节预算呈现、错误类别、Anthropic 动态提醒位置、
CJK 估算、压缩切点配对、模式能力段与提示词的稳定性。"""
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.providers.anthropic import AnthropicProvider
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration import output_paging
from app.orchestration.output_paging import present_output_bytes
from app.orchestration.prompts.main import (
    MAIN_SYSTEM_PROMPT,
    _CAPABILITY_TOOLS,
    _MODE_NOTES,
    _mode_capability_section,
    build_main_system_prompt,
)
from app.orchestration.token_counter import _approx_token_count
from app.orchestration.tools.base import ERROR_CODES, ERROR_HINTS, ToolResult


# ───────────────── 字节预算呈现 ─────────────────


@pytest.fixture
def spill_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path), raising=False)
    return tmp_path


def test_under_budget_is_returned_unchanged(spill_dir):
    text = "短输出"
    assert present_output_bytes(text, 1024, session_id=1, tag="t") == text


def test_over_budget_keeps_head_and_tail_and_spills_full_text(spill_dir):
    text = "A" * 20000 + "Z" * 20000  # 尾部报错信息
    out = present_output_bytes(text, 8000, session_id=1, tag="terminal", tail_bytes=3000)
    assert out.startswith("A")
    assert "Z" * 2000 in out  # 尾部保留，报错信息不丢
    assert "A" * 20000 not in out  # 中间部分未整段保留
    assert len(out.encode("utf-8")) < 8000 + 400
    assert "完整内容已保存" in out and "read_attachment" in out
    saved = list((spill_dir / output_paging.SPILL_DIRNAME).rglob("*.txt"))
    assert saved and saved[0].read_text(encoding="utf-8") == text


def test_multibyte_text_is_not_cut_mid_character(spill_dir):
    text = "中" * 9000  # 每字 3 字节
    out = present_output_bytes(text, 4000, session_id=1, tag="t")
    head = out.split("\n\n")[0]
    assert all(ch == "中" for ch in head)  # 没有出现破碎字符
    assert len(head.encode("utf-8")) <= 4000


def test_tool_presentation_budget_per_tool(monkeypatch, spill_dir):
    from app.orchestration.agent_loop import _present_tool_output

    monkeypatch.setattr(settings, "tool_output_bytes_grep", 500, raising=False)
    monkeypatch.setattr(settings, "tool_output_bytes_default", 100000, raising=False)
    big = "x" * 3000
    assert _present_tool_output(big, tool_name="fs_grep", session_id=1, call_key="c") != big
    assert _present_tool_output(big, tool_name="other_tool", session_id=1, call_key="c") == big


def test_fs_read_output_is_never_head_truncated(spill_dir):
    from app.orchestration.agent_loop import _present_tool_output

    big = "y" * 50000
    assert _present_tool_output(big, tool_name="fs_read", session_id=1, call_key="c") == big


# ───────────────── 错误类别 ─────────────────


def test_every_error_code_has_a_recovery_hint():
    assert set(ERROR_CODES) == set(ERROR_HINTS)
    for code in ERROR_CODES:
        assert ERROR_HINTS[code].strip()


def test_failure_text_for_each_code_mentions_code_and_hint():
    for code in ERROR_CODES:
        text = ToolResult(ok=False, output="", error="boom", error_code=code).failure_text()
        assert f"[错误类别: {code}]" in text
        assert ERROR_HINTS[code] in text


def test_custom_hint_overrides_default():
    r = ToolResult(ok=False, output="", error="x", error_code="NOT_FOUND", hint="换一个目录试试")
    assert "换一个目录试试" in r.failure_text()
    assert ERROR_HINTS["NOT_FOUND"] not in r.failure_text()


# ───────────────── Anthropic 动态提醒位置 ─────────────────


def _anthropic():
    return AnthropicProvider(api_key="k", base_url="https://api.anthropic.com/v1", model="claude-sonnet-4-6")


def test_mid_dialogue_system_message_stays_in_place_as_user_reminder():
    req = ChatRequest(model="claude-sonnet-4-6", messages=[
        ChatMessage(role="system", content="SYSTEM-HEAD"),
        ChatMessage(role="developer", content="DEV-CTX"),
        ChatMessage(role="user", content="请修复 bug"),
        ChatMessage(role="assistant", content="好的"),
        ChatMessage(role="system", content="LANG-REMINDER"),
    ])
    payload = _anthropic()._build_payload(req, stream=False)
    assert "SYSTEM-HEAD" in payload["system"] and "DEV-CTX" in payload["system"]
    assert "LANG-REMINDER" not in payload["system"]
    last = payload["messages"][-1]
    assert last["role"] == "user"
    assert "<system-reminder>" in last["content"] and "LANG-REMINDER" in last["content"]


def test_leading_system_messages_still_merge_to_top_level():
    req = ChatRequest(model="claude-sonnet-4-6", messages=[
        ChatMessage(role="system", content="A"),
        ChatMessage(role="developer", content="B"),
        ChatMessage(role="user", content="hi"),
    ])
    payload = _anthropic()._build_payload(req, stream=False)
    assert payload["system"] == "A\n\nB"
    assert [m["role"] for m in payload["messages"]] == ["user"]


# ───────────────── CJK 估算 ─────────────────


def test_cjk_counts_roughly_one_token_per_char():
    assert _approx_token_count("中文测试") == 4
    assert _approx_token_count("abcd") == 1
    assert _approx_token_count("") == 0


def test_mixed_text_uses_both_rules():
    # 2 个汉字 = 2 token；'hello world' 11 字节 → ceil(11/4)=3
    assert _approx_token_count("你好hello world") == 2 + 3


# ───────────────── 模式能力段 ─────────────────


def test_capability_section_lists_only_unavailable_tools():
    text = _mode_capability_section("readonly", {"fs_read", "fs_grep"})
    assert _MODE_NOTES["readonly"] in text
    unavailable_line = [line for line in text.splitlines() if "Not available" in line][0]
    assert "fs_write" in unavailable_line and "fs_read" not in unavailable_line
    # 清单里的每一项都必须是“未下发”的工具，已下发的不得出现
    listed = unavailable_line.split(": ", 1)[1].split(", ")
    assert set(listed) == set(_CAPABILITY_TOOLS) - {"fs_read", "fs_grep"}


def test_capability_section_absent_without_mode_or_when_unknown():
    assert _mode_capability_section(None, {"fs_read"}) == ""
    assert _mode_capability_section("unknown-mode", {"fs_read"}) == ""


def test_plan_mode_requires_asking_before_writing_the_document():
    text = build_main_system_prompt(plan_flow_enabled=True)  # 计划流程章节仅在计划模式保留
    assert "Before writing the plan, resolve every open question with `ask_user_question`" in text
    assert "user-deferred" in text


def test_static_prompt_has_methodology_sections():
    for heading in ("## Development workflow", "## Avoid hallucination", "## Tool combinations"):
        assert heading in MAIN_SYSTEM_PROMPT


def test_prompt_verifies_by_risk_and_volume_not_per_step():
    """验证时机：按风险与改动量自行判断，不固定在每步/每批，交付前整体验证。"""
    text = build_main_system_prompt(execution_mode="agent", available_tools={"fs_read"})
    assert "Work in coherent batches" in text
    assert "Judge for yourself when to verify" in text
    assert "Verify the whole change before delivery" in text
    assert "After each step, run tests or lint" not in text
    assert "Verify each step" not in text
    assert "Verify once per batch" not in text

    from app.orchestration.prompts.ta3_fusion import (
        CHATCODER_ADDENDUM,
        CHATCODER_ADDENDUM_FULL_ACCESS,
        CHATCODER_ADDENDUM_READONLY,
    )

    for tpl in (CHATCODER_ADDENDUM, CHATCODER_ADDENDUM_FULL_ACCESS):
        assert "按需验证" in tpl
        assert "小步修改" not in tpl
    # plan-562-2318 A23：只读会话不写文件、不跑命令，原先把可写流程规范一并套进去
    # （复查改动、交付前自查可运行性、后台进程管理、交付前统一验证）与审批层的硬拒绝冲突。
    # 因此只读模板改为**反向断言**：不得再出现这些指向不存在动作的条款。
    assert "按需验证" not in CHATCODER_ADDENDUM_READONLY
    assert "BashKill" not in CHATCODER_ADDENDUM_READONLY
    assert "修改文件后" not in CHATCODER_ADDENDUM_READONLY


def test_system_prompt_is_deterministic_for_same_inputs():
    a = build_main_system_prompt(execution_mode="agent", available_tools={"fs_read"})
    b = build_main_system_prompt(execution_mode="agent", available_tools={"fs_read"})
    assert a == b


# ───────────────── 压缩切点配对 ─────────────────


def test_compaction_boundary_never_splits_a_tool_pair():
    from app.core.enums import MsgType
    from app.orchestration.context_compressor import _is_pairing_balanced

    def m(msg_type, call_key=""):
        return SimpleNamespace(msg_type=msg_type.value, content={"call_key": call_key})

    # 序列：text, tool_call(c1), tool_result(c1), text
    msgs = [
        m(MsgType.TEXT),
        m(MsgType.TOOL_CALL, "c1"),
        m(MsgType.TOOL_RESULT, "c1"),
        m(MsgType.TEXT),
    ]
    # 切点 2：压缩区含 tool_call 但结果在保留区 → 配对被切断，不合法
    assert _is_pairing_balanced(msgs, 2) is False
    # 切点 3：压缩区 [0..2] 配对闭合，保留区只剩文本 → 合法
    assert _is_pairing_balanced(msgs, 3) is True
    # 切点 1：tool_call 与 tool_result 都在保留区，配对完整 → 合法（切点不切断配对即可）
    assert _is_pairing_balanced(msgs, 1) is True

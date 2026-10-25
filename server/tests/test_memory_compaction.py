"""压缩器的会话记忆双产出单测（plan-548-2277）。

覆盖三件事：
1. `<session-memory>` 的各种脏输入都不应让压缩失败（记忆是附加收益）；
2. 摘要与记忆互不干扰——记忆很长也不能把摘要撑过 max_chars；
3. 压缩提示词确实要求了两段输出。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestration.context_compressor import (
    _parse_session_memories,
    _split_summary_and_memories,
)
from app.orchestration.prompts.summary import (
    SESSION_MEMORY_CLOSE_TAG,
    SESSION_MEMORY_OPEN_TAG,
    build_compaction_prompt,
)


# ── 解析：正常与容错 ─────────────────────────────────────────────────────────
def test_parse_valid_items():
    block = (
        '[{"text": "选择落库式压缩而不是内存摘要，因为跨轮可复用", '
        '"kind": "decision", "keywords": ["压缩", "compaction"]}]'
    )
    items = _parse_session_memories(block)
    assert len(items) == 1
    assert items[0]["kind"] == "decision"
    assert items[0]["keywords"] == ["压缩", "compaction"]


def test_parse_json_inside_code_fence():
    block = '```json\n[{"text": "围栏里的 JSON 也要能解析出来，不能丢记忆"}]\n```'
    items = _parse_session_memories(block)
    assert len(items) == 1


def test_parse_invalid_returns_empty_never_raises():
    assert _parse_session_memories("") == []
    assert _parse_session_memories("not json at all") == []
    assert _parse_session_memories('{"a": 1}') == []  # 对象不是数组
    assert _parse_session_memories("[1, 2, 3]") == []  # 元素不是 dict


def test_parse_filters_short_and_normalizes_kind():
    block = (
        '[{"text": "短", "kind": "decision"},'
        ' {"text": "这是一条长度足够长的正常记忆内容用于测试解析",'
        '  "kind": "not-a-kind", "keywords": "a, b、c"},'
        ' {"text": "这是另一条长度足够长的正常记忆内容用于测试", "keywords": ["x"]}]'
    )
    items = _parse_session_memories(block)
    assert len(items) == 2
    assert items[0]["kind"] == "fact"           # 非法 kind 归一
    assert items[0]["keywords"] == ["a", "b", "c"]  # 字符串关键词切分
    assert items[1]["kind"] == "fact"           # 缺 kind 默认 fact


def test_parse_caps_item_count():
    items = ",".join(
        '{"text": "第%d条足够长的会话记忆内容用于测试条数上限"}' % i for i in range(20)
    )
    assert len(_parse_session_memories("[" + items + "]")) == 12


def test_parse_caps_text_length():
    block = '[{"text": "' + "长" * 5000 + '"}]'
    items = _parse_session_memories(block)
    assert len(items) == 1
    assert len(items[0]["text"]) == 1200


# ── 拆分：摘要与记忆互不污染 ─────────────────────────────────────────────────
def test_split_summary_and_memories():
    raw = (
        "## Primary Request and Intent\n- 做记忆改造\n"
        + SESSION_MEMORY_OPEN_TAG
        + '\n[{"text": "决定三层记忆用 scope 区分，会话层参与压缩归档"}]'
        + SESSION_MEMORY_CLOSE_TAG
    )
    summary, mems = _split_summary_and_memories(raw, 4000)
    assert summary.startswith("## Primary Request")
    assert SESSION_MEMORY_OPEN_TAG not in summary
    assert SESSION_MEMORY_CLOSE_TAG not in summary
    assert len(mems) == 1


def test_split_without_tag_is_compatible():
    summary, mems = _split_summary_and_memories("## Primary Request and Intent\n- 只有摘要", 4000)
    assert summary.startswith("## Primary Request")
    assert mems == []


def test_summary_truncated_but_memories_intact():
    mem_text = "这是一条需要完整保留的会话记忆内容，不能被摘要长度限制截断"
    raw = (
        "x" * 5000
        + SESSION_MEMORY_OPEN_TAG
        + '[{"text": "%s"}]' % mem_text
        + SESSION_MEMORY_CLOSE_TAG
    )
    summary, mems = _split_summary_and_memories(raw, 100)
    assert len(summary) == 100        # 摘要按 max_chars 截断
    assert len(mems) == 1
    assert mems[0]["text"] == mem_text  # 记忆完整保留


def test_split_unclosed_tag_still_parses():
    raw = (
        "## S\n- a" + SESSION_MEMORY_OPEN_TAG
        + '[{"text": "未闭合标签下的记忆内容也要能解析出来，这是测试要求"}]'
    )
    summary, mems = _split_summary_and_memories(raw, 4000)
    assert summary.startswith("## S")
    assert len(mems) == 1


# ── 提示词 ───────────────────────────────────────────────────────────────────
def test_compaction_prompt_requests_two_outputs():
    p = build_compaction_prompt("zh")
    assert "session-memory" in p
    assert "keywords" in p
    assert "200-1200" in p
    assert "简体中文" in p            # 语言纪律仍生效
    assert "FIRST block" in p         # 摘要必须在前


def test_compaction_prompt_english_variant():
    p = build_compaction_prompt("en")
    assert "Summary language: English" in p
    assert "session-memory" in p

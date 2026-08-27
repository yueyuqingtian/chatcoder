"""正文内联思考标签剥离测试（gpt-5.6-luna 等模型把思考混入 content）。

覆盖:
- 离线全量剥离 `_split_inline_thinking`（闭合/未闭合/嵌套/多块/孤立闭合）
- 流式增量剥离 `_InlineThinkingStreamSplitter`（标签跨 chunk 拆分、未闭合兜底）
"""
import pytest

from app.orchestration.agent_loop import (
    _InlineThinkingStreamSplitter,
    _split_inline_thinking,
)


class TestSplitInlineThinking:
    def test_closed_block(self):
        assert _split_inline_thinking("<thinking>a</thinking>def") == ("def", "a")

    def test_unclosed_block(self):
        # gpt-5.6-luna 只输出 <thinking> 前缀、无闭合标签时，截断到末尾仍视为思考
        text = (
            "<thinking>Fixing _stream_llm line lengthsTesting focused provider"
            "Considering limitations of automatic mode switch due missing history"
            "****Ensuring UI changes build"
        )
        clean, think = _split_inline_thinking(text)
        assert clean == ""
        assert think.startswith("Fixing _stream_llm line lengths")
        assert "****" in think

    def test_text_before_block(self):
        assert _split_inline_thinking("abc<thinking>xyz") == ("abc", "xyz")

    def test_nested_open_tags(self):
        assert _split_inline_thinking("<thinking>a<thinking>b</thinking>c") == ("c", "a\nb")

    def test_multiple_blocks(self):
        assert _split_inline_thinking(
            "<thinking>a</thinking><thinking>b</thinking>") == ("", "a\nb")

    def test_orphan_closing_tag(self):
        # 块外孤立闭合标签按普通正文保留
        assert _split_inline_thinking("hello</thinking>world") == ("helloworld", "")

    def test_thought_tag(self):
        assert _split_inline_thinking("<thought>abc</thought>def") == ("def", "abc")

    def test_no_tag_passthrough(self):
        assert _split_inline_thinking("普通文本") == ("普通文本", "")

    def test_case_insensitive(self):
        assert _split_inline_thinking("<THINKING>abc</THINKING>") == ("", "abc")


class TestStreamSplitter:
    """流式剥离：标签可被任意拆分到多个 chunk，未闭合块由 flush 兜底。"""

    def _pump(self, chunks):
        s = _InlineThinkingStreamSplitter()
        events = []
        for ch in chunks:
            c, t = s.feed(ch)
            if t:
                events.append(("thinking", t))
            if c:
                events.append(("content", c))
        c, t = s.flush()
        if t:
            events.append(("thinking", t))
        if c:
            events.append(("content", c))
        return events

    def test_tag_split_across_chunks(self):
        events = self._pump((
            "<think", "ing>", "Fixing", " line", " length", "s</th", "inking>", " done",
        ))
        assert events == [("thinking", "Fixing line lengths"), ("content", " done")]

    def test_text_before_and_after_block(self):
        events = self._pump(("hello", " <thinking", ">think", "text", "</thinking", "> world"))
        # thinking 块在后续正文之前广播
        assert events == [
            ("content", "hello"), ("content", " "),
            ("thinking", "thinktext"), ("content", " world"),
        ]

    def test_unclosed_block_flushed_as_thinking(self):
        events = self._pump(("正文", "<thinking>", "未闭合思考", "to end"))
        assert events == [("content", "正文"), ("thinking", "未闭合思考to end")]

    def test_multiple_blocks(self):
        events = self._pump(("<thinking>a</thinking>b", "<thinking>c</thinking>"))
        assert events == [("thinking", "a"), ("content", "b"), ("thinking", "c")]

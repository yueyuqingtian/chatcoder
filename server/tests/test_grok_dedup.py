"""plan-248-1258 M7: grok 专项——流式内容去重与任务边界标记单测。"""
from app.models.providers.openai_compatible import _ContentDeduper


def test_dedup_passes_short_stream():
    """短增量（逐字流式）不应被误判丢弃。"""
    d = _ContentDeduper()
    out = "".join(d.feed(c) for c in ["你", "好", "世", "界"])
    assert out == "你好世界"


def test_dedup_drops_duplicate_full_chunk():
    """整段重发（网关重复投递同一块）应被丢弃。"""
    d = _ContentDeduper()
    first = d.feed("This is a fairly long sentence that repeats.")
    assert first == "This is a fairly long sentence that repeats."
    again = d.feed("This is a fairly long sentence that repeats.")
    assert again == ""


def test_dedup_trims_overlapping_tail():
    """尾部重叠（上一块尾部 + 新块整体重发）只保留新增部分。"""
    d = _ContentDeduper()
    d.feed("Hello world, this is a long first chunk of text.")
    # 新块以旧块尾部的前缀开头（重叠 20 字符）
    overlap = "this is a long first chunk of text."
    added = d.feed(overlap + " Plus brand new content here.")
    assert added == " Plus brand new content here."


def test_dedup_keeps_genuinely_new_content():
    d = _ContentDeduper()
    d.feed("First chunk of a long paragraph.")
    out = d.feed("Second different chunk of text.")
    assert out == "Second different chunk of text."


def test_dedup_empty_delta():
    d = _ContentDeduper()
    assert d.feed("") == ""


def test_task_boundary_injected_between_history_and_instruction():
    """ContextBundle：设置 task_boundary 后，边界提示位于历史与新 user 指令之间。"""
    from app.orchestration.context_manager import ContextBundle
    from app.models.schemas import ChatMessage

    b = ContextBundle(system="sys", instruction="new short task")
    b.history = [
        ChatMessage(role="user", content="prev request"),
        ChatMessage(role="assistant", content="long previous result " * 100),
    ]
    b.task_boundary = "The previous task has ENDED."
    msgs = b.to_messages()
    roles = [m.role for m in msgs]
    # 断言行序：... history(assistant) → system(boundary) → user(instruction)
    assert roles[-1] == "user"
    assert roles[-2] == "system"
    assert msgs[-2].content == "The previous task has ENDED."
    assert msgs[-1].content == "new short task"


def test_task_boundary_absent_without_history():
    from app.orchestration.context_manager import ContextBundle

    b = ContextBundle(system="sys", instruction="hi")
    b.task_boundary = "The previous task has ENDED."
    msgs = b.to_messages()
    # 无历史时不注入边界提示（避免多余 system 消息）
    assert all(m.content != "The previous task has ENDED." for m in msgs if m.role == "system")

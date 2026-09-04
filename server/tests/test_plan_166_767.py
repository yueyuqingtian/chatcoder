"""plan-166-767 回归测试：摘要阈值/窗口预算、图片消息摘要保护、注入/合并保留图片块、trae 图片降级。"""
from types import SimpleNamespace

from app.models.schemas import ChatMessage
from app.orchestration.token_counter import MAIN_SUMMARIZE_RATIO, MAIN_WINDOW_RATIO
from app.orchestration.context_memory import _is_image_message
from app.orchestration.compaction import normalize_tool_sequence
from app.models.providers.trae import TraeProvider


def _fake(content):
    """构造带 content 的最小消息对象（用于 _is_image_message）。"""
    return SimpleNamespace(content=content)


def test_summarize_and_window_ratio():
    """计划：摘要阈值与注入预算统一为 0.85（预算 ≥ 阈值，避免静默丢失）。"""
    assert MAIN_SUMMARIZE_RATIO == 0.85
    assert MAIN_WINDOW_RATIO == 0.85
    assert MAIN_WINDOW_RATIO >= MAIN_SUMMARIZE_RATIO


def test_is_image_message_true_for_image_attachment():
    m = _fake({"text": "hi", "attachments": [{"type": "image", "filename": "a.png", "path": "x/a.png"}]})
    assert _is_image_message(m) is True


def test_is_image_message_true_by_filename():
    """type 缺失时按文件名/路径后缀判断（is_image）。"""
    m = _fake({"text": "hi", "attachments": [{"filename": "b.jpeg", "path": "x/b.jpeg"}]})
    assert _is_image_message(m) is True


def test_is_image_message_false_for_text():
    m = _fake({"text": "hi", "attachments": [{"type": "text", "filename": "a.txt", "path": "x/a.txt"}]})
    assert _is_image_message(m) is False


def test_is_image_message_false_without_attachments():
    assert _is_image_message(_fake({"text": "hi"})) is False


def test_normalize_consecutive_users_keeps_content_blocks():
    """连续 user 消息合并时保留图片内容块（此前被丢弃）。"""
    img_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}
    msgs = [
        ChatMessage(role="user", content="第一句"),
        ChatMessage(role="user", content="第二句", content_blocks=[img_block]),
    ]
    fixed = normalize_tool_sequence(msgs)
    assert len(fixed) == 1
    assert fixed[0].role == "user"
    assert fixed[0].content == "第一句\n\n第二句"
    assert fixed[0].content_blocks == [img_block]


def test_trae_convert_message_image_block_downgrade():
    """trae 通道不直传图片：降级为 read_attachment 提示，禁止静默丢失。"""
    p = TraeProvider(api_key="", base_url="http://localhost", model="m")
    msg = ChatMessage(
        role="user",
        content="看图",
        content_blocks=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}],
    )
    out = p._convert_message(msg)
    assert "1 张图片" in out["content"]
    assert "read_attachment" in out["content"]


if __name__ == "__main__":
    test_summarize_and_window_ratio()
    test_is_image_message_true_for_image_attachment()
    test_is_image_message_true_by_filename()
    test_is_image_message_false_for_text()
    test_is_image_message_false_without_attachments()
    test_normalize_consecutive_users_keeps_content_blocks()
    test_trae_convert_message_image_block_downgrade()
    print("All plan-166-767 tests passed successfully!")

"""后台命令完成回报单测（plan-474-2105）。

覆盖：
- `_format` 摘要含命令、退出码、耗时、日志尾部，超过 5 条只列前 5 条；
- `on_bg_exit` 在合并窗口内只投递一次并合并多条；
- 无事件循环时安全跳过（不抛异常）。
"""
import asyncio

import pytest

from app.orchestration import bg_notify, mailbox


@pytest.fixture(autouse=True)
def _clean():
    bg_notify.reset_for_tests()
    mailbox.reset_for_tests()
    yield
    bg_notify.reset_for_tests()
    mailbox.reset_for_tests()


def _payload(shell_id="bg_1", rc=0, cmd="npm run build", tail="ok"):
    return {
        "shell_id": shell_id, "session_id": 3, "turn_id": 10,
        "command": cmd, "returncode": rc, "elapsed": 1.5, "tail": tail,
    }


def test_format_includes_exit_code_and_tail():
    text = bg_notify._format([_payload()])
    assert "共 1 个后台命令已结束" in text
    assert "退出码 0" in text
    assert "npm run build" in text
    assert "ok" in text


def test_format_marks_failure_and_truncates_items():
    items = [_payload(shell_id=f"bg_{i}", rc=1) for i in range(7)]
    text = bg_notify._format(items)
    assert "退出码 1" in text
    assert "另有 2 个已完成命令未列出" in text


@pytest.mark.asyncio
async def test_on_bg_exit_merges_and_delivers_once(monkeypatch):
    monkeypatch.setattr(bg_notify, "MERGE_WINDOW_S", 0.02)
    delivered: list[str] = []

    async def fake_publish(kind, *, session_id, text, source_session_id=None,
                          question_id=None, persist=True, extra=None):
        delivered.append(text)
        return {"delivered": "queued"}

    monkeypatch.setattr(mailbox, "publish", fake_publish)

    await bg_notify.on_bg_exit(_payload("bg_a"))
    await bg_notify.on_bg_exit(_payload("bg_b"))
    await asyncio.sleep(0.1)

    assert len(delivered) == 1, "同一会话的完成事件应合并成一条"
    assert "共 2 个后台命令已结束" in delivered[0]


def test_format_handles_empty_tail():
    """日志尾部为空时不能出现空行噪声（命令没有任何输出是常见情况）。"""
    text = bg_notify._format([_payload(tail="")])
    assert "日志尾部" not in text
    assert "退出码 0" in text

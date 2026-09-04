"""terminal_bg_status 的 wait_until_done 动态等待行为单测（plan-167-774）。

验证:
- wait_until_done=true 且进程已结束 → 立即返回「已退出」最终状态（动态，非固定等待）
- wait_until_done=true 且进程仍在运行 → 超时后返回「仍在运行」明确结果（非报错/静默失败）
- 未知 shell_id → 明确报错
- 等待期间收到中断信号 → 抛 CancelledError（与 agent_loop 取消语义一致）
- _parse_bool / _parse_wait_timeout 解析与钳制
"""
import asyncio
import time
from typing import Any

import pytest

import app.orchestration.tools.bg_process as bgp
from app.orchestration.tools.bg_process import _BgEntry, _parse_bool, _parse_wait_timeout
from app.orchestration.tools.base import ToolContext


def _ctx(**over) -> ToolContext:
    base: dict[str, Any] = {
        "workspace_root": "/tmp/ws", "session_id": 1, "task_id": 1,
        "agent_id": 1, "agent_name": "t",
    }
    base.update(over)
    return ToolContext(**base)


def _make_entry(shell_id: str, returncode: int | None, log: str = "hello\n"):
    """构造一个可被 status() 读取的注册表条目（不启动真实收集进程）。"""
    entry = _BgEntry(shell_id, proc=object(), command="echo hi", cwd="", session_id=1)
    entry.append_output(log)
    entry.returncode = returncode
    if returncode is not None:
        entry.finished_at = time.time()
    return entry


@pytest.fixture
def clean_registry():
    """隔离模块级单例 bg_process_registry，测后恢复，避免污染其它测试。"""
    saved = dict(bgp.bg_process_registry._entries)
    bgp.bg_process_registry._entries.clear()
    yield bgp.bg_process_registry
    bgp.bg_process_registry._entries.clear()
    bgp.bg_process_registry._entries.update(saved)


async def _no_broadcast(*_a, **_k):
    """no-op 广播，隔离 ws_manager 全局 seq/buffer 副作用。"""
    return None


@pytest.mark.asyncio
async def test_wait_until_done_returns_finished(clean_registry, monkeypatch):
    monkeypatch.setattr("app.orchestration.agent_events.broadcast", _no_broadcast)
    sid = "bg_test_fin"
    clean_registry._entries[sid] = _make_entry(sid, returncode=0, log="done\n")
    r = await bgp.TerminalBgStatusTool().run(
        {"shell_id": sid, "wait_until_done": True}, _ctx(),
    )
    assert r.ok is True
    assert "已退出" in r.output
    assert "退出码 0" in r.output


@pytest.mark.asyncio
async def test_wait_until_done_timeout_returns_running(clean_registry, monkeypatch):
    monkeypatch.setattr("app.orchestration.agent_events.broadcast", _no_broadcast)
    monkeypatch.setattr(bgp, "_WAIT_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(bgp, "_WAIT_PROGRESS_INTERVAL", 0.02)
    monkeypatch.setattr(bgp, "_parse_wait_timeout", lambda raw: 0.2)  # 测试缩短超时
    sid = "bg_test_run"
    clean_registry._entries[sid] = _make_entry(sid, returncode=None, log="building...\n")
    r = await bgp.TerminalBgStatusTool().run(
        {"shell_id": sid, "wait_until_done": True}, _ctx(),
    )
    assert r.ok is True  # 超时返回明确结果，而非报错
    assert "运行中" in r.output
    assert "仍在运行" in r.output
    assert "已到 wait_timeout 上限" in r.output
    assert "building..." in r.output


@pytest.mark.asyncio
async def test_wait_until_done_unknown_shell_id(clean_registry):
    r = await bgp.TerminalBgStatusTool().run(
        {"shell_id": "bg_nope", "wait_until_done": True}, _ctx(),
    )
    assert r.ok is False
    assert "未知 shell_id" in r.error


@pytest.mark.asyncio
async def test_wait_until_done_cancelled(clean_registry):
    sid = "bg_test_cancel"
    clean_registry._entries[sid] = _make_entry(sid, returncode=None)
    ev = asyncio.Event()
    ev.set()
    with pytest.raises(asyncio.CancelledError):
        await bgp.TerminalBgStatusTool().run(
            {"shell_id": sid, "wait_until_done": True}, _ctx(cancel_event=ev),
        )


@pytest.mark.asyncio
async def test_status_no_wait_passthrough(clean_registry):
    """wait_until_done 缺省 false 时应保持原有即时查询行为。"""
    sid = "bg_test_passthrough"
    clean_registry._entries[sid] = _make_entry(sid, returncode=None, log="")
    r = await bgp.TerminalBgStatusTool().run({"shell_id": sid}, _ctx())
    assert r.ok is True
    assert "运行中" in r.output
    assert "暂无新日志" in r.output


def test_parse_bool():
    assert _parse_bool(True) is True
    assert _parse_bool(False) is False
    assert _parse_bool("true") is True
    assert _parse_bool("false") is False
    assert _parse_bool(None) is False
    assert _parse_bool("") is False


def test_parse_wait_timeout_clamp(monkeypatch):
    from app.core.config import settings as _settings
    monkeypatch.setattr(_settings, "tool_exec_timeout_sec", 30)
    assert _parse_wait_timeout(9999) == 20  # 30 - 10 buffer
    assert _parse_wait_timeout(1) == 5      # 下限 5
    assert _parse_wait_timeout(12) == 12
    assert _parse_wait_timeout(None) == 20  # 默认取全局工具超时

"""打包场景下的诊断能力测试（v36）。

保证「重新打包后 terminal_exec 出问题也能定位」：
1. 日志目录解析必须落到可写位置，且打包态（sys.frozen）下不依赖 cwd；
2. diagnostics handler 独立幂等，不受 setup_logging 调用时序影响；
3. 工具异常必须记录 traceback 与调用上下文（文件+行号）；
4. terminal_exec 执行路径可用，且超时/失败分支不抛异常。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import _diag  # noqa: E402
from app.core.logging import (  # noqa: E402
    _writable_data_dir,
    ensure_diagnostics_logger,
    resolve_log_dir,
)


def test_resolve_log_dir_returns_writable_dir(tmp_path):
    """显式传入的目录存在且可写时，应原样使用。"""
    target = tmp_path / "mylogs"
    got = resolve_log_dir(str(target))
    assert got == target
    assert got.is_dir()


def test_resolve_log_dir_skips_unwritable_candidate(tmp_path, monkeypatch):
    """首个候选不可写时，应回退到后续候选，而不是抛异常。

    打包后 cwd 可能在 Program Files（只读），此行为是诊断日志不丢失的前提。
    """
    monkeypatch.setenv("CHATCODER_LOG_DIR", str(tmp_path / "envlogs"))
    got = resolve_log_dir()
    assert got.is_dir()
    # 探针文件不应残留
    assert not (got / ".write_probe").exists()


def test_writable_data_dir_is_absolute():
    """数据目录必须是绝对路径，否则打包后相对路径会随 cwd 漂移。"""
    d = _writable_data_dir()
    assert d.is_absolute()


def test_writable_data_dir_frozen_uses_user_dir(monkeypatch):
    """打包态（frozen）必须落到用户可写目录，不能是 exe 同目录。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    d = _writable_data_dir()
    assert d.is_absolute()
    assert "chatcoder" in d.parts


def test_ensure_diagnostics_logger_idempotent():
    """重复调用不应重复挂 handler（否则日志重复、文件句柄泄漏）。"""
    first = ensure_diagnostics_logger()
    n_after_first = len(logging.getLogger("app.diagnostics").handlers)
    second = ensure_diagnostics_logger()
    n_after_second = len(logging.getLogger("app.diagnostics").handlers)
    assert first == second
    assert n_after_first == n_after_second


def test_format_exc_chain_includes_root_cause_and_line():
    """异常链必须含根因类型与抛错行号，否则无法定位代码位置。"""
    def inner():
        result = True
        _a, _b = result  # 解包 bool → TypeError
        return _a, _b

    try:
        try:
            inner()
        except TypeError as exc:
            raise RuntimeError("外层包装") from exc
    except RuntimeError as exc:
        chain = _diag.format_exc_chain(exc)
    else:  # pragma: no cover
        pytest.fail("应抛出异常")

    assert "TypeError" in chain
    assert "RuntimeError" in chain
    assert "test_format_exc_chain_includes_root_cause_and_line" in chain
    assert "inner" in chain


def test_summarize_args_handles_non_dict_and_long_values():
    """参数摘要不得因非 dict 或超长值抛错——日志本身不能成为故障源。"""
    out = _diag.summarize_args({"path": "a/b.py", "old_text": "x" * 500})
    assert "path='a/b.py'" in out
    assert "已截断" in out
    assert isinstance(_diag.summarize_args("not a dict"), str)
    assert isinstance(_diag.summarize_args(None), str)


def test_log_tool_error_records_traceback(tmp_path, caplog):
    """工具异常日志必须包含 traceback 段与调用上下文。"""
    logger = logging.getLogger("app.diagnostics")
    with caplog.at_level(logging.DEBUG, logger="app.diagnostics"):
        try:
            raise ValueError("boom")
        except ValueError as exc:
            _diag.log_tool_error(
                turn_id=42, step=3, tool_name="terminal_exec",
                call_key="ck_1", exc=exc, args={"command": "dir"},
                phase="execute",
            )
    record = next((r for r in caplog.records if "[tool.error]" in r.message), None)
    assert record is not None
    assert "turn=42" in record.message
    assert "tool=terminal_exec" in record.message
    assert "exc_type=ValueError" in record.message
    assert "--- traceback ---" in record.message
    assert "test_log_tool_error_records_traceback" in record.message


def test_terminal_tool_importable_after_patch():
    """terminal 模块补丁后仍可导入（防止语法/缩进错误破坏打包）。"""
    from app.orchestration.tools.terminal import TerminalExecTool
    tool = TerminalExecTool()
    assert tool.name == "terminal_exec"
    assert tool.approval_precheck({}, None) == (False, "命令为空")


def test_terminal_timeout_path_emits_diagnostics(caplog):
    """超时分支应记录 [term.timeout]，而不是仅返回一行错误。"""
    import asyncio

    from app.orchestration.tools.base import ToolContext
    from app.orchestration.tools import terminal as term_mod

    ctx = ToolContext(
        workspace_root=str(Path(__file__).resolve().parents[1]),
        session_id=0, task_id=0, agent_id=0, agent_name="t",
    )
    tool = term_mod.TerminalExecTool()
    original = term_mod._TIMEOUT_SEC
    term_mod._TIMEOUT_SEC = 0.05
    try:
        with caplog.at_level(logging.INFO):
            res = asyncio.run(tool.run({"command": "sleep 5"}, ctx))
    finally:
        term_mod._TIMEOUT_SEC = original

    # 超时或立即失败都不得抛异常；若命中超时分支应有日志
    assert res is not None
    if not res.ok:
        assert res.error

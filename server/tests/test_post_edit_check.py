"""写后最小检查单测（plan-548-2277）。

守护点：
1. .py / .json / .js 能查出真错误，.md 等按设计跳过；
2. 从工具结果 data 里正确提取写入路径；
3. 同一文件连续失败到阈值后不再重复跑检查，改为提醒模型先读根因。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.post_edit_check import (
    check_after_tool,
    check_written_file_sync,
    is_checkable,
    paths_from_result,
)


# ── 单文件检查 ────────────────────────────────────────────────────────────────
def test_python_syntax_ok(tmp_path):
    p = tmp_path / "ok.py"
    p.write_text("def f():\n    return 1\n", encoding="utf-8")
    r = check_written_file_sync(p)
    assert r.ok and not r.skipped
    assert r.as_note("ok.py") == ""  # 通过时不打扰上下文


def test_python_syntax_error(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text("def f(:\n", encoding="utf-8")
    r = check_written_file_sync(p)
    assert not r.ok
    assert "SyntaxError" in r.output or "invalid syntax" in r.output
    note = r.as_note("bad.py")
    assert "写后检查未通过" in note and "bad.py" in note


def test_json_valid_and_invalid(tmp_path):
    good = tmp_path / "good.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    assert check_written_file_sync(good).ok

    bad = tmp_path / "bad.json"
    bad.write_text('{"a": }', encoding="utf-8")
    r = check_written_file_sync(bad)
    assert not r.ok
    assert "JSON" in r.output


def test_json_with_bom_is_not_a_false_positive(tmp_path):
    """plan-549-2281 复盘修复：带 UTF-8 BOM 的合法 JSON 不得被误报为解析失败。

    本机 PowerShell 5 的 Set-Content 默认写 BOM（AGENTS.md 第 7 节），触发条件真实；
    旧实现按 utf-8 读，首字符是 \ufeff，json.loads 抛「line 1 column 1」。
    """
    p = tmp_path / "bom.json"
    p.write_bytes('{"a": 1, "说明": "中文"}'.encode("utf-8-sig"))
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")  # 确认确实写了 BOM
    r = check_written_file_sync(p)
    assert r.ok, f"带 BOM 的合法 JSON 被误报：{r.output}"


def test_python_check_never_spawns_a_subprocess(tmp_path, monkeypatch):
    """plan-549-2281 复盘修复：.py 检查必须进程内完成，不再依赖 sys.executable。

    打包态 sys.executable 是 chatcoder-server.exe，`-c` 会被当成未知 argv 走到 main()
    → 抢实例锁 → 非零退出，于是每次写 .py 都报「后端已在运行」的假失败。
    这里把所有子进程入口一律打成失败，确保走 ast.parse 路径时根本不进 subprocess。
    """
    import subprocess as _sp

    def _boom(*args, **kwargs):  # noqa: ANN001, ANN002
        raise AssertionError("写后检查不应拉起子进程")

    monkeypatch.setattr(_sp, "run", _boom)
    monkeypatch.setattr(_sp, "Popen", _boom)

    p = tmp_path / "noproc.py"
    p.write_text("x = 1\n", encoding="utf-8")
    assert check_written_file_sync(p).ok is True


def test_python_syntax_error_reports_line_and_column(tmp_path):
    """错误输出采用「第 N 行第 M 列：msg」形式，直接可定位。"""
    p = tmp_path / "bad2.py"
    p.write_text("def f():\n    return 1\n\nx = (\n", encoding="utf-8")
    r = check_written_file_sync(p)
    assert not r.ok
    assert "第 " in r.output and "行第" in r.output and "列" in r.output


def test_python_source_with_bom_is_accepted(tmp_path):
    """带 BOM 的 .py 也能通过（tokenize.open 处理编码声明与 BOM）。"""
    p = tmp_path / "bom.py"
    p.write_bytes("# -*- coding: utf-8 -*-\nx = 1\n".encode("utf-8-sig"))
    assert check_written_file_sync(p).ok is True


def test_python_command_display_is_runnable(tmp_path):
    """展示串必须是模型能照抄跑通的命令（不能是 `… ast.parse(…)` 这种假命令）。"""
    p = tmp_path / "shown.py"
    p.write_text("def f(:\n", encoding="utf-8")
    r = check_written_file_sync(p)
    assert not r.ok
    assert "ast.parse" in r.command
    assert "…" not in r.command, "展示串里不得留省略号占位"
    assert "utf-8-sig" in r.command, "展示串必须对 BOM 安全"


def test_markdown_skipped(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# hi", encoding="utf-8")
    r = check_written_file_sync(p)
    assert r.ok and r.skipped
    assert "无可自动判定" in r.reason or r.reason


def test_typescript_skipped_with_reason(tmp_path):
    p = tmp_path / "a.ts"
    p.write_text("const x: number = 1", encoding="utf-8")
    r = check_written_file_sync(p)
    assert r.ok and r.skipped
    assert "typecheck" in r.reason


def test_missing_file_skipped(tmp_path):
    r = check_written_file_sync(tmp_path / "nope.py")
    assert r.ok and r.skipped


def test_is_checkable():
    assert is_checkable("a.py") is True
    assert is_checkable("a.JSON".lower()) is True
    assert is_checkable("a.ts") is False
    assert is_checkable("a.md") is False


# ── 从工具结果提取路径 ────────────────────────────────────────────────────────
def test_paths_from_result():
    assert paths_from_result("multi_file_edit", {"files": ["a.py", "b.py"]}) == ["a.py", "b.py"]
    assert paths_from_result("fs_write", {"path": "c.py"}) == ["c.py"]
    assert paths_from_result("editor_apply_diff", {"path": "d.py"}) == ["d.py"]
    # 只读工具不该触发检查
    assert paths_from_result("fs_read", {"path": "e.py"}) == []
    # 脏数据不抛异常
    assert paths_from_result("fs_write", None) == []
    assert paths_from_result("multi_file_edit", {"files": "not-a-list"}) == []


# ── check_after_tool：限次 ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_check_after_tool_reports_then_stops(tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n", encoding="utf-8")
    counts: dict[str, int] = {}

    note1 = await check_after_tool(
        "editor_apply_diff", {"path": "bad.py"}, str(tmp_path), fail_counts=counts,
    )
    assert "写后检查未通过" in note1

    note2 = await check_after_tool(
        "editor_apply_diff", {"path": "bad.py"}, str(tmp_path), fail_counts=counts,
    )
    assert "写后检查未通过" in note2

    # 第三次：达到 max_rounds，不再跑检查，改为提醒先读根因
    note3 = await check_after_tool(
        "editor_apply_diff", {"path": "bad.py"}, str(tmp_path), fail_counts=counts,
    )
    assert "连续 2 次" in note3
    assert "写后检查未通过" not in note3


@pytest.mark.asyncio
async def test_check_after_tool_passes_for_good_file(tmp_path):
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")
    counts: dict[str, int] = {}
    note = await check_after_tool(
        "fs_write", {"path": "good.py"}, str(tmp_path), fail_counts=counts,
    )
    assert note == ""
    assert counts.get(str((tmp_path / "good.py")).lower(), 0) == 0


@pytest.mark.asyncio
async def test_check_after_tool_ignores_non_edit_tool(tmp_path):
    note = await check_after_tool("fs_read", {"path": "whatever.py"}, str(tmp_path))
    assert note == ""

"""plan-20261020-pts：工具调用容错单测。

覆盖真实失败数据里的各类问题：参数写法归一化、未知工具建议、路径不存在的就近提示、
fs_read 传目录 / 上下文折叠缓存回落、正则不合法降级、待办清单折叠、项目级记忆写入、
只读命令判定、终端失败提示与 /dev/null 改写、重定向解析、页面脚本包装、折叠落盘位置。
"""
import pytest

from app.core.config import settings
from app.models.schemas import ChatMessage
from app.orchestration.browser.eval_wrap import eval_error_of, wrap_script
from app.orchestration.compaction import build_api_copy
from app.orchestration.tools.arg_normalizer import (
    describe_params,
    missing_args_error,
    normalize_args,
    notes_suffix,
)
from app.orchestration.tools.base import ToolContext, ToolResult
from app.orchestration.tools.executor import _annotate_result, _unknown_tool_error
from app.orchestration.tools.fs_list import FsListTool
from app.orchestration.tools.fs_read import FsReadTool
from app.orchestration.tools.grep import GrepTool
from app.orchestration.tools.memory_write import MemoryWriteTool
from app.orchestration.tools.path_hints import missing_path_hint
from app.orchestration.tools.registry import tool_registry
from app.orchestration.tools.shell_hints import failure_hint, rewrite_null_device
from app.orchestration.tools.shell_policy import is_readonly_command, risk_note
from app.orchestration.tools.terminal import _CD_ANYWHERE_RE, _GIT_CMD_RE, _GIT_DASH_C_RE, _LEADING_CD_RE
from app.orchestration.tools.todo import TodoWriteTool
from app.services.rollback_service import extract_shell_write_paths


def _ctx(workspace, session_id: int = 1) -> ToolContext:
    return ToolContext(
        workspace_root=str(workspace), session_id=session_id, task_id=1, agent_id=1, agent_name="tester",
    )


def _tool(name):
    t = tool_registry.get(name)
    assert t is not None, name
    return t


# ───────────────────────── 参数归一化 ─────────────────────────


def test_normalize_claude_style_edit_args():
    r = normalize_args(_tool("editor_apply_diff"), {
        "file_path": "a.py", "old_string": "x", "new_string": "y", "replace_all": "true",
    })
    assert r.args == {"path": "a.py", "old_text": "x", "new_text": "y", "replace_all": True}
    assert r.notes and not r.missing and not r.unknown
    assert "下次请直接使用" in notes_suffix(r.notes)


def test_normalize_camel_snake_and_number_strings():
    r = normalize_args(_tool("terminal_exec"), {"command": "dir", "wait_for_completion": "false", "timeout": "30"})
    assert r.args["waitForCompletion"] is False
    assert r.args["timeout"] == 30


def test_normalize_synonym_groups_follow_schema():
    assert normalize_args(_tool("memory_search"), {"query": "坑"}).args == {"keyword": "坑"}
    assert normalize_args(_tool("fs_grep"), {"query": "foo", "path": "src"}).args["pattern"] == "foo"
    assert normalize_args(_tool("fs_list"), {"directory": "src"}).args == {"path": "src"}


def test_normalize_skips_ambiguous_alias():
    # git 工具同时有 file 与 cwd：dir 可能是任一个，不猜
    r = normalize_args(_tool("git"), {"command": "status", "dir": "clinic"})
    assert "dir" in r.args and "cwd" not in r.args and "file" not in r.args


def test_normalize_json_string_array_and_enum_synonym():
    r = normalize_args(_tool("todo_write"), {"todos": '[{"content": "a", "status": "Done"}, {"content": "b", "status": "doing"}]'})
    assert r.args["todos"] == [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}]


def test_normalize_single_object_into_array_and_nested_keys():
    r = normalize_args(_tool("multi_file_edit"), {"edits": {"file_path": "a.ts", "old_string": "x", "new_string": "y"}})
    assert r.args["edits"] == [{"path": "a.ts", "old_text": "x", "new_text": "y"}]


def test_normalize_end_line_to_limit():
    r = normalize_args(_tool("fs_read"), {"path": "a.py", "start_line": 10, "end_line": 20})
    assert r.args["offset"] == 10 and r.args["limit"] == 11


def test_missing_and_unknown_params_are_reported():
    r = normalize_args(_tool("fs_read"), {"foo": 1})
    assert r.missing == ["path"] and r.unknown == ["foo"]
    msg = missing_args_error(_tool("fs_read"), r.missing)
    assert "path" in msg and "必填" in describe_params(_tool("fs_read"))


def test_annotate_result_appends_notes_and_param_list():
    tool = _tool("fs_read")
    ok = ToolResult(ok=True, output="内容")
    _annotate_result(ok, tool, normalize_args(tool, {"file_path": "a"}))
    assert "参数已自动纠正" in ok.output
    bad = ToolResult(ok=False, output="", error="路径非法或无法解析: ")
    _annotate_result(bad, tool, normalize_args(tool, {"foo": 1}))
    assert "缺少必填参数" in bad.error and "foo" in bad.error
    denied = ToolResult(ok=False, output="", error="[权限策略] x", data={"denied": True})
    _annotate_result(denied, tool, normalize_args(tool, {"foo": 1}))
    assert denied.error == "[权限策略] x"


def test_unknown_tool_suggests_project_tool():
    assert "fs_read" in _unknown_tool_error("read_file")
    assert "terminal_exec" in _unknown_tool_error("Bash")
    assert "未知工具" in _unknown_tool_error("Mb>")


# ───────────────────────── 路径提示 / 读取回落 ─────────────────────────


def test_missing_path_hint_points_to_same_name_file(workspace):
    target = workspace / "clinic" / "clinic-common" / "sql" / "202609.sql"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    (workspace / "clinic" / "clinic-basic").mkdir(parents=True)
    hint = missing_path_hint(str(workspace), "clinic/clinic-basic/sql/202609.sql", want_dir=False)
    assert "clinic/clinic-common/sql/202609.sql" in hint
    assert "clinic/clinic-basic/" in hint  # 最近存在的上级目录


@pytest.mark.asyncio
async def test_fs_read_missing_file_has_hint(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "main.ts").write_text("x", encoding="utf-8")
    r = await FsReadTool().run({"path": "lib/main.ts"}, _ctx(workspace))
    assert not r.ok and "src/main.ts" in r.error


@pytest.mark.asyncio
async def test_fs_read_directory_returns_listing(workspace):
    (workspace / "docs").mkdir()
    (workspace / "docs" / "a.md").write_text("x", encoding="utf-8")
    r = await FsReadTool().run({"path": "docs"}, _ctx(workspace))
    assert r.ok and "是目录" in r.output and "a.md" in r.output


@pytest.mark.asyncio
async def test_fs_read_compact_cache_falls_back_to_global(workspace, tmp_path, monkeypatch):
    global_ws = tmp_path / "global_ws"
    (global_ws / ".compact-cache").mkdir(parents=True)
    (global_ws / ".compact-cache" / "abcdef12.txt").write_text("折叠前的完整输出", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_root", str(global_ws))
    r = await FsReadTool().run({"path": ".compact-cache/abcdef12.txt"}, _ctx(workspace))
    assert r.ok and "折叠前的完整输出" in r.output


@pytest.mark.asyncio
async def test_fs_list_on_file_is_not_failure(workspace):
    (workspace / "a.txt").write_text("hello", encoding="utf-8")
    r = await FsListTool().run({"path": "a.txt"}, _ctx(workspace))
    assert r.ok and "是文件" in r.output


@pytest.mark.asyncio
async def test_grep_invalid_regex_falls_back_to_literal(workspace):
    (workspace / "a.java").write_text("int x = safeGet(map, key);\n", encoding="utf-8")
    r = await GrepTool().run({"pattern": "safeGet(", "path": "."}, _ctx(workspace))
    assert r.ok
    assert "不是合法正则" in r.output and "safeGet(" in r.output


def test_micro_compact_writes_into_session_workspace(workspace, monkeypatch):
    # fs_read 窗口阈值已改为字节口径（tool_output_bytes_read）
    monkeypatch.setattr(settings, "tool_output_bytes_read", 100, raising=False)
    msgs = [
        ChatMessage(role="user", content="u"),
        ChatMessage(role="tool", content="x" * 500, name="fs_read", tool_call_id="c1"),
    ]
    api = build_api_copy(msgs, str(workspace))
    placeholder = api[-1].content
    assert ".compact-cache/" in placeholder
    files = list((workspace / ".compact-cache").glob("*.txt"))
    assert len(files) == 1 and files[0].name in placeholder


# ───────────────────────── 待办清单 / 记忆 ─────────────────────────


@pytest.mark.asyncio
async def test_todo_write_folds_oldest_completed(workspace):
    todos = [{"content": f"done-{i}", "status": "completed"} for i in range(17)]
    todos += [{"content": "now", "status": "in_progress"}] + [{"content": f"next-{i}", "status": "pending"} for i in range(4)]
    r = await TodoWriteTool().run({"todos": todos}, _ctx(workspace))
    assert r.ok, r.error
    assert "已折叠最早完成的 2 项" in r.output


@pytest.mark.asyncio
async def test_todo_write_rejects_too_many_open_items(workspace):
    todos = [{"content": f"step-{i}", "status": "pending"} for i in range(21)]
    r = await TodoWriteTool().run({"todos": todos}, _ctx(workspace))
    assert not r.ok and "未完成的步骤就有 21 项" in r.error


@pytest.mark.asyncio
async def test_memory_write_project_scope_and_fallback(workspace):
    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Session as SessionModel
    from app.persistence.models.project import Project

    async with async_session_factory() as db:
        db.add(Project(id=7, name="demo", path=str(workspace)))
        await db.commit()  # 先落项目：会话对项目有外键
        db.add(SessionModel(id=11, project_id=7))
        db.add(SessionModel(id=12, project_id=None))
        await db.commit()

    tool = MemoryWriteTool()
    r = await tool.run({"text": "数据库迁移脚本统一放在 clinic-common 模块", "scope": "project"}, _ctx(workspace, 11))
    assert r.ok and "scope=project" in r.output

    r2 = await tool.run({"text": "这个会话没有绑定任何项目的约定", "scope": "project"}, _ctx(workspace, 12))
    assert r2.ok and "scope=session" in r2.output and "已改存为会话级" in r2.output


# ───────────────────────── 命令策略 / 终端 ─────────────────────────


@pytest.mark.parametrize("cmd", [
    'Get-ChildItem -Path src -Recurse -Include *.vue | Select-String -Pattern "x" -List | Select-Object -First 20 Path',
    'git show 9b0cdc1100 --stat; git show 566cee27e0 --stat',
    '$bytes = Get-Content "a.doc" -Encoding Byte -TotalCount 2048; ($bytes[0..15] | ForEach-Object { $_.ToString("X2") }) -join " "',
    '(Get-Content "a.doc" -Encoding Unicode)[400..700]',
    'Get-ItemProperty "HKLM:\\SOFTWARE\\Classes\\Word.Application\\CurVer"',
    'Test-NetConnection -ComputerName 10.0.0.1 -Port 5432 -InformationLevel Quiet',
    'Get-ChildItem -Directory -Recurse -Depth 2 | Where-Object { $_.Name -like "*WPS*" } | Select-Object -First 5 -ExpandProperty FullName',
    'Get-Item x | Select-Object Name, Length | Format-Table -AutoSize',
    'git log --format="%h|%an|%s" -n 6',
    '[System.IO.File]::ReadAllText("a.doc", [System.Text.Encoding]::Unicode).Substring(0, 60)',
    'Get-ChildItem | Select-Object Name, @{n="KB"; e={[math]::Round($_.Length / 1KB, 1)}}',
    'Set-Location clinic; git diff --stat',
])
def test_readonly_commands_are_recognized(cmd):
    assert is_readonly_command(cmd)


@pytest.mark.parametrize("cmd", [
    'Get-ChildItem | ForEach-Object { $_.Delete() }',
    'Get-ChildItem | ForEach-Object { Set-Content $_ "x" }',
    'Get-Content a.txt | Out-File b.txt',
    'echo hi > out.txt',
    '[System.IO.File]::WriteAllText("a", "b")',
    'Get-Item a.txt | ForEach-Object { $_.IsReadOnly = $true }',
    'git show HEAD; git reset --hard',
    'python -c "print(1)"',
    'Invoke-WebRequest https://x -OutFile a.zip',
    'Write-Output "$(Set-Content x y)"',
    'Copy-Item a b',
])
def test_write_commands_are_not_readonly(cmd):
    assert not is_readonly_command(cmd)


def test_format_risk_pattern_no_false_positive():
    assert risk_note("format c:")
    assert risk_note("Format-Volume -DriveLetter D")
    for cmd in ("Get-Item x | Format-Table", "git log --format=%h", "Format-Hex a.bin -Count 64", "ls 2>/dev/null"):
        assert not risk_note(cmd), cmd


def test_rewrite_null_device_by_shell():
    assert rewrite_null_device("git status 2>/dev/null", "pwsh") == ("git status 2>$null", True)
    assert rewrite_null_device("dir > /dev/null", "cmd") == ("dir >nul", True)
    assert rewrite_null_device("ls 2>/dev/null", "git-bash") == ("ls 2>/dev/null", False)


def test_failure_hints_for_common_shell_mistakes():
    assert "Select-String 没有 -Recurse" in failure_hint(
        'Select-String -Path src -Include *.java -Pattern "x" -Recurse', "找不到与参数名称 'Recurse' 匹配的参数。", "pwsh")
    assert "Select-Object -First" in failure_hint("npx tsc --noEmit 2>&1 | head -50", "", "pwsh")
    assert "cmd.exe" in failure_hint("Get-ChildItem src", "'Get-ChildItem' 不是内部或外部命令", "cmd")
    assert "cwd" in failure_hint("git status", "fatal: not a git repository", "pwsh")
    assert "javap" in failure_hint("javap -v A", "javap: 术语 'javap' 不会被识别为 cmdlet", "pwsh")
    assert failure_hint("npm test", "1 failed", "pwsh") == ""


def test_terminal_cd_and_git_detection():
    m = _LEADING_CD_RE.match('Set-Location "F:\\project\\work\\clinic"; git diff --stat')
    assert m and m.group("dir").strip('"') == "F:\\project\\work\\clinic"
    assert _LEADING_CD_RE.match("cd clinic && git status").group("dir") == "clinic"
    assert _CD_ANYWHERE_RE.search("$x = 1; Set-Location clinic; git diff")
    assert not _GIT_CMD_RE.search("Get-Content .gitignore")
    assert _GIT_CMD_RE.search("git status")
    assert _GIT_DASH_C_RE.search("git -C clinic status")


@pytest.mark.parametrize("cmd,want", [
    ('Select-String -Path a.java -Pattern "" -List > $null', []),
    ('Select-String -Path x.java -Pattern "Map.Entry<String, List<Vo>> entry"', []),
    ('New-Item -ItemType Directory -Force -Path "$env:TEMP\\out" | Out-Null', []),
    ("mvn compile -q > out.txt 2>&1", ["out.txt"]),
    ('echo hi > "out file.txt"', ["out file.txt"]),
])
def test_redirect_parsing_ignores_quotes_and_null(cmd, want):
    assert extract_shell_write_paths(cmd) == want


# ───────────────────────── 页面脚本包装 ─────────────────────────


def test_wrap_script_modes():
    plain, _ = wrap_script("document.title")
    assert plain.startswith("try {") and "__ccEvalError" in plain
    ret, _ = wrap_script("const a = 1;\nreturn a + 1;")
    assert ret.startswith("(async () =>")
    awaited, _ = wrap_script("await sleep(1);\ndocument.title")
    assert "return document.title" in awaited
    inner, _ = wrap_script("const t = [1, 2].find(b => { return b > 1 });\nt ? 'y' : 'n'")
    assert "return t ? 'y' : 'n'" in inner
    fn, note = wrap_script("() => document.title")
    assert "(() => document.title)()" in fn and note


def test_eval_error_marker():
    assert eval_error_of({"__ccEvalError": "TypeError: x"}) == "TypeError: x"
    assert eval_error_of({"a": 1}) is None

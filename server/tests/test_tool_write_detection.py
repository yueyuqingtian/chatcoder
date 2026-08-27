"""工具伪装写盘检测测试（v25）。

复现缺陷：模型用 terminal_exec（重定向 / Set-Content / Out-File / python open 等）
改文件时不在 _WRITE_TOOLS 白名单，原逻辑不留任何写盘记录 → 前端展开看不到文件变更。
本测试覆盖命令解析、路径归一化与变更检测的纯逻辑（与 agent_loop 实际接线一致）。
"""
from pathlib import Path

from app.services.rollback_service import (
    _diff_stats,
    extract_shell_write_paths,
    resolve_command_write_paths,
)


# ── 重定向写盘 ──


def test_redirect_write_paths():
    assert extract_shell_write_paths("echo hello > out.txt") == ["out.txt"]
    assert extract_shell_write_paths("echo x >> logs/app.log") == ["logs/app.log"]
    assert extract_shell_write_paths("findstr foo bar.txt > result.txt 2> err.txt") == ["result.txt", "err.txt"]
    assert extract_shell_write_paths("echo hi > \"my file.txt\"") == ["my file.txt"]
    assert extract_shell_write_paths("findstr x . > nul") == []


def test_redirect_excludes_fd_and_device():
    # >&1 / >&2 是 fd 引用不是文件；空设备写入不登记
    assert extract_shell_write_paths("command 2>&1 | findstr x") == []
    assert extract_shell_write_paths("foo > /dev/null") == []


# ── PowerShell 写盘 cmdlet ──


def test_cmdlet_write_paths():
    assert extract_shell_write_paths("Set-Content -Path config.json -Value '{}'") == ["config.json"]
    assert extract_shell_write_paths("Set-Content -Path config.json -Value \"{}\"") == ["config.json"]
    assert extract_shell_write_paths("Add-Content f.txt \"line\"") == ["f.txt"]
    assert extract_shell_write_paths("'text' | Out-File -FilePath out.txt") == ["out.txt"]
    assert extract_shell_write_paths("New-Item -Path newfile.txt -ItemType File") == ["newfile.txt"]
    assert extract_shell_write_paths("Set-Content -Value x -Path data.json") == ["data.json"]


# ── POSIX 写命令 ──


def test_posix_write_paths():
    assert extract_shell_write_paths("echo hi | tee tee_out.txt") == ["tee_out.txt"]
    assert extract_shell_write_paths("touch new.txt") == ["new.txt"]
    assert extract_shell_write_paths("curl -o dl.bin https://example.com/x") == ["dl.bin"]
    assert extract_shell_write_paths("curl --output a.bin https://example.com/x") == ["a.bin"]
    assert extract_shell_write_paths("wget -O w.bin https://example.com/x") == ["w.bin"]


# ── 内联脚本写文件 ──


def test_inline_script_write_paths():
    assert extract_shell_write_paths("python -c \"open('data.json','w').write('{}')\"") == ["data.json"]
    assert extract_shell_write_paths("node -e \"fs.writeFileSync('out.js','x')\"") == ["out.js"]


# ── 路径归一化（相对工作区 + 越界剔除）──


def test_resolve_command_write_paths_normalizes(workspace):
    args = {"command": "Set-Content -Path src/main.py -Value x", "cwd": ""}
    assert resolve_command_write_paths("terminal_exec", args, str(workspace)) == ["src/main.py"]


def test_resolve_with_cwd_prefix(workspace):
    (workspace / "sub").mkdir()
    args = {"command": "Set-Content -Path note.txt -Value x", "cwd": "sub"}
    assert resolve_command_write_paths("terminal_exec", args, str(workspace)) == ["sub/note.txt"]


def test_resolve_absolute_inside_workspace(workspace):
    target = workspace / "abs.txt"
    args = {"command": f"echo x > {target}", "cwd": ""}
    assert resolve_command_write_paths("terminal_exec", args, str(workspace)) == ["abs.txt"]


def test_resolve_escapes_workspace_rejected(workspace):
    args = {"command": "echo x > ../../etc/passwd", "cwd": ""}
    assert resolve_command_write_paths("terminal_exec", args, str(workspace)) == []
    outside = Path(workspace).parent / "outside.txt"
    args = {"command": f"echo x > {outside}", "cwd": ""}
    assert resolve_command_write_paths("terminal_exec", args, str(workspace)) == []


def test_resolve_non_terminal_tool_returns_empty(workspace):
    assert resolve_command_write_paths("ci_run", {"check": "build"}, str(workspace)) == []
    assert resolve_command_write_paths("fs_write", {"path": "a.txt"}, str(workspace)) == []


# ── 变更检测（复现"缺 diff"场景：伪装写盘应被识别为真实变更）──


def test_disguised_write_detected_as_change(workspace):
    """terminal_exec 写文件 → 前后内容对比应识别变更并产生行级统计。

    这正是此前"模型改了文件、前端看不到变更"的缺失环节：
    识别出路径 + 前后内容不同 → 才可能登记 RollbackWrite，前端才有 diff 可展示。
    """
    args = {"command": "Set-Content -Path code.py -Value \"print('v2')\"", "cwd": ""}
    paths = resolve_command_write_paths("terminal_exec", args, str(workspace))
    assert paths == ["code.py"]

    # 执行前快照（文件尚不存在 → before=None）
    before = None
    # 模拟命令执行写入
    (workspace / "code.py").write_text("print('v2')\n", encoding="utf-8")
    after = (workspace / "code.py").read_text(encoding="utf-8")

    assert before != after  # 变更被识别
    add, dele = _diff_stats(before, after)
    assert add == 1 and dele == 0  # 新增文件全为 + 行


def test_noop_write_ignored(workspace):
    """命令写相同内容（mtime 变化但内容一致）不应登记为变更。"""
    f = workspace / "same.txt"
    f.write_text("keep", encoding="utf-8")
    args = {"command": "Set-Content -Path same.txt -Value keep", "cwd": ""}
    paths = resolve_command_write_paths("terminal_exec", args, str(workspace))
    before = f.read_text(encoding="utf-8")
    f.write_text("keep", encoding="utf-8")
    after = f.read_text(encoding="utf-8")
    assert paths == ["same.txt"]
    assert before == after  # 无变更 → 不登记

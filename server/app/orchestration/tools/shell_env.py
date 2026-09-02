"""v1.2: 终端 shell 解析（agent 的 terminal_exec 与交互终端共用同一套规则）。

与 electron/pty.cjs 保持一致：支持设置项 terminal_shell = auto / pwsh / powershell / cmd / git-bash；
Windows 默认按存在性探测 pwsh → powershell → cmd（避免直接 spawn 失败）。
提供 shell_hint() 供上下文管理器把"当前 shell 是什么、能用什么命令"注入系统提示词，
防止 agent 在 cmd 里写 PowerShell 命令、或写 bash 才有的语法。
"""
import os
import shutil
import sys
from pathlib import Path

# 与 electron/pty.cjs 相同的 git-bash 候选安装路径
_GIT_BASH_CANDIDATES = [
    os.environ.get("PROGRAMFILES", "") + r"\Git\bin\bash.exe",
    os.environ.get("PROGRAMFILES(X86)", "") + r"\Git\bin\bash.exe",
    os.environ.get("LOCALAPPDATA", "") + r"\Programs\Git\bin\bash.exe",
]


def _user_config() -> dict:
    """读取持久化用户配置（terminal_shell 设置）。失败返回空 dict。"""
    try:
        import json
        path = Path(
            os.environ.get("CHATCODER_USER_CONFIG", str(Path.home() / ".chatcoder" / "config.json"))
        )
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def resolve_git_bash() -> str | None:
    """定位 git-bash 的 bash.exe；找不到返回 None。"""
    for p in _GIT_BASH_CANDIDATES:
        if p and Path(p).exists():
            return p
    return shutil.which("bash")


def resolve_win_default() -> str:
    """Windows 默认 shell：pwsh → powershell → cmd（存在性探测）。"""
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    candidates = [
        sys32 / "pwsh.exe",
        sys32 / "powershell.exe",
        sys32 / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        sys32 / "cmd.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # SystemRoot 缺失等异常环境：退化为 cmd
    return "cmd.exe"


def resolve_shell() -> str:
    """返回 terminal_exec 实际执行的 shell（可执行文件路径/文件名）。"""
    want = (_user_config().get("terminal_shell") or "auto").strip().lower()
    if want in ("pwsh", "powershell"):
        return want + ".exe"
    if want == "cmd":
        return "cmd.exe"
    if want == "git-bash":
        return resolve_git_bash() or "bash"
    if sys.platform == "win32":
        return resolve_win_default()
    return os.environ.get("SHELL", "bash")


def shell_kind() -> str:
    """shell 类别：pwsh / powershell / cmd / git-bash / unix。"""
    shell = resolve_shell()
    base = Path(shell).name.lower()
    if "pwsh" in base:
        return "pwsh"
    if "powershell" in base:
        return "powershell"
    if base in ("cmd", "cmd.exe"):
        return "cmd"
    if base in ("bash", "bash.exe", "sh", "zsh"):
        return "git-bash" if "bash" in base else "unix"
    if base in ("sh", "zsh"):
        return "unix"
    if base.endswith(".exe"):
        return "cmd"
    return "unix"


def shell_label() -> str:
    """人话标签（用于提示词）。"""
    kind = shell_kind()
    return {
        "pwsh": "PowerShell 7 (pwsh)",
        "powershell": "Windows PowerShell",
        "cmd": "cmd.exe",
        "git-bash": "Git Bash (bash)",
        "unix": "POSIX shell (bash/sh)",
    }[kind]


def shell_hint() -> str:
    """注入系统提示词的环境说明，告诉 agent 当前 shell 与可用命令。

    目标：消除三类常见失败——
    1) 在 cmd 里用 PowerShell 命令（Get-ChildItem … 不是内部或外部命令）；
    2) 在 PowerShell/cmd 里用 bash 命令（grep/find/ls -la … 不存在或语法不同）；
    3) 用 /dev/null 重定向（Windows 无此设备）。
    """
    kind = shell_kind()
    common = (
        "\n## Shell 环境\n"
        f"- 当前 shell: {shell_label()}。命令会按该 shell 的真实语法执行，请勿假设是 bash。\n"
        "- 内容搜索请优先使用 fs_grep 工具（支持正则、可过滤目录/扩展名），"
        "不要在终端里依赖 grep/ripgrep——Windows 机器通常没有这些命令。\n"
        "- 长驻进程（dev server、watch、后端服务）用 terminal_exec 的 waitForCompletion=false "
        "后台启动（返回 shell_id），用 terminal_bg_status 查日志、terminal_bg_kill 终止；"
        "长命令（安装/构建/测试）用 timeout 参数延长等待。"
    )
    if kind in ("pwsh", "powershell"):
        return common + (
            "\n- 可用: Get-ChildItem/dir/ls, Get-Content/cat/type, findstr, Select-String, "
            "Get-Command, git, node, npm, python。\n"
            "- 禁止: bash 语法命令（grep/find/sed/awk 不存在；ls -la 这类短横线参数不生效）。\n"
            "- 静默丢弃输出请用 `> $null`（或 `> nul`），不要用 `> /dev/null`。"
        )
    if kind == "git-bash":
        return common + (
            "\n- 可用 Unix 命令: ls, grep, find, cat, sed, awk, git, node, npm, python。\n"
            "- 静默丢弃输出用 `> /dev/null 2>&1`。"
        )
    # cmd
    return common + (
        "\n- 可用: dir, type, findstr, where, cd, git, node, npm, python。\n"
        "- 禁止: PowerShell 命令（Get-ChildItem/Select-String 等）和 Unix 命令（grep/find/ls -la）。\n"
        "- 静默丢弃输出请用 `> nul`（Windows 写法），不要用 `> /dev/null`。"
    )

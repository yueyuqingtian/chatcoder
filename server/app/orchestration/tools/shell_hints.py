"""终端命令的小幅纠正与失败提示（plan-20261020-pts）。

真实库里 terminal_exec 的失败大头是「退出码非 0」，其中相当一部分是 shell 用错了：
在 PowerShell 里写 `| head -50`、`grep`、`ls -la`，给 Select-String 传不存在的 -Recurse，
在 cmd 里写 Get-ChildItem / $变量，在非 git 目录跑 git……模型只看到「退出码 1」和一段报错，
常常换个写法再错一次。这里按「命令 + 报错」识别这些固定模式，把正确写法直接附在失败结果后面。
"""
from __future__ import annotations

import re

_LABELS = {
    "pwsh": "PowerShell 7",
    "powershell": "Windows PowerShell 5",
    "cmd": "cmd.exe",
    "git-bash": "Git Bash",
    "unix": "POSIX shell",
}


def rewrite_null_device(command: str, kind: str) -> tuple[str, bool]:
    """PowerShell / cmd 下把 `> /dev/null` 改写为 `$null` / `nul`（Windows 没有 /dev/null 设备）。"""
    if kind not in ("pwsh", "powershell", "cmd"):
        return command, False
    target = "$null" if kind != "cmd" else "nul"
    new = re.sub(r"(\d?>>?)\s*/dev/null\b", lambda m: f"{m.group(1)}{target}", command)
    return new, new != command


_NOT_FOUND = re.compile(
    r"术语\s*[“\"']?(?P<a>[^\s“”\"']+)[”\"']?\s*不会被识别"
    r"|The term '(?P<b>[^']+)' is not recognized"
    r"|'(?P<c>[^']+)'\s*不是内部或外部命令"
    r"|'(?P<d>[^']+)' is not recognized as an internal or external command"
    r"|(?P<e>[\w.-]+): command not found",
    re.I,
)


def failure_hint(command: str, output: str, kind: str) -> str:
    """返回以「提示：」开头的若干行（无匹配时返回空串）。最多给两条，避免喧宾夺主。"""
    cmd = command or ""
    out = output or ""
    label = _LABELS.get(kind, kind)
    is_ps = kind in ("pwsh", "powershell")
    hints: list[str] = []

    def add(text: str) -> None:
        if text not in hints:
            hints.append(text)

    if is_ps and re.search(r"\bSelect-String\b[^|;]*\s-Recurse\b", cmd, re.I):
        add("Select-String 没有 -Recurse 参数：用 `Get-ChildItem -Recurse -Include *.java | Select-String -Pattern 'x'`，"
            "或直接用 fs_grep 工具（更快，可按扩展名过滤）。")
    if is_ps and re.search(r"(?:\|\s*|^\s*)(?:head|tail)\b", cmd):
        add("PowerShell 没有 head / tail：用 `| Select-Object -First N` / `-Last N`，读文件末尾用 `Get-Content 文件 -Tail N`。")
    if kind in ("pwsh", "powershell", "cmd") and re.search(r"(?:^|[|;&]\s*)(?:grep|sed|awk|xargs)\b", cmd):
        add(f"当前 shell 是 {label}，没有 grep / sed / awk：搜索文件内容请用 fs_grep 工具"
            + ("，或 Select-String。" if is_ps else "，或 findstr。"))
    if is_ps and re.search(r"(?:^|[|;&]\s*)ls\s+-[a-zA-Z]{2,}", cmd):
        add("PowerShell 的 ls 是 Get-ChildItem 的别名，不支持 -la 这类参数：用 `Get-ChildItem -Force`。")
    if kind == "powershell" and re.search(r"&&|\|\|", cmd):
        add("Windows PowerShell 5 不支持 && / ||：用 ; 分隔，或写成 `命令1; if ($?) { 命令2 }`。")
    if is_ps and re.search(r"-Encoding\s+Byte\b", cmd, re.I) and re.search(r"Byte", out):
        add("PowerShell 7 读字节请用 `-AsByteStream`（-Encoding Byte 已移除）。")
    if kind == "cmd" and (
        re.search(r"\b(?:Get|Set|Select|Where|ForEach|Test|New|Remove|Out|Write)-[A-Z]\w+", cmd)
        or re.search(r"(?:^|[\s;(])\$\w+", cmd)
    ):
        add("当前 shell 是 cmd.exe：Get-ChildItem、Select-String、$变量 等 PowerShell 写法不可用；"
            "改用 dir / findstr，或整体包一层 `powershell -NoProfile -Command \"…\"`。")
    if re.search(r"not a git repository|不是 git 仓库", out, re.I):
        add("当前目录不是 git 仓库：用 cwd 参数指定仓库所在目录（多仓库工作区里先用 fs_list 看清各仓库）。")
    if not hints:
        m = _NOT_FOUND.search(out)
        if m:
            name = next(g for g in m.groups() if g)
            add(f"`{name}` 在当前环境不存在（未安装或不在 PATH）；当前 shell 是 {label}，请换用该 shell 的等价命令。")
    if not hints:
        return ""
    return "\n" + "\n".join(f"提示：{h}" for h in hints[:2])

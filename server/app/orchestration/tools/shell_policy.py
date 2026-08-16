"""v2.2 (对齐 zcode 3.12): shell 命令静态安全分级。

ZCode 的 analyzeBashCommand/classifySafeCommandIdentity 思路简化版：
不解析 AST（Python 侧无 shell parser），用管道分段 + 首 token 匹配两级判定：

- allow：只读白名单命令（ls/cat/git status…）→ 免审批直接执行；
- deny ：危险黑名单（rm -rf / format / 磁盘清理…）→ 直接拒绝，不执行、不审批；
- ask  ：其余命令 → 走原有审批门（risk=high）+ exec_policy 前缀规则。

每段管道单独判定，任一段 deny 即整体 deny；全部 allow 才整体 allow。
"""

from __future__ import annotations

import re

# 只读白名单：{首 token: 允许的子命令集合（None = 不限子命令）}
_READONLY_WHITELIST: dict[str, set[str] | None] = {
    "ls": None,
    "dir": None,
    "cat": None,
    "type": None,
    "echo": None,
    "pwd": None,
    "cd": None,
    "chdir": None,
    "where": None,
    "which": None,
    "whoami": None,
    "hostname": None,
    "env": None,
    "set": None,
    "printenv": None,
    "rg": None,
    "findstr": None,
    "grep": None,
    "find": None,
    "tree": None,
    "wc": None,
    "head": None,
    "tail": None,
    "sort": None,
    "uniq": None,
    "date": None,
    "time": None,
    "ps": None,
    "tasklist": None,
    "netstat": None,
    "ipconfig": None,
    "ping": None,
    "nslookup": None,
    "node": {"--version", "-v"},
    "npm": {"--version", "-v", "list", "ls", "view", "info"},
    "pnpm": {"--version", "-v", "list", "ls"},
    "yarn": {"--version", "-v", "list"},
    "python": {"--version", "-V"},
    "python3": {"--version", "-V"},
    "pip": {"list", "show", "--version", "-V"},
    "git": {
        "status", "log", "diff", "show", "branch", "tag", "remote", "rev-parse",
        "ls-files", "ls-tree", "describe", "blame", "shortlog",
        "count-objects", "fsck", "name-rev", "reflog", "submodule",
    },
    "gh": {"status", "pr", "issue", "repo", "run"},
    "curl": {"-i", "-I", "--head"},
    "wget": {"--spider"},
    "docker": {"ps", "images", "inspect", "logs", "stats", "version"},
    "kubectl": {"get", "describe", "logs", "top", "explain", "version"},
    "get-content": None,
    "get-childitem": None,
    "get-location": None,
    "get-process": None,
    "get-service": None,
    "select-string": None,
    "write-output": None,
    "test-path": None,
    "resolve-path": None,
    "split-path": None,
    "get-item": None,
    "get-command": None,
    "measure-object": None,
}

# 危险黑名单：命中即整体拒绝（regex 匹配原始命令行）
_DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive)", re.I),
    re.compile(r"\brm\s+(-[a-z]*f|--force)\b", re.I),
    re.compile(r"\bRemove-Item\b.*\b-Recurse\b", re.I),
    re.compile(r"\bdel\s+/[sfq]", re.I),
    re.compile(r"\brmdir\s+/s", re.I),
    re.compile(r"\bformat\b", re.I),
    re.compile(r"\bdiskpart\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r"\bshutdown\b", re.I),
    re.compile(r"\breboot\b", re.I),
    re.compile(r"\bStop-Computer\b", re.I),
    re.compile(r"\bRestart-Computer\b", re.I),
    re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s+\.)", re.I),
    re.compile(r">\s*(/dev/|nul\b)", re.I),
    re.compile(r"\bchmod\s+-R\s+777\b", re.I),
    re.compile(r"\bicacls\b", re.I),
    re.compile(r"\brmdir\b.*\b/s\b", re.I),
]

# 管道分隔（考虑引号内管道符误判：简单处理——按常见引号保护）
_PIPE_SPLIT_RE = re.compile(r'[|](?=(?:[^"\']|"[^"]*"|\'[^\']*\')*$)')


def analyze(command: str) -> tuple[str, str]:
    """静态分析命令。返回 (verdict, reason)：allow / deny / ask。"""
    cmd = (command or "").strip()
    if not cmd:
        return "deny", "命令为空"

    # 1. 危险黑名单（原始命令匹配，任一段命中即 deny）
    for pat in _DANGEROUS_PATTERNS:
        if pat.search(cmd):
            return "deny", f"危险命令已拦截: {pat.pattern[:40]}"

    # 2. 逐段管道判定只读白名单
    segments = [s.strip() for s in _PIPE_SPLIT_RE.split(cmd) if s.strip()]
    if not segments:
        return "deny", "命令为空"

    for seg in segments:
        # 去掉 && / ; 串联（串联命令一律按 ask 处理，防止白名单命令夹带写操作）
        if "&&" in seg or ";" in seg:
            return "ask", "命令含串联操作符"
        tokens = seg.split()
        if not tokens:
            continue
        first = tokens[0]
        # Windows 全路径调用（C:\...\ls.exe）取 basename
        if "\\" in first or "/" in first:
            first = first.replace("\\", "/").rsplit("/", 1)[-1].lower()
        else:
            first = first.lower()
        allowed_subs = _READONLY_WHITELIST.get(first)
        if allowed_subs is None and first not in _READONLY_WHITELIST:
            return "ask", f"命令不在只读白名单: {first}"
        if allowed_subs is not None:
            sub = tokens[1] if len(tokens) > 1 else None
            if sub is not None:
                # 只读子命令集合匹配：git diff / npm list 等；
                # 先原样匹配，再尝试去前导 - 匹配（--version ↔ version 两种写法）
                sub_lower = sub.lower()
                if sub_lower not in allowed_subs and sub_lower.lstrip("-") not in allowed_subs:
                    # 子命令不在只读集 → ask（如 git push / git reset）
                    return "ask", f"{first} 子命令不在只读白名单: {sub}"

    return "allow", "只读安全命令"

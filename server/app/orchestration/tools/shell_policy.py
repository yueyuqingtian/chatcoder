"""v2.2 (对齐 zcode 3.12): shell 命令静态分级。

plan-75-332 语义调整（重要）：本模块**不再做任何拦截**。
改造前 `analyze()` 命中黑名单会直接 deny（连审批机会都没有），命中白名单则免审批——
两者都是"工具自身在替用户做决定"。现在它只回答两个分类问题：

- `risk_note(cmd)`     → 风险命令特征（原黑名单），命中即归 `exec_risky`；
- `delete_note(cmd)`   → 删除操作特征，命中即归 `delete`；
- `is_readonly_command(cmd)` → 只读命令判定（原白名单），命中即归 `exec_readonly`。

是否放行、是否弹审批一律交给 `approval_policy.decide()` 按权限模式裁决：
完全访问下 `rm -rf` 可执行（用户明确要求），自动审批下它进审批卡而不是被硬拒。

`analyze()` 保留原签名与三态语义，作为向后兼容的薄封装（deny 仅在描述风险时返回，
调用方不应再据此拒绝执行）。
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

# ── 风险特征（原"危险黑名单"）：不可逆、影响系统范围或破坏版本历史 ──
# 命中 → exec_risky（自动审批下仍需人工确认；完全访问下放行）。
_RISK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive)", re.I), "递归强制删除（rm -rf）"),
    (re.compile(r"\bRemove-Item\b.*\b-Recurse\b", re.I), "递归删除（Remove-Item -Recurse）"),
    (re.compile(r"\bdel\s+/[sfq]", re.I), "静默/递归删除（del /s /f /q）"),
    (re.compile(r"\brmdir\s+/s", re.I), "递归删除目录（rmdir /s）"),
    (re.compile(r"\bformat\b", re.I), "格式化磁盘（format）"),
    (re.compile(r"\bdiskpart\b", re.I), "磁盘分区操作（diskpart）"),
    (re.compile(r"\bmkfs\b", re.I), "创建文件系统（mkfs）"),
    (re.compile(r"\bdd\s+if=", re.I), "裸设备写入（dd if=）"),
    (re.compile(r"\bshutdown\b", re.I), "关机（shutdown）"),
    (re.compile(r"\breboot\b", re.I), "重启（reboot）"),
    (re.compile(r"\bStop-Computer\b", re.I), "关机（Stop-Computer）"),
    (re.compile(r"\bRestart-Computer\b", re.I), "重启（Restart-Computer）"),
    (re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s+\.)", re.I), "Git 破坏性操作（丢失未提交改动）"),
    (re.compile(r">\s*/dev/", re.I), "重定向到 /dev/（Windows 无此设备，会产生垃圾文件）"),
    (re.compile(r"\bchmod\s+-R\s+777\b", re.I), "开放全部权限（chmod -R 777）"),
    (re.compile(r"\bicacls\b", re.I), "修改文件访问控制（icacls）"),
]

# ── 删除特征（非递归、破坏面局限在目标路径）──
_DELETE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[a-z]+\s+)*[^\s-]", re.I), "删除文件（rm）"),
    (re.compile(r"\bRemove-Item\b", re.I), "删除文件/目录（Remove-Item）"),
    (re.compile(r"\b(?:del|erase)\s+", re.I), "删除文件（del）"),
    (re.compile(r"\b(?:rmdir|rd)\s+", re.I), "删除目录（rmdir）"),
    (re.compile(r"\bunlink\s+", re.I), "删除文件（unlink）"),
]

# 管道分隔（考虑引号内管道符误判：简单处理——按常见引号保护）
_PIPE_SPLIT_RE = re.compile(r'[|](?=(?:[^"\']|"[^"]*"|\'[^\']*\')*$)')


def risk_note(command: str) -> str:
    """风险特征说明；未命中返回空串（调用方据此归为 exec_risky）。"""
    cmd = (command or "").strip()
    if not cmd:
        return ""
    for pat, note in _RISK_PATTERNS:
        if pat.search(cmd):
            return note
    return ""


def delete_note(command: str) -> str:
    """删除操作说明；未命中返回空串。

    风险特征优先：`rm -rf` 既是删除也是风险，由 `risk_note` 先命中归为 exec_risky，
    审批卡会按"风险命令"而非"删除"呈现（破坏性更强，文案更醒目）。
    """
    cmd = (command or "").strip()
    if not cmd or risk_note(cmd):
        return ""
    for pat, note in _DELETE_PATTERNS:
        if pat.search(cmd):
            return note
    return ""


def is_readonly_command(command: str) -> bool:
    """只读命令判定（原白名单逻辑）。

    逐段管道判定：含串联操作符（&& / ;）或输出重定向一律不算只读
    （白名单命令夹带写操作是典型的绕过手法）；每段首 token 及其子命令都需在
    只读集内，全部段落满足才算只读。

    含删除/风险特征时直接判否，避免 `cat x; rm y` 这类混淆被误判。
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    if risk_note(cmd) or delete_note(cmd):
        return False

    segments = [s.strip() for s in _PIPE_SPLIT_RE.split(cmd) if s.strip()]
    if not segments:
        return False

    for seg in segments:
        # 串联命令一律不视为只读（防白名单命令夹带写操作）
        if "&&" in seg or ";" in seg:
            return False
        # 输出重定向（> / >>）不算只读；`> $null` / `> nul` / `2>&1` 等安全静默输出除外
        clean_seg = re.sub(r">\s*(?:\$null|nul|&\d+)", "", seg, flags=re.I)
        if ">" in clean_seg:
            return False
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
            return False
        if allowed_subs is not None:
            sub = tokens[1] if len(tokens) > 1 else None
            if sub is not None:
                # 先原样匹配，再尝试去前导 - 匹配（--version ↔ version 两种写法）
                sub_lower = sub.lower()
                if sub_lower not in allowed_subs and sub_lower.lstrip("-") not in allowed_subs:
                    return False
    return True


def analyze(command: str) -> tuple[str, str]:
    """兼容包装：返回 (verdict, reason)，verdict ∈ allow / deny / ask。

    语义已变更（plan-75-332）：`deny` 仅表示"命中风险特征"，**不再**代表拒绝执行。
    新的裁决方 `approval_policy.decide()` 会把它当作 exec_risky 分类；保留三态返回值
    只为不破坏既有调用点与测试的签名约定。
    """
    cmd = (command or "").strip()
    if not cmd:
        return "deny", "命令为空"

    note = risk_note(cmd)
    if note:
        return "deny", f"风险命令: {note}"

    if is_readonly_command(cmd):
        return "allow", "只读安全命令"

    del_note = delete_note(cmd)
    if del_note:
        return "ask", del_note

    return "ask", "命令不在只读白名单"

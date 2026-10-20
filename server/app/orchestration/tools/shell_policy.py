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
    # plan-20261020-pts：常见的只读查询（版本 / 环境信息）
    "java": {"-version", "--version"},
    "javac": {"-version", "--version"},
    "mvn": {"-v", "--version"},
    "gradle": {"-v", "--version"},
    "dotnet": {"--info", "--version", "--list-sdks", "--list-runtimes"},
    "go": {"version", "env"},
    "systeminfo": None,
    "ver": None,
}

# plan-20261020-pts：PowerShell 只读 cmdlet 与常用别名——纯查询、筛选、格式化，不改文件与系统状态。
# 此前计划 / 只读模式下 Select-Object、Where-Object、Format-Table 这类管道整形都被判为「可能写文件」拒绝，
# 真实库里约 35 次。ForEach-Object / Where-Object 的脚本块另做内容检查（见 _readonly_block）。
_PS_READONLY: frozenset[str] = frozenset({
    "select-object", "sort-object", "group-object", "measure-object", "where-object", "foreach-object",
    "format-table", "format-list", "format-wide", "format-custom", "format-hex",
    "out-string", "out-null", "out-host", "write-host", "write-output", "write-verbose",
    "convertto-json", "convertfrom-json", "convertto-csv", "convertfrom-csv",
    "get-itemproperty", "get-itempropertyvalue", "get-date", "get-filehash", "get-member", "get-help",
    "get-alias", "get-module", "get-psdrive", "get-variable", "get-culture", "get-timezone", "get-host",
    "get-uptime", "get-acl", "get-ciminstance", "get-wmiobject", "get-nettcpconnection", "get-netipaddress",
    "get-netadapter", "get-volume", "get-disk", "get-hotfix", "get-computerinfo", "get-history",
    "get-psprovider", "get-executionpolicy", "get-eventlog", "get-winevent", "get-random", "get-unique",
    "test-netconnection", "test-connection", "resolve-dnsname", "compare-object", "select-xml", "join-path",
    "start-sleep", "set-location", "push-location", "pop-location", "sl", "pushd", "popd",
    # 常用别名（只读）
    "select", "sort", "where", "foreach", "%", "?", "gci", "gc", "gi", "gp", "gcm", "sls", "measure",
    "group", "ft", "fl", "fw", "write", "sleep",
})

# 脚本块 / 表达式里允许出现的语句关键字（结构本身不执行命令，内部内容另行检查）
_PS_KEYWORDS: frozenset[str] = frozenset({
    "if", "else", "elseif", "foreach", "for", "while", "do", "until", "switch", "return", "break", "continue",
    "try", "catch", "finally", "throw", "in",
})

# 表达式里允许调用的方法（字符串 / 数组 / 数值的只读操作）；其余方法（Delete / MoveTo / Kill / Start …）一律不放行
_SAFE_METHODS: frozenset[str] = frozenset({
    "tostring", "trim", "trimstart", "trimend", "substring", "replace", "split", "tolower", "toupper",
    "tolowerinvariant", "toupperinvariant", "startswith", "endswith", "contains", "indexof", "lastindexof",
    "padleft", "padright", "join", "format", "gettype", "equals", "compareto", "toarray", "where", "foreach",
    "getbytes", "getstring", "toint32", "toint64", "todouble", "round", "floor", "ceiling", "abs", "min", "max",
    "matches", "match", "ismatch", "getfilename", "getfilenamewithoutextension", "getextension",
    "getdirectoryname", "combine", "getfullpath", "exists", "getvalue", "tobase64string", "frombase64string",
    "tochararray", "insert",
})

# 允许的静态类型调用（[类型]::成员）；文件 / 目录类型只允许读取类成员
_SAFE_STATIC_TYPES: frozenset[str] = frozenset({
    "math", "system.math", "string", "system.string", "int", "long", "double", "decimal", "datetime",
    "timespan", "convert", "system.convert", "char", "regex", "system.text.regularexpressions.regex",
    "io.path", "system.io.path", "text.encoding", "system.text.encoding", "bitconverter",
    "system.bitconverter", "environment", "system.environment",
})
_FILE_STATIC_TYPES: frozenset[str] = frozenset({
    "io.file", "system.io.file", "io.directory", "system.io.directory",
})
_FILE_READ_MEMBERS = re.compile(
    r"^(?:readall\w*|readlines|exists|getlastwritetime\w*|getcreationtime\w*|getattributes|getfiles|"
    r"getdirectories|getfilesystementries|enumerate\w+)$",
    re.I,
)
# 参数里出现即视为写盘（输出到文件 / 复制到目标）
_WRITE_PARAMS = re.compile(r"(?<![\w-])-(?:OutFile|Destination|DestinationPath|LiteralDestination)\b", re.I)
_STATEMENT_SEPS = ("&&", "||")

# ── 风险特征（原"危险黑名单"）：不可逆、影响系统范围或破坏版本历史 ──
# 命中 → exec_risky（自动审批下仍需人工确认；完全访问下放行）。
_RISK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive)", re.I), "递归强制删除（rm -rf）"),
    (re.compile(r"\bRemove-Item\b.*\b-Recurse\b", re.I), "递归删除（Remove-Item -Recurse）"),
    (re.compile(r"\bdel\s+/[sfq]", re.I), "静默/递归删除（del /s /f /q）"),
    (re.compile(r"\brmdir\s+/s", re.I), "递归删除目录（rmdir /s）"),
    # plan-20261020-pts：只认「format 盘符:」与 Format-Volume 等磁盘 cmdlet。
    # 旧写法 \bformat\b 把 Format-Table / Format-List / git log --format 都当成格式化磁盘（真实库里误拦 10 次）
    (re.compile(r"(?<![\w.-])format(?:\.com|\.exe)?\s+(?:/\S+\s+)*[a-z]:(?!\w)", re.I), "格式化磁盘（format）"),
    (re.compile(r"\b(?:Format-Volume|Clear-Disk|Initialize-Disk)\b", re.I), "格式化 / 清空磁盘"),
    (re.compile(r"\bdiskpart\b", re.I), "磁盘分区操作（diskpart）"),
    (re.compile(r"\bmkfs\b", re.I), "创建文件系统（mkfs）"),
    (re.compile(r"\bdd\s+if=", re.I), "裸设备写入（dd if=）"),
    (re.compile(r"\bshutdown\b", re.I), "关机（shutdown）"),
    (re.compile(r"\breboot\b", re.I), "重启（reboot）"),
    (re.compile(r"\bStop-Computer\b", re.I), "关机（Stop-Computer）"),
    (re.compile(r"\bRestart-Computer\b", re.I), "重启（Restart-Computer）"),
    (re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s+\.)", re.I), "Git 破坏性操作（丢失未提交改动）"),
    # plan-20261020-pts：> /dev/null 只是丢弃输出（终端在 PowerShell / cmd 下会改写成 $null / nul），不算风险
    (re.compile(r">\s*/dev/(?!null\b)", re.I), "重定向到 /dev/ 设备"),
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


_PLACE_STR = "\x01"  # 字符串字面量占位
_PLACE_GRP = "\x02"  # (…) / $(…) / @(…) 占位
_PLACE_BLK = "\x03"  # {…} 脚本块 / @{…} 哈希表占位
_MAX_NEST = 6


def _powershell_shell() -> bool:
    try:
        from app.orchestration.tools.shell_env import shell_kind

        return shell_kind() in ("pwsh", "powershell")
    except Exception:  # noqa: BLE001
        return True


def _skip_quote(text: str, j: int) -> int | None:
    """j 指向引号；返回配对的收尾引号下标（PowerShell：'' / "" 双写与反引号转义）。不配对返回 None。"""
    q = text[j]
    n = len(text)
    k = j + 1
    while k < n:
        c = text[k]
        if c == "`" and q == '"':
            k += 2
            continue
        if c == "\\" and q == '"' and not _powershell_shell():
            k += 2
            continue
        if c == q:
            if k + 1 < n and text[k + 1] == q:
                k += 2
                continue
            return k
        k += 1
    return None


def _take_balanced(text: str, start: int, open_ch: str, close_ch: str) -> int | None:
    """start 指向 open_ch，返回配对 close_ch 的下标（跳过引号）；不配对返回 None。"""
    depth = 0
    j, n = start, len(text)
    while j < n:
        c = text[j]
        if c in ("'", '"'):
            k = _skip_quote(text, j)
            if k is None:
                return None
            j = k + 1
            continue
        if c == "`":
            j += 2
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def _dq_subexprs(body: str) -> list[str] | None:
    """双引号字符串里的 $(…) 子表达式（会执行命令）。"""
    out: list[str] = []
    j = 0
    while True:
        j = body.find("$(", j)
        if j < 0:
            return out
        end = _take_balanced(body, j + 1, "(", ")")
        if end is None:
            return None
        out.append(body[j + 2:end])
        j = end + 1


def _scan_statements(text: str) -> tuple[list[str], list[tuple[str, str]]] | None:
    """把命令切成顶层「简单段」，嵌套内容抽出来单独检查。

    段：按顶层的 | ; && || 换行 与单独的 & 切分；段内的字符串、(…) / $(…) / @(…)、{…} 换成占位符，
    双引号字符串里的 $(…) 子表达式同样抽出来检查。引号 / 括号不配对返回 None（按非只读处理）。
    """
    segs: list[str] = []
    groups: list[tuple[str, str]] = []
    buf: list[str] = []
    ps = _powershell_shell()
    i, n = 0, len(text)

    def flush() -> None:
        s = "".join(buf).strip()
        if s:
            segs.append(s)
        buf.clear()

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c in ("'", '"'):
            k = _skip_quote(text, i)
            if k is None:
                return None
            if c == '"':
                subs = _dq_subexprs(text[i + 1:k])
                if subs is None:
                    return None
                groups.extend(("paren", s) for s in subs)
            buf.append(_PLACE_STR)
            i = k + 1
            continue
        if c == "`":
            if not ps:
                return None  # bash 的反引号是命令替换，无法静态判定
            buf.append(text[i:i + 2])
            i += 2
            continue
        if c == "$" and nxt == "{":
            k = text.find("}", i)
            if k < 0:
                return None
            buf.append("$v")
            i = k + 1
            continue
        if c in "$@" and nxt == "(":
            end = _take_balanced(text, i + 1, "(", ")")
            if end is None:
                return None
            groups.append(("paren", text[i + 2:end]))
            buf.append(_PLACE_GRP)
            i = end + 1
            continue
        if c == "@" and nxt == "{":
            end = _take_balanced(text, i + 1, "{", "}")
            if end is None:
                return None
            groups.append(("hash", text[i + 2:end]))
            buf.append(_PLACE_BLK)
            i = end + 1
            continue
        if c == "(":
            end = _take_balanced(text, i, "(", ")")
            if end is None:
                return None
            groups.append(("paren", text[i + 1:end]))
            buf.append(_PLACE_GRP)
            i = end + 1
            continue
        if c == "{":
            end = _take_balanced(text, i, "{", "}")
            if end is None:
                return None
            groups.append(("block", text[i + 1:end]))
            buf.append(_PLACE_BLK)
            i = end + 1
            continue
        if text.startswith(_STATEMENT_SEPS, i):
            flush()
            i += 2
            continue
        if c in "|;\n":
            flush()
            i += 1
            continue
        if c == "&" and "".join(buf).strip() and not "".join(buf).rstrip().endswith(">"):
            flush()  # cmd 的单个 & 串联（2>&1 这类重定向里的 & 除外）
            i += 1
            continue
        buf.append(c)
        i += 1
    flush()
    return segs, groups


def _readonly_expression(s: str) -> bool:
    """表达式（或命令参数）是否只读：不改对象属性、只调用安全方法与安全静态成员。"""
    # 属性赋值：$x.Prop = … / $_.IsReadOnly = $true 会改动对象（含文件属性）
    if re.search(r"\$[\w:]+(?:\.\w+|\[[^\]]*\])+\s*[+\-*/]?=(?!=)", s):
        return False
    for m in re.finditer(r"\[([\w.]+)\]::(\w+)", s):
        t, member = m.group(1).lower(), m.group(2)
        if t in _SAFE_STATIC_TYPES:
            continue
        if t in _FILE_STATIC_TYPES and _FILE_READ_MEMBERS.match(member):
            continue
        return False
    if "::" in re.sub(r"\[[\w.]+\]::\w+", "", s):
        return False
    for m in re.finditer(r"\.(\w+)\s*(?:\(|" + _PLACE_GRP + ")", s):
        if m.group(1).lower() not in _SAFE_METHODS:
            return False
    return True


def _readonly_segment(seg: str) -> bool:
    s = seg.strip()
    if not s:
        return True
    # 输出重定向（> / >>）不算只读；`> $null` / `> nul` / `2>&1` 等静默输出除外
    clean = re.sub(r"\*?\d?>>?\s*(?:\$null|nul\b|&\d+)", "", s, flags=re.I)
    if ">" in clean:
        return False
    if _WRITE_PARAMS.search(s):
        return False
    # 赋值前缀：$x = … / $x += …（变量赋值本身不改文件）
    m = re.match(r"^\$[\w:]+(?:\[[^\]]*\])?\s*[+\-*/]?=(?!=)\s*", s)
    if m:
        s = s[m.end():].strip()
        if not s:
            return True
    tokens = s.split()
    first = tokens[0]
    if first[0] in "$[0123456789-+!" + _PLACE_STR + _PLACE_GRP + _PLACE_BLK:
        return _readonly_expression(s)
    name = first.lower()
    if "\\" in name or "/" in name:
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if name.endswith(".exe"):
        name = name[:-4]
    rest = s[len(first):]
    if name in _PS_KEYWORDS or name in _PS_READONLY:
        return _readonly_expression(rest)
    if name not in _READONLY_WHITELIST:
        return False
    subs = _READONLY_WHITELIST[name]
    if subs is not None and len(tokens) > 1:
        sub = tokens[1].lower()
        # 先原样匹配，再尝试去前导 - 匹配（--version ↔ version 两种写法）
        if sub not in subs and sub.lstrip("-") not in subs:
            return False
    return _readonly_expression(rest)


def _readonly_text(text: str, depth: int = 0, *, hashtable: bool = False) -> bool:
    if depth > _MAX_NEST:
        return False
    scanned = _scan_statements(text)
    if scanned is None:
        return False
    segs, groups = scanned
    for kind, inner in groups:
        if not _readonly_text(inner, depth + 1, hashtable=(kind == "hash")):
            return False
    for seg in segs:
        if hashtable:
            # 哈希表的「键 = 值」：去掉键，只检查值（Select-Object 计算属性 @{n='x'; e={…}} 很常见）
            seg = re.sub(r"^\s*[\w" + _PLACE_STR + r"]+\s*=\s*", "", seg)
            if not _readonly_expression(seg):
                return False
        elif not _readonly_segment(seg):
            return False
    return True


def is_readonly_command(command: str) -> bool:
    """只读命令判定（plan-20261020-pts 重写）。

    逐段解析：顶层按 | ; && || 换行切段，字符串、(…)、$(…)、{…} 脚本块、@{…} 哈希表逐层抽出递归检查。
    每段的命令须在只读白名单（含 PowerShell 只读 cmdlet）内；表达式与参数里只允许安全的方法调用
    与静态成员、不允许改对象属性；输出重定向（静默到 $null / nul 除外）与 -OutFile 等写盘参数一律判否。
    串联的多条命令（git show a; git show b）每段都只读即视为只读——此前一律判否，计划模式下被误拒。

    含删除 / 风险特征时直接判否，避免 `cat x; rm y` 这类混淆被误判。
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    if risk_note(cmd) or delete_note(cmd):
        return False
    return _readonly_text(cmd)


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

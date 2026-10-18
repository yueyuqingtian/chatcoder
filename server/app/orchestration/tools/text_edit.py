"""编辑类工具共用的文本读写与匹配（plan-20261016-ux 问题 3）。

用户反馈「改某一块时会一直修改失败，换一个工具才成功」。排查到编辑链路三类根因，这里统一收口：

1. 编码与换行（读写对称）：
   - 旧实现 `read_text(encoding="utf-8", errors="replace")`：GBK/GB18030 文件（中文 Java 项目常见）的中文
     被替换成「�」——模型看到的是乱码，写回还会把整份文件的中文毁掉；
   - 旧实现 `write_text()` 在 Windows 上把 `\\n` 全转成 `\\r\\n`：LF 文件每次编辑都被整份改成 CRLF。
   现在按字节读取，识别 BOM / UTF-8 / GB18030 与主换行风格，内存里统一成 `\\n` 处理，
   写回时还原为原编码、原 BOM、原换行。无法可靠解码或含 NUL 的文件拒绝改写，避免损坏。

2. 匹配过死：只认精确子串，行尾空白、Tab/空格缩进差异、把 fs_read 的行号前缀带进 old_text
   都会判「未匹配」。现在精确匹配失败后依次尝试：去行号前缀（含只有部分行带行号的形态）→
   忽略行尾空白 → 忽略缩进与行内空白差异 → 字面转义解码 → **跨行空白归一化**
   （模型常把两行合并成一行、或把一行拆成多行，逐行比对永远对不上），
   且**必须唯一命中**（replace_all 除外）；缩进不同时按文件实际缩进重排 new_text。

3. 失败无线索：旧错误只有「old_text 在文件中未匹配」，模型只能原样重试。现在附上文件里最相近的原文
   片段（带行号）与差异提示（Tab/空格、行尾空白），多处命中时列出行号。
"""
from __future__ import annotations

import codecs
import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path

# 读取上限：编辑类工具面对的都是源码 / 配置，超过此大小按「不适合原地编辑」处理
MAX_EDIT_BYTES = 8 * 1024 * 1024


class TextFileError(Exception):
    """文件不适合按文本编辑（二进制、编码无法识别、过大）。message 直接回给模型。"""


@dataclass
class TextFile:
    """解码后的文本文件：text 已统一为 \\n 换行，其余字段用于按原样写回。"""

    text: str
    encoding: str = "utf-8"   # "utf-8" | "gb18030"
    bom: bool = False         # 原文件是否带 UTF-8 BOM
    newline: str = "\n"       # 原文件主换行风格："\r\n" | "\n"


def _detect_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return os.linesep  # 单行文件：沿用平台默认（与旧行为一致）
    return "\r\n" if crlf > lf else "\n"


def decode_bytes(raw: bytes, *, strict: bool = True) -> TextFile:
    """字节 → TextFile。strict=True 时二进制 / 无法识别的编码抛 TextFileError；
    strict=False（只读展示用）时按 UTF-8 尽力解码，不抛异常。"""
    if b"\x00" in raw[:8192]:
        if strict:
            raise TextFileError("这是二进制文件，不能按文本编辑")
        return TextFile(raw.decode("utf-8", errors="replace").replace("\r\n", "\n"))
    bom = raw.startswith(codecs.BOM_UTF8)
    body = raw[len(codecs.BOM_UTF8):] if bom else raw
    try:
        text = body.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        try:
            # GB18030 是 GBK 的超集，可无损往返：未改动的部分写回后字节完全一致
            text = body.decode("gb18030")
            encoding = "gb18030"
        except UnicodeDecodeError:
            if strict:
                raise TextFileError(
                    "无法识别文件编码（既不是 UTF-8 也不是 GBK/GB18030），为避免损坏文件未做修改"
                ) from None
            text = body.decode("utf-8", errors="replace")
            encoding = "utf-8"
    return TextFile(
        text=text.replace("\r\n", "\n"),
        encoding=encoding,
        bom=bom,
        newline=_detect_newline(text),
    )


def read_text_file(path: Path, *, strict: bool = True) -> TextFile:
    try:
        size = path.stat().st_size
    except OSError as e:
        raise TextFileError(f"读取失败: {e}") from None
    if strict and size > MAX_EDIT_BYTES:
        raise TextFileError(f"文件过大（{size // 1024} KB），不适合原地编辑")
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise TextFileError(f"读取失败: {e}") from None
    return decode_bytes(raw, strict=strict)


def encode_text(tf: TextFile, text: str) -> bytes:
    """按原文件的编码、BOM 与换行风格编码新内容。"""
    norm = text.replace("\r\n", "\n")
    if tf.newline == "\r\n":
        norm = norm.replace("\n", "\r\n")
    data = norm.encode(tf.encoding)
    return (codecs.BOM_UTF8 + data) if tf.bom else data


def write_text_file(path: Path, tf: TextFile, text: str) -> None:
    path.write_bytes(encode_text(tf, text))


# ───────────────────────── 匹配与替换 ─────────────────────────

# fs_read 输出形如「  12: code」（空行为「  13: 」，模型常把末尾空格丢掉）；兼容 cat -n 的 Tab、「12→」与「12 | code」
_LINE_NO = re.compile(r"^\s*(\d+)(?:: |:$|\t|→|\s*\|\s*)")
# 模型双重转义产生的字面转义序列（只在没有真实控制字符时尝试还原）
_LITERAL_ESCAPE = re.compile(r"\\(n|t|r|\"|')")


@dataclass
class ReplaceResult:
    ok: bool
    text: str = ""
    count: int = 0
    # exact / line_numbers / trailing_ws / indent —— 非 exact 时在工具输出里说明，便于模型下次写准
    strategy: str = "exact"
    error: str = ""


STRATEGY_NOTES = {
    "line_numbers": "已自动去掉 old_text 中的行号前缀",
    "trailing_ws": "已忽略行尾空白差异",
    "indent": "已忽略缩进差异，并按文件实际缩进调整了 new_text",
    "whitespace": "已忽略换行与空白位置差异（old_text 与原文的换行方式不同）",
    "literal_escapes": "已还原 old_text 中的字面 \\n / \\t 转义",
}


def _split_lines(text: str) -> list[str]:
    """按 \\n 切行；末尾换行不产生多余的空行（与「整行替换」语义一致）。"""
    lines = text.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def _strip_line_numbers(text: str) -> str | None:
    """old_text 的每个非空行都带 fs_read 行号前缀且行号递增时，去掉前缀；否则返回 None。"""
    lines = text.split("\n")
    out: list[str] = []
    last = -1
    seen = 0
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        m = _LINE_NO.match(line)
        if not m:
            return None
        n = int(m.group(1))
        if n <= last:
            return None
        last = n
        seen += 1
        out.append(line[m.end():])
    return "\n".join(out) if seen else None


def _strip_line_numbers_loose(text: str) -> str:
    """new_text 的行号前缀：已确认 old_text 带行号时，new_text 中**多数**行带前缀才去掉（逐行、只去匹配的行）。
    new_text 里模型新增的行常不带行号，不能要求「全部带」。"""
    lines = text.split("\n")
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return text
    hits = sum(1 for l in non_empty if _LINE_NO.match(l))
    if hits * 2 < len(non_empty):
        return text
    return "\n".join((l[_LINE_NO.match(l).end():] if _LINE_NO.match(l) else l) for l in lines)


def _strip_line_numbers_partial(text: str) -> str | None:
    """部分行带行号的宽松处理（模型复制一段时经常只保留首行行号）。

    判定条件（同时满足才生效，避免误伤内容本身以「数字:」开头的代码）：
    - 至少两行非空内容；
    - 首个非空行带行号；
    - 带行号的行号严格递增。
    只去掉**带行号的行**的前缀，未带行号的行原样保留。
    """
    lines = text.split("\n")
    if len([l for l in lines if l.strip()]) < 2:
        return None
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is None or not _LINE_NO.match(lines[first]):
        return None
    out: list[str] = []
    last = -1
    seen = 0
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        m = _LINE_NO.match(line)
        if not m:
            out.append(line)
            continue
        n = int(m.group(1))
        if n <= last:
            return None
        last = n
        seen += 1
        out.append(line[m.end():])
    if seen < 1:
        return None
    return "\n".join(out)


def _decode_literal_escapes(text: str) -> str:
    """把模型双重转义产生的字面 ``\\n`` / ``\\t`` / ``\\r`` 还原成真实控制字符。

    仅在文本**不含**任何真实换行/制表符时尝试（否则可能是代码里的字符串字面量，
    不能碰）；调用方只在精确匹配失败后才使用，且仍要求唯一命中。
    """
    if not text or "\n" in text or "\t" in text or "\r" in text:
        return text
    if "\\" not in text:
        return text

    def _repl(m: re.Match[str]) -> str:
        return {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'"}[m.group(1)]

    return _LITERAL_ESCAPE.sub(_repl, text)


def _normalize_ws_with_map(text: str) -> tuple[str, list[int]]:
    """把连续空白压缩成单个空格，并记录归一化后每个字符在原文中的下标。

    返回 (归一化文本, 下标表)。下标表用于把归一化匹配区间映射回原文区间。
    """
    out: list[str] = []
    pos: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            out.append(" ")
            pos.append(i)
            i = j
        else:
            out.append(text[i])
            pos.append(i)
            i += 1
    return "".join(out), pos


def _whitespace_hits(content: str, old_text: str) -> list[int]:
    """跨行空白归一化后的命中位置（归一化文本下标）。"""
    norm_content, _ = _normalize_ws_with_map(content)
    norm_old, _ = _normalize_ws_with_map(old_text)
    target = norm_old.strip()
    # 太短的片段不做跨行宽松匹配，避免把普通单词/符号误配到别处
    if len(target.replace(" ", "")) < 6:
        return []
    hits: list[int] = []
    start = 0
    while True:
        i = norm_content.find(target, start)
        if i < 0:
            break
        hits.append(i)
        start = i + 1
    return hits


def _lead(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _reindent(new_lines: list[str], old_ind: str, file_ind: str) -> list[str]:
    """把 new_text 的缩进从 old_text 的写法换算成文件实际写法（只处理以 old_ind 开头的行）。"""
    if old_ind == file_ind:
        return new_lines
    # 缩进单位换算：空格 ↔ Tab（能整除时按比例换算，避免出现 Tab 与空格混排）
    sp_per_tab = 0
    to_tabs = False
    if old_ind and file_ind:
        if set(old_ind) == {" "} and set(file_ind) == {"\t"} and len(old_ind) % len(file_ind) == 0:
            sp_per_tab, to_tabs = len(old_ind) // len(file_ind), True
        elif set(old_ind) == {"\t"} and set(file_ind) == {" "} and len(file_ind) % len(old_ind) == 0:
            sp_per_tab = len(file_ind) // len(old_ind)
    out: list[str] = []
    for line in new_lines:
        if not line.strip():
            out.append(line)
            continue
        lead = _lead(line)
        body = line[len(lead):]
        if sp_per_tab and to_tabs and set(lead) <= {" "} and len(lead) % sp_per_tab == 0:
            out.append("\t" * (len(lead) // sp_per_tab) + body)
        elif sp_per_tab and not to_tabs and set(lead) <= {"\t"}:
            out.append(" " * (len(lead) * sp_per_tab) + body)
        elif lead.startswith(old_ind):
            out.append(file_ind + lead[len(old_ind):] + body)
        else:
            out.append(line)
    return out


def _windows(keys: list[str], target: list[str]) -> list[int]:
    n = len(target)
    if n == 0 or n > len(keys):
        return []
    first = target[0]
    return [i for i in range(len(keys) - n + 1) if keys[i] == first and keys[i:i + n] == target]


def _line_no_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _format_snippet(file_lines: list[str], start: int, end: int) -> str:
    start = max(0, start)
    end = min(len(file_lines), end)
    width = len(str(end))
    return "\n".join(f"{i + 1:>{width}}: {file_lines[i]}" for i in range(start, end))


def _nearest_hint(file_lines: list[str], old_lines: list[str]) -> str:
    """未命中时：找文件里与 old_text 首个非空行最相近的位置，给出原文片段与差异提示。"""
    probe_idx = next((k for k, l in enumerate(old_lines) if l.strip()), None)
    if probe_idx is None:
        return ""
    probe = old_lines[probe_idx].strip()
    best_i, best_r = -1, 0.0
    for i, line in enumerate(file_lines):
        s = line.strip()
        if not s:
            continue
        sm = difflib.SequenceMatcher(None, probe, s, autojunk=False)
        if sm.real_quick_ratio() <= best_r or sm.quick_ratio() <= best_r:
            continue
        r = sm.ratio()
        if r > best_r:
            best_i, best_r = i, r
    if best_i < 0 or best_r < 0.6:
        return "文件中没有找到相近的内容，请先用 fs_read 重新读取文件，确认要修改的位置与原文。"
    start = best_i - probe_idx
    span = len(old_lines)
    snippet = _format_snippet(file_lines, start - 1, min(start + span + 1, start + 30))
    notes: list[str] = []
    region = file_lines[max(0, start):max(0, start) + span]
    old_uses_tab = any(_lead(l).startswith("\t") for l in old_lines if l.strip())
    file_uses_tab = any(_lead(l).startswith("\t") for l in region if l.strip())
    if file_uses_tab and not old_uses_tab:
        notes.append("文件此处用 Tab 缩进，old_text 用的是空格")
    elif old_uses_tab and not file_uses_tab:
        notes.append("文件此处用空格缩进，old_text 用的是 Tab")
    if any(l != l.rstrip() for l in region):
        notes.append("原文部分行尾带有空白")
    tip = ("；".join(notes) + "。") if notes else ""
    return (
        f"文件中最相近的原文在第 {max(1, start + 1)} 行附近（以下为带行号的原文，行号不属于内容）：\n"
        f"{snippet}\n{tip}请按原文逐字（含缩进与空白）复制 old_text 后重试。"
    )


def _whitespace_replace(content: str, old_text: str, new_text: str,
                        replace_all: bool) -> ReplaceResult | None:
    """跨行空白归一化替换：允许 old_text 与文件的换行位置不同（模型常把两行并成一行）。

    返回 None 表示未命中，交由调用方继续走失败提示。
    """
    hits = _whitespace_hits(content, old_text)
    if not hits:
        return None
    _, pos = _normalize_ws_with_map(content)
    target_len = len(_normalize_ws_with_map(old_text)[0].strip())
    if len(hits) > 1 and not replace_all:
        lines = ", ".join(str(_line_no_of(content, pos[i])) for i in hits[:10])
        return ReplaceResult(
            False,
            error=(
                f"old_text（忽略换行与空白差异后）匹配 {len(hits)} 处（第 {lines} 行），"
                "请带上更多上下文使其唯一，或设置 replace_all=true"
            ),
        )
    old_lines = _split_lines(old_text)
    old_first = next((l for l in old_lines if l.strip()), "")
    old_ind = _lead(old_first)
    out = content
    applied = 0
    last_start = len(out) + 1
    # 归一化命中位置 → 原文字符区间；从后往前替换，前面下标不受影响
    for i in sorted(hits, reverse=True):
        raw_start = pos[i]
        raw_end = pos[i + target_len - 1] + 1
        if raw_end > last_start:
            continue
        line_start = out.rfind("\n", 0, raw_start) + 1
        file_ind = out[line_start:raw_start]
        if file_ind.strip():
            file_ind = ""  # 命中位置在行中间，行首到该位置不是纯缩进
        new_lines = _split_lines(new_text) if new_text else []
        if file_ind and old_ind:
            # 替换区间从行首缩进之后开始：文件原有缩进已保留，new_text 首行
            # 若再带一份 old_ind 会叠成双倍缩进，先去掉再按文件缩进重排。
            if new_lines and _lead(new_lines[0]) == old_ind:
                new_lines[0] = new_lines[0][len(old_ind):]
            new_lines = _reindent(new_lines, old_ind, file_ind)
        out = out[:raw_start] + "\n".join(new_lines) + out[raw_end:]
        applied += 1
        last_start = raw_start
    return ReplaceResult(True, out, applied, "whitespace")


def apply_replacement(content: str, old_text: str, new_text: str, replace_all: bool = False) -> ReplaceResult:
    """在 content（\\n 换行）里把 old_text 替换为 new_text。

    精确匹配 → 去行号前缀 → 忽略行尾空白 → 忽略缩进/行内空白差异 → 跨行空白归一化；
    除 replace_all 外必须唯一命中。
    """
    old_text = (old_text or "").replace("\r\n", "\n")
    new_text = (new_text or "").replace("\r\n", "\n")
    if not old_text:
        return ReplaceResult(False, error="old_text 不能为空（新建或整体覆盖文件请用 fs_write）")
    if old_text == new_text:
        return ReplaceResult(False, error="old_text 与 new_text 相同，文件无需修改")

    # 1) 精确匹配
    count = content.count(old_text)
    if count == 1 or (count > 1 and replace_all):
        return ReplaceResult(True, content.replace(old_text, new_text, -1 if replace_all else 1), count)
    if count > 1:
        idxs: list[int] = []
        pos = content.find(old_text)
        while pos != -1 and len(idxs) < 10:
            idxs.append(_line_no_of(content, pos))
            pos = content.find(old_text, pos + 1)
        return ReplaceResult(
            False,
            error=(
                f"old_text 匹配 {count} 处（第 {', '.join(map(str, idxs))} 行"
                f"{' 等' if count > len(idxs) else ''}），请带上更多上下文使其唯一，或设置 replace_all=true"
            ),
        )

    # 2) 字面转义（模型双重转义）：只在精确匹配失败后尝试，还原后整体重走匹配
    unescaped = _decode_literal_escapes(old_text)
    if unescaped != old_text:
        res = apply_replacement(content, unescaped, _decode_literal_escapes(new_text), replace_all)
        if res.ok and res.strategy == "exact":
            res.strategy = "literal_escapes"
        return res

    # 3) old_text 带了 fs_read 的行号前缀：去掉后整体重来（失败时的提示也按去掉行号后的原文给出）
    stripped = _strip_line_numbers(old_text)
    if stripped and stripped != old_text:
        res = apply_replacement(content, stripped, _strip_line_numbers_loose(new_text), replace_all)
        if res.ok and res.strategy == "exact":
            res.strategy = "line_numbers"
        return res

    # 3b) 只有部分行带行号（模型复制片段时常只保留首行行号）
    partial = _strip_line_numbers_partial(old_text)
    if partial and partial != old_text:
        res = apply_replacement(content, partial, _strip_line_numbers_loose(new_text), replace_all)
        if res.ok and res.strategy == "exact":
            res.strategy = "line_numbers"
        return res

    # 4) / 5) 整行宽松匹配
    file_lines = content.split("\n")
    old_lines = _split_lines(old_text)
    new_lines = _split_lines(new_text) if new_text else []
    tiers = (
        ("trailing_ws", lambda s: s.rstrip()),
        ("indent", lambda s: " ".join(s.split())),
    )
    for strategy, key in tiers:
        keys = [key(l) for l in file_lines]
        target = [key(l) for l in old_lines]
        if not any(target):
            continue
        hits = _windows(keys, target)
        if not hits:
            continue
        if len(hits) > 1 and not replace_all:
            lines = ", ".join(str(h + 1) for h in hits[:10])
            return ReplaceResult(
                False,
                error=(
                    f"old_text（忽略空白差异后）匹配 {len(hits)} 处（第 {lines} 行），"
                    "请带上更多上下文使其唯一，或设置 replace_all=true"
                ),
            )
        n = len(old_lines)
        out = list(file_lines)
        # 从后往前替换，前面的下标不受影响；重叠窗口只取不重叠的部分
        applied = 0
        last_start = len(out) + 1
        for i in sorted(hits, reverse=True):
            if i + n > last_start:
                continue
            repl = new_lines
            if strategy == "indent":
                k = next((j for j, l in enumerate(old_lines) if l.strip()), 0)
                repl = _reindent(new_lines, _lead(old_lines[k]), _lead(file_lines[i + k]))
            out[i:i + n] = repl
            applied += 1
            last_start = i
        return ReplaceResult(True, "\n".join(out), applied, strategy)

    # 6) 跨行空白归一化：允许换行位置不同（模型把两行并成一行 / 把一行拆成多行）
    ws_res = _whitespace_replace(content, old_text, new_text, replace_all)
    if ws_res is not None:
        return ws_res

    return ReplaceResult(False, error="old_text 在文件中未匹配。" + _nearest_hint(file_lines, old_lines))


def pick_arg(args: dict, *names: str):
    """参数名容错：模型偶尔沿用其它工具的命名（file_path / old_string / oldString …）。"""
    for n in names:
        if n in args and args[n] is not None:
            return args[n]
    return None

"""工作区漂移检测（设计 §6.6）：暂停时记录写入范围内文件的指纹，续跑前比对，外部改动需用户确认。

只覆盖 execute 节点声明的写入范围（写入契约的边界），不扫描整个工作区，避免大仓库的开销。
文件超过哈希上限时只比较大小与修改时间；字面路径（无通配符）即使不存在也会记录，
以便发现“暂停期间被新建”的文件。
"""

from __future__ import annotations

import difflib
import glob
import hashlib
from pathlib import Path
from typing import Any

MAX_FILES = 2000
HASH_LIMIT_BYTES = 256 * 1024
# 暂停时保存内容的文件上限（只为“查看差异”服务，超过则只记录指纹）
TEXT_LIMIT_BYTES = 32 * 1024
DIFF_MAX_LINES = 400
# 一次暂停保存的文本内容总量上限，超出的文件只记录指纹
CONTENTS_BUDGET_BYTES = 512 * 1024
RECORD_VERSION = 2
ABSENT = "absent"
UNREADABLE = "unreadable"
_GLOB_CHARS = frozenset("*?[")


def _digest(path: Path) -> str:
    try:
        st = path.stat()
        content_hash = "-"
        if st.st_size <= HASH_LIMIT_BYTES:
            content_hash = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
        return f"{st.st_size}:{st.st_mtime_ns}:{content_hash}"
    except OSError:
        return UNREADABLE


def _relative(root: Path, path: Path) -> str | None:
    try:
        # 先解析 ..，否则词法比较会把 root/../x 误判为工作区内
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        # 写入范围指向工作区之外（或绝对路径）时不纳入指纹
        return None


def snapshot_fingerprint(workspace: str, patterns: list[str]) -> dict[str, str]:
    """写入范围内文件的指纹：相对路径 → 指纹串。"""
    root = Path(workspace)
    out: dict[str, str] = {}
    for pattern in patterns:
        normalized = pattern.replace("\\", "/").strip("/")
        if not normalized:
            continue
        if not (set(normalized) & _GLOB_CHARS):
            path = root / normalized
            rel = _relative(root, path)
            if rel is not None:
                out[rel] = _digest(path) if path.is_file() else ABSENT
            continue
        for hit in sorted(glob.glob(str(root / normalized), recursive=True)):
            path = Path(hit)
            if not path.is_file():
                continue
            rel = _relative(root, path)
            if rel is None or rel in out:
                continue
            out[rel] = _digest(path)
            if len(out) >= MAX_FILES:
                return out
    return out


def diff_fingerprints(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """两次指纹的差异（新增、删除或内容变化），按路径排序。"""
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k, ABSENT) != after.get(k, ABSENT))


def snapshot_contents(workspace: str, rels: list[str]) -> dict[str, str]:
    """写入范围内小型 UTF-8 文本文件的内容（相对路径 → 文本），用于展示暂停后的差异。"""
    root = Path(workspace)
    out: dict[str, str] = {}
    budget = CONTENTS_BUDGET_BYTES
    for rel in rels:
        text = read_text_for_diff(root / rel)
        if text is None:
            continue
        size = len(text.encode("utf-8"))
        if size > budget:
            break
        budget -= size
        out[rel] = text
    return out


def read_text_for_diff(path: Path) -> str | None:
    """文件的 UTF-8 文本；不存在、超过大小上限或不是文本时返回 None。"""
    try:
        if path.is_file() and path.stat().st_size <= TEXT_LIMIT_BYTES:
            return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return None


def fingerprint_record(
    files: dict[str, str], contents: dict[str, str], head: str | None
) -> dict[str, Any]:
    """暂停时写入数据库的工作区记录：指纹、文本内容与 Git 的 HEAD。"""
    return {"v": RECORD_VERSION, "files": files, "contents": contents, "head": head}


def files_of(record: dict[str, Any]) -> dict[str, str]:
    """兼容旧记录：早期版本直接存放“路径 → 指纹”的映射。"""
    if record.get("v") == RECORD_VERSION:
        return dict(record.get("files") or {})
    return dict(record)


def contents_of(record: dict[str, Any]) -> dict[str, str]:
    if record.get("v") == RECORD_VERSION:
        return dict(record.get("contents") or {})
    return {}


def head_of_record(record: dict[str, Any]) -> str | None:
    return record.get("head") if record.get("v") == RECORD_VERSION else None


def unified_diff(path: str, before: str, after: str) -> str:
    """两个版本之间的统一差异；超过上限时截断并注明省略的行数。"""
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"暂停时 {path}",
            tofile=f"当前 {path}",
            n=3,
        )
    )
    if len(lines) <= DIFF_MAX_LINES:
        return "".join(lines)
    omitted = len(lines) - DIFF_MAX_LINES
    return "".join(lines[:DIFF_MAX_LINES]) + f"… 其余 {omitted} 行已省略\n"

"""路径类失败的就近提示（plan-20261020-pts）。

真实失败数据里，「文件不存在 / 目录不存在 / 路径不存在」占读写类失败的大头，且同一个错误路径常被
连续重试 3～8 次：例如迁移脚本其实在 clinic-common 模块下，模型却反复去 clinic-basic 下找。
旧报错只有一句「文件不存在: x」，模型拿不到任何纠错线索。这里在失败时补两条线索：
- 同名文件 / 目录在工作区里的实际位置（有时间与数量上限，只在失败时才搜）；
- 最近一级存在的上级目录里有什么。

另：上下文折叠的落盘文件（.compact-cache/<hash>.txt）此前写在全局默认工作区，
旧会话里的占位提示仍指向那里，读不到时回落到全局目录再找一次。
"""
from __future__ import annotations

import os
import re
import time
from collections import deque
from pathlib import Path

# 搜索同名文件时跳过的目录：依赖、构建产物、版本库与本软件自己的缓存
_SKIP_DIRS = frozenset({
    ".git", ".svn", "node_modules", "target", "dist", "build", "out", ".venv", "venv", "__pycache__",
    ".idea", ".vscode", ".gradle", ".next", ".nuxt", "coverage", ".chatcoder", ".compact-cache",
})
_WALK_BUDGET_S = 0.6
_WALK_MAX_ENTRIES = 60_000
_LIST_LIMIT = 14
_COMPACT_RE = re.compile(r"(?:^|[\\/])\.compact-cache[\\/]([0-9a-f]{8})\.txt$", re.I)


def compact_cache_fallback(workspace_root: str, raw: str) -> Path | None:
    """`.compact-cache/<hash>.txt` 在会话工作区找不到时，到全局默认工作区再找一次。"""
    m = _COMPACT_RE.search((raw or "").strip())
    if not m:
        return None
    name = f"{m.group(1).lower()}.txt"
    from app.core.config import settings

    for base in (workspace_root, getattr(settings, "workspace_root", "")):
        if not base:
            continue
        p = Path(base) / ".compact-cache" / name
        if p.is_file():
            return p
    return None


def _abs(workspace_root: str, raw: str) -> Path:
    p = Path(raw.strip().strip('"').strip("'"))
    return p if p.is_absolute() else Path(workspace_root) / p


def _display(workspace_root: str, p: Path) -> str:
    try:
        return p.resolve().relative_to(Path(workspace_root).resolve()).as_posix() or "."
    except (ValueError, OSError):
        return str(p)


def _nearest_existing_dir(workspace_root: str, raw: str) -> Path | None:
    cur = _abs(workspace_root, raw)
    for _ in range(64):
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
        try:
            if cur.is_dir():
                return cur
        except OSError:
            return None
    return None


def list_dir_brief(d: Path, limit: int = _LIST_LIMIT) -> str:
    """目录内容一行摘要：目录在前（带 /），文件在后，超出数量只给总数。"""
    try:
        entries = [e for e in os.scandir(d) if e.name not in _SKIP_DIRS]
    except OSError:
        return ""
    entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    names = [e.name + ("/" if e.is_dir(follow_symlinks=False) else "") for e in entries]
    if not names:
        return "（空目录）"
    text = "、".join(names[:limit])
    return text + (f" 等共 {len(names)} 项" if len(names) > limit else "")


def find_by_name(workspace_root: str, name: str, want_dir: bool | None = None, limit: int = 5) -> list[str]:
    """在工作区里按名字（不区分大小写）找文件 / 目录，浅层优先；超出时间或数量上限即停。"""
    root = Path(workspace_root)
    target = name.lower()
    found: list[str] = []
    t0 = time.monotonic()
    seen = 0
    queue: deque[Path] = deque([root])
    while queue:
        d = queue.popleft()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen > _WALK_MAX_ENTRIES or time.monotonic() - t0 > _WALK_BUDGET_S:
                        return found
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if e.name.lower() == target and (want_dir is None or want_dir == is_dir):
                        found.append(_display(workspace_root, Path(e.path)) + ("/" if is_dir else ""))
                        if len(found) >= limit:
                            return found
                    if is_dir and e.name not in _SKIP_DIRS:
                        queue.append(Path(e.path))
        except OSError:
            continue
    return found


def missing_path_hint(workspace_root: str, raw: str, *, want_dir: bool | None = None) -> str:
    """路径不存在时的就近提示（以换行开头，可直接拼在错误后面）；没有线索时返回空串。"""
    raw = (raw or "").strip()
    if not raw or not workspace_root:
        return ""
    lines: list[str] = []
    name = Path(raw.replace("\\", "/").rstrip("/")).name
    if name and name not in (".", ".."):
        hits = [h for h in find_by_name(workspace_root, name, want_dir) if h.rstrip("/") != raw.replace("\\", "/").rstrip("/")]
        if hits:
            kind = "目录" if want_dir else "文件" if want_dir is False else "路径"
            lines.append(f"同名{kind}在：{'、'.join(hits)}")
    anc = _nearest_existing_dir(workspace_root, raw)
    if anc is not None:
        brief = list_dir_brief(anc)
        if brief:
            lines.append(f"最近存在的上级目录 {_display(workspace_root, anc)}/ 下有：{brief}")
    return ("\n" + "\n".join(lines)) if lines else ""

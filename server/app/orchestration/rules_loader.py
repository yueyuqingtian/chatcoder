"""项目规则文档加载（AGENTS.md/CLAUDE.md 多文件，codex 规范 D7）。

优先级：project.rules_docs(手动配置) > 自动扫描(根+一级子目录 AGENTS.md/.cursorrules/CLAUDE.md)。
总量上限 32KiB（对齐 codex project_doc_max_bytes）。
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_RULE_NAMES = ("AGENTS.md", ".cursorrules", "CLAUDE.md", "CLAUDE.md", "AGENTS.md")
_MAX_TOTAL_BYTES = 32 * 1024
_MAX_SINGLE_BYTES = 16 * 1024


async def scan_rules_docs(root: str) -> list[str]:
    """扫描目录（根 + 一级子目录）下的规范文档，返回相对路径列表。"""
    base = Path(root)
    if not base.is_dir():
        return []
    dirs = [base]
    try:
        dirs += [d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")][:8]
    except OSError:
        pass
    found: list[str] = []
    seen: set[str] = set()
    for d in dirs:
        for name in _RULE_NAMES:
            p = d / name
            if p.is_file():
                rel = str(p.relative_to(base)).replace("\\", "/")
                if rel not in seen:
                    seen.add(rel)
                    found.append(rel)
    return found


async def load_session_rules(workspace: str, rules_docs: list[str] | None = None) -> str:
    """加载规范文档内容（拼接，各带文件名头）。"""
    base = Path(workspace)
    candidates: list[Path] = []
    for rel in (rules_docs or []):
        p = Path(rel)
        candidates.append(p if p.is_absolute() else base / p)

    # 自动探测补全（未手动配置的文件）
    auto = await scan_rules_docs(workspace)
    for rel in auto:
        p = base / rel
        if p not in candidates:
            candidates.append(p)

    parts: list[str] = []
    seen: set[str] = set()
    total = 0
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if not text:
                    continue
                chunk = f"({p.name})\n{text[:_MAX_SINGLE_BYTES]}"
                if total + len(chunk) > _MAX_TOTAL_BYTES:
                    break
                parts.append(chunk)
                total += len(chunk)
        except OSError:
            continue

    if not parts:
        return ""
    return "\n\n".join(parts)


async def project_structure_brief(workspace: str, max_lines: int = 30) -> str:
    """项目结构摘要：一级目录 + 根文件 + git 仓库。"""
    base = Path(workspace)
    if not base.is_dir():
        return ""
    lines: list[str] = []
    try:
        dirs = sorted(d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith("."))
        files = sorted(f.name for f in base.iterdir() if f.is_file() and not f.name.startswith("."))
        if dirs:
            lines.append("Top-level directories: " + ", ".join(dirs[:20]))
        if files:
            lines.append("Root files: " + ", ".join(files[:15]))
        repos = [d.name for d in base.iterdir() if d.is_dir() and (d / ".git").exists()]
        if repos:
            lines.append("Git repos: " + ", ".join(repos))
    except OSError:
        pass
    return "\n".join(lines[:max_lines])

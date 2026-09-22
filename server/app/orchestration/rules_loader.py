"""项目规则文档加载（AGENTS.md/CLAUDE.md 多文件，codex 规范 D7）。

优先级：project.rules_docs(手动配置) > 自动扫描(根+一级子目录 AGENTS.md/.cursorrules/CLAUDE.md)。
总量上限 32KiB（对齐 codex project_doc_max_bytes）。

plan-282-1441（#6）：补齐各 CLI 的规则文档名——不同工具用不同约定名，
此前只认 AGENTS.md / .cursorrules / CLAUDE.md，导致用户已有的
.codebuddy/AGENTS.md、.trae/rules、.qoder/AGENTS.md、.cursor/rules 等一律读不到。
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 根 / 一级子目录下的规则文件名（各 CLI 约定名）
_RULE_NAMES = (
    "AGENTS.md",          # codex / 通用（含 codebuddy、qoder 的 AGENTS.md 约定）
    "CLAUDE.md",          # Claude Code
    ".cursorrules",       # Cursor（旧式单文件）
    "CODEBUDDY.md",       # CodeBuddy
    "QODER.md",           # Qoder
    "rules.md",           # Trae
    "GEMINI.md",          # Gemini CLI
    ".windsurfrules",     # Windsurf
    "CONTRIBUTING.md",    # 通用工程约定（弱规则，放最后）
)

# 以「目录」形式存放规则的工具：扫目录下的 *.md
_RULE_DIRS = (
    ".cursor/rules",
    ".trae/rules",
    ".codebuddy/rules",
    ".qoder/rules",
    ".github/instructions",
)

_MAX_TOTAL_BYTES = 32 * 1024
_MAX_SINGLE_BYTES = 16 * 1024

# v6: 规则文档 → 来源软件映射（用于按来源启用/停用）
_RULE_SOURCE_MAP: list[tuple[str, str]] = [
    # (规则文件/目录相对路径, 来源)
    ("CLAUDE.md", "claude"),
    (".claude/CLAUDE.md", "claude"),
    ("AGENTS.md", "codex"),
    (".codex/AGENTS.md", "codex"),
    ("CODEBUDDY.md", "codebuddy"),
    (".codebuddy/AGENTS.md", "codebuddy"),
    (".codebuddy/rules", "codebuddy"),
    (".trae/rules", "trae"),
    ("rules.md", "trae"),
    ("QODER.md", "qoder"),
    (".qoder/AGENTS.md", "qoder"),
    (".qoder/rules", "qoder"),
    (".cursorrules", "cursor"),
    (".cursor/rules", "cursor"),
    ("GEMINI.md", "gemini"),
    (".windsurfrules", "windsurf"),
    (".github/instructions", "github"),
]


def _get_enabled_rule_sources() -> set[str]:
    """读取用户配置中启用的规则来源；未配置时全部启用。"""
    try:
        import json as _json
        from pathlib import Path as _Path
        import os as _os
        cfg_path = _Path(
            _os.environ.get("CHATCODER_USER_CONFIG", str(_Path.home() / ".chatcoder" / "config.json"))
        )
        if cfg_path.exists():
            data = _json.loads(cfg_path.read_text(encoding="utf-8"))
            enabled = data.get("ai_rules_enabled")
            if isinstance(enabled, list) and enabled:
                return set(str(x) for x in enabled)
    except Exception:
        pass
    return {s for _, s in _RULE_SOURCE_MAP}


def _source_of(rel: str) -> str | None:
    """判断相对路径所属的规则来源。"""
    for path, source in _RULE_SOURCE_MAP:
        if rel == path or rel.startswith(path.rstrip("/") + "/"):
            return source
    return None


async def scan_rules_docs(root: str) -> list[str]:
    """扫描目录（根 + 一级子目录）下的规范文档，返回相对路径列表。

    plan-282-1441（#6）：除约定文件名外，还支持「以目录存放规则」的工具
    （.cursor/rules、.trae/rules、.codebuddy/rules 等），取其中 *.md。
    """
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

    def _add(p: Path) -> None:
        try:
            rel = str(p.relative_to(base)).replace("\\", "/")
        except ValueError:
            return
        if rel not in seen:
            seen.add(rel)
            found.append(rel)

    for d in dirs:
        for name in _RULE_NAMES:
            p = d / name
            if p.is_file():
                _add(p)
        # 目录形态的规则（如 .cursor/rules/*.md）
        for rel_dir in _RULE_DIRS:
            rd = d / rel_dir
            if not rd.is_dir():
                continue
            try:
                for f in sorted(rd.iterdir()):
                    if f.is_file() and f.suffix.lower() in (".md", ".mdc"):
                        _add(f)
            except OSError:
                continue
    return found


async def collect_rule_docs(workspace: str, rules_docs: list[str] | None = None) -> list[Path]:
    """收集本轮将要加载的规则文档路径（顺序与 load_session_rules 完全一致）。

    plan-19-82 后续：拆出独立函数，供「本轮实际加载了哪些规则文档」清单复用——
    每轮随用户消息注入的规则锚点需要点名这些文档，模型才知道该去读/遵循哪几个文件。
    """
    base = Path(workspace)
    candidates: list[Path] = []
    for rel in (rules_docs or []):
        p = Path(rel)
        candidates.append(p if p.is_absolute() else base / p)

    # 自动探测补全（仅加载已启用来源的规则文档，v6 按来源启停）
    enabled = _get_enabled_rule_sources()
    auto = await scan_rules_docs(workspace)
    for rel in auto:
        src = _source_of(rel)
        if src is not None and src not in enabled:
            continue
        p = base / rel
        if p not in candidates:
            candidates.append(p)
    return candidates


async def list_rules_doc_names(workspace: str, rules_docs: list[str] | None = None) -> list[str]:
    """已加载规则文档的相对路径清单（空则返回空列表）。

    与 load_session_rules 同源：只列**确实存在且有内容**的文档，避免提示词点名一个
    空文件让模型白读。
    """
    base = Path(workspace)
    names: list[str] = []
    for p in await collect_rule_docs(workspace, rules_docs):
        try:
            if not p.is_file():
                continue
            rel = str(p.relative_to(base)).replace("\\", "/")
        except (OSError, ValueError):
            rel = p.name
        if rel not in names:
            names.append(rel)
    return names


async def load_session_rules(workspace: str, rules_docs: list[str] | None = None) -> str:
    """加载规范文档内容（拼接，各带文件名头）。"""
    base = Path(workspace)
    candidates = await collect_rule_docs(workspace, rules_docs)

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
                # plan-19-82：规则来源标注——让模型与用户能核对规则出自哪个文件/工具。
                try:
                    rel = str(p.relative_to(base)).replace("\\", "/")
                except ValueError:
                    rel = p.name
                src = _source_of(rel)
                label = f"{rel}" + (f" · {src}" if src else "")
                chunk = f"({label})\n{text[:_MAX_SINGLE_BYTES]}"
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

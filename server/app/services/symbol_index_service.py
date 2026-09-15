"""符号索引服务（plan-230-1144 M3）。

改造前问题：项目探索只能靠 fs_list + fs_grep + fs_read 反复扫描，没有函数级
符号索引，AI 无法回答"这个函数在哪个文件、哪几行、签名是什么"。

本模块提供 AST/正则混合的符号索引：

- **Python**：标准库 `ast` 精确解析（函数/类/方法/嵌套、装饰器、行号区间）；
- **其他语言**（TS/TSX/JS/Java/Go/Rust/C#/C/C++/Ruby/PHP 等）：正则签名提取器
  （`func` / `function` / `class` / `interface` / `struct` / `fn` / 箭头函数赋值等）。
  选型说明：tree-sitter 需新增依赖且 PyInstaller 采集 .so/.pyd 有打包风险，
  按方案既定路径降级为正则提取——精度略低但零依赖、可打包、可离线。

存储：workspace 内 `.chatcoder/symbols.db`（SQLite，与 codebase_index.json 并存独立）。
增量：files 表记录 (mtime, size, sha1)，只重解析变化文件；文件删除同步清理符号。

对外主要接口（供 symbol_search / outline 工具与诊断面板使用）：
- `index_workspace(workspace, force=False) -> dict`  建/更新索引，返回统计
- `search_symbols(workspace, query, kind, file_glob, limit) -> list[dict]`
- `outline_file(workspace, file_path) -> list[dict]`
- `index_stats(workspace) -> dict`
"""
from __future__ import annotations

import ast
import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_INDEX_DIR = ".chatcoder"
_DB_NAME = "symbols.db"

# 参与索引的源代码扩展名 → 解析器类型
_PY_EXTS = {".py", ".pyi"}
_GENERIC_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".java", ".kt", ".go", ".rs", ".cs", ".c", ".cpp", ".cc", ".h", ".hpp",
    ".rb", ".php", ".swift", ".scala", ".sh", ".ps1", ".sql",
}
_EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", ".idea", ".vscode",
    ".chatcoder", ".mypy_cache", ".ruff_cache", ".pytest_cache",
}
_MAX_FILE_BYTES = 1_500_000  # 超大文件跳过（避免解析卡顿）
_MAX_FILES = 8000            # 单工作区文件上限（防极端仓库拖垮扫描）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT,
    line_start INTEGER,
    line_end INTEGER,
    signature TEXT,
    doc TEXT,
    parent_id INTEGER,
    hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);

CREATE TABLE IF NOT EXISTS files (
    file_path TEXT PRIMARY KEY,
    mtime REAL,
    size INTEGER,
    sha1 TEXT,
    symbol_count INTEGER,
    updated_at REAL
);
"""


def _db_path(workspace: str | Path) -> Path:
    return Path(workspace) / _INDEX_DIR / _DB_NAME


def _connect(workspace: str | Path) -> sqlite3.Connection | None:
    db = _db_path(workspace)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    try:
        conn = sqlite3.connect(str(db), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        return conn
    except sqlite3.Error:
        logger.warning("[symbols] 打开索引库失败 %s", db, exc_info=True)
        return None


# ── 提取器 ────────────────────────────────────────────────────────────

def _python_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = [a.arg for a in node.args.posonlyargs + node.args.args]
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args[:6])}{'…' if len(args) > 6 else ''})"
    except Exception:
        return f"def {node.name}(…)"


def _first_doc_line(node) -> str:
    try:
        doc = ast.get_docstring(node) or ""
        return doc.strip().splitlines()[0][:120] if doc.strip() else ""
    except Exception:
        return ""


def _extract_python(source: str, rel: str) -> list[dict]:
    """标准库 ast 精确提取（含嵌套与行号区间）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[dict] = []

    def walk(node, parent_qname: str, parent_idx: int | None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                qname = f"{parent_qname}.{child.name}" if parent_qname else child.name
                idx = len(out)
                sig = _python_signature(child) if kind != "class" else f"class {child.name}"
                out.append({
                    "file_path": rel, "kind": kind, "name": child.name,
                    "qualified_name": qname,
                    "line_start": child.lineno, "line_end": getattr(child, "end_lineno", child.lineno),
                    "signature": sig, "doc": _first_doc_line(child),
                    "parent_id": parent_idx,
                })
                walk(child, qname, idx)
            else:
                walk(child, parent_qname, parent_idx)

    walk(tree, "", None)

    # 方法标记：类直接子级的函数改 kind=method（上面的通用遍历不做父类型判断）
    by_id = {i: s for i, s in enumerate(out)}
    for s in out:
        pid = s.get("parent_id")
        if pid is not None and by_id.get(pid, {}).get("kind") == "class" and s["kind"] == "function":
            s["kind"] = "method"
    return out


# 通用语言签名正则：覆盖常见声明形态（捕获组 1/2 = 名字）
_GENERIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M)),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)),
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)", re.M)),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*=", re.M)),
    ("enum", re.compile(r"^\s*(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)", re.M)),          # go
    ("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)", re.M)),                    # rust
    ("struct", re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][\w]*)", re.M)),                                # rust/c
    ("enum", re.compile(r"^\s*(?:pub\s+)?enum\s+([A-Za-z_][\w]*)", re.M)),
    ("class", re.compile(r"^\s*(?:public|private|protected|internal|open|final|abstract|static|\s)*class\s+([A-Za-z_][\w]*)", re.M)),     # java/kotlin/c#
    ("interface", re.compile(r"^\s*(?:public|private|protected|internal|\s)*interface\s+([A-Za-z_][\w]*)", re.M)),
    ("function", re.compile(r"^\s*(?:public|private|protected|internal|static|final|override|virtual|\s)*[\w<>\[\],\s]+\s+([A-Za-z_][\w]*)\s*\([^;]*\)\s*\{", re.M)),  # java/c# 方法（保守）
    ("function", re.compile(r"^\s*func\s+([A-Za-z_][\w]*)", re.M)),
    ("function", re.compile(r"^\s*def\s+([A-Za-z_][\w!?]*)", re.M)),                                          # ruby
    ("function", re.compile(r"^\s*(?:public|private|protected|static|\s)*function\s+([A-Za-z_][\w]*)", re.M)), # php
    ("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_][\w]*)", re.M)),
]


def _extract_generic(source: str, rel: str) -> list[dict]:
    """正则签名提取（非 Python）。行号精确到声明行，end 以缩进/大括号粗估。"""
    lines = source.splitlines()
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for kind, pat in _GENERIC_PATTERNS:
        for m in pat.finditer(source):
            name = (m.group(1) or "").strip()
            if not name or name in ("if", "for", "while", "switch", "catch", "return", "then", "constructor"):
                continue
            line_no = source.count("\n", 0, m.start()) + 1
            key = (name, line_no)
            if key in seen:
                continue
            seen.add(key)
            # 粗估结束行：同一缩进层级的下一个声明行或文件末尾，上限 +80 行
            indent = len(lines[line_no - 1]) - len(lines[line_no - 1].lstrip()) if 0 < line_no <= len(lines) else 0
            end = line_no
            for j in range(line_no, min(len(lines), line_no + 80)):
                raw = lines[j]
                if raw.strip() and (len(raw) - len(raw.lstrip())) <= indent and j > line_no:
                    break
                end = j + 1
            out.append({
                "file_path": rel, "kind": kind, "name": name,
                "qualified_name": name,
                "line_start": line_no, "line_end": end,
                "signature": lines[line_no - 1].strip()[:160] if 0 < line_no <= len(lines) else name,
                "doc": "", "parent_id": None,
            })
    out.sort(key=lambda s: (s["line_start"], s["name"]))
    return out


def _iter_source_files(workspace: Path):
    count = 0
    for p in workspace.rglob("*"):
        # rglob/Path stat 也可能长时间占用 GIL（尤其大型 Windows 工作区），
        # 周期性让出执行权，避免后台索引饿死 HTTP 事件循环。
        if count and count % 64 == 0:
            time.sleep(0)
        if count >= _MAX_FILES:
            logger.info("[symbols] 文件数超过上限 %s，停止扫描", _MAX_FILES)
            return
        try:
            if not p.is_file():
                continue
            if any(part in _EXCLUDE_DIRS for part in p.parts):
                continue
            if p.suffix.lower() not in (_PY_EXTS | _GENERIC_EXTS):
                continue
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        count += 1
        yield p


# ── 索引主流程 ────────────────────────────────────────────────────────

def index_workspace(workspace: str | Path, *, force: bool = False,
                    cancel_file: str | None = None,
                    progress_cb=None) -> dict:
    """建立/增量更新符号索引。

    cancel_file/progress_cb 仅供独立 worker 使用；默认保持旧同步调用契约。
    """
    t0 = time.time()
    ws = Path(workspace)
    if not ws.is_dir():
        return {"error": f"工作区不存在: {workspace}"}
    conn = _connect(ws)
    if conn is None:
        return {"error": "无法打开索引库"}

    stats = {"files_scanned": 0, "files_updated": 0, "symbols": 0, "removed_files": 0}
    try:
        known = {row[0]: row[1] for row in conn.execute("SELECT file_path, sha1 FROM files")}
        current: set[str] = set()

        for f in _iter_source_files(ws):
            rel = f.relative_to(ws).as_posix()
            current.add(rel)
            stats["files_scanned"] += 1
            if cancel_file and Path(cancel_file).exists():
                conn.rollback()
                stats["cancelled"] = True
                stats["progress"] = int(stats["files_scanned"] / max(1, _MAX_FILES) * 100)
                return stats
            if progress_cb and stats["files_scanned"] % 32 == 0:
                try:
                    progress_cb(min(95, stats["files_scanned"] * 100 // max(1, _MAX_FILES)), files_scanned=stats["files_scanned"])
                except Exception:
                    logger.debug("[symbols] progress callback failed", exc_info=True)
            # 大型仓库的 AST/正则解析在工作线程中运行；周期性让出 GIL，避免
            # Windows 单进程服务的其它 HTTP 协程在全量索引期间长时间得不到调度。
            if stats["files_scanned"] % 4 == 0:
                time.sleep(0)
            try:
                st = f.stat()
                raw = f.read_bytes()
            except OSError:
                continue
            if st.st_size > _MAX_FILE_BYTES:
                continue
            sha1 = hashlib.sha1(raw).hexdigest()
            if not force and known.get(rel) == sha1:
                continue  # 未变化：跳过重解析（增量核心）

            source = raw.decode("utf-8", errors="replace")
            if f.suffix.lower() in _PY_EXTS:
                syms = _extract_python(source, rel)
            else:
                syms = _extract_generic(source, rel)

            conn.execute("DELETE FROM symbols WHERE file_path = ?", (rel,))
            conn.executemany(
                "INSERT INTO symbols (file_path, kind, name, qualified_name, line_start,"
                " line_end, signature, doc, parent_id, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(s["file_path"], s["kind"], s["name"], s["qualified_name"],
                  s["line_start"], s["line_end"], s["signature"], s["doc"],
                  s["parent_id"], sha1) for s in syms],
            )
            conn.execute(
                "INSERT OR REPLACE INTO files (file_path, mtime, size, sha1, symbol_count, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (rel, st.st_mtime, st.st_size, sha1, len(syms), time.time()),
            )
            stats["files_updated"] += 1

        # 已删除文件：清理其符号
        for rel in set(known) - current:
            conn.execute("DELETE FROM symbols WHERE file_path = ?", (rel,))
            conn.execute("DELETE FROM files WHERE file_path = ?", (rel,))
            stats["removed_files"] += 1

        conn.commit()
        stats["symbols"] = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    except sqlite3.Error:
        logger.warning("[symbols] 索引写入失败", exc_info=True)
        return {"error": "索引写入失败，详见服务端日志"}
    finally:
        conn.close()

    stats["elapsed_ms"] = int((time.time() - t0) * 1000)
    return stats


def search_symbols(workspace: str | Path, query: str, *,
                   kind: str | None = None, file_glob: str | None = None,
                   limit: int = 20) -> list[dict]:
    """符号检索。排序：精确名 > 前缀 > 包含；同名同类按文件聚合。"""
    conn = _connect(Path(workspace))
    if conn is None:
        return []
    try:
        like = f"%{query}%"
        sql = (
            "SELECT file_path, kind, name, qualified_name, line_start, line_end, signature, doc"
            " FROM symbols WHERE (name LIKE ? OR qualified_name LIKE ?)"
        )
        params: list = [like, like]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if file_glob:
            sql += " AND file_path LIKE ?"
            params.append(file_glob.replace("**", "%").replace("*", "%"))
        sql += " LIMIT 300"
        rows = conn.execute(sql, params).fetchall()

        def rank(r) -> tuple:
            name = r[2]
            if name == query:
                pri = 0
            elif name.lower().startswith(query.lower()):
                pri = 1
            else:
                pri = 2
            return (pri, len(name), r[0])

        rows.sort(key=rank)
        return [
            {"file_path": r[0], "kind": r[1], "name": r[2], "qualified_name": r[3],
             "line_start": r[4], "line_end": r[5], "signature": r[6], "doc": r[7]}
            for r in rows[: max(1, min(limit, 100))]
        ]
    except sqlite3.Error:
        logger.warning("[symbols] 检索失败", exc_info=True)
        return []
    finally:
        conn.close()


def outline_file(workspace: str | Path, file_path: str) -> list[dict]:
    """返回单个文件的符号骨架（树状，按行号）。"""
    conn = _connect(Path(workspace))
    if conn is None:
        return []
    try:
        rel = file_path.replace("\\", "/")
        rows = conn.execute(
            "SELECT id, kind, name, qualified_name, line_start, line_end, signature, doc, parent_id"
            " FROM symbols WHERE file_path = ? OR file_path = ?"
            " ORDER BY line_start",
            (rel, file_path),
        ).fetchall()
        return [
            {"id": r[0], "kind": r[1], "name": r[2], "qualified_name": r[3],
             "line_start": r[4], "line_end": r[5], "signature": r[6], "doc": r[7],
             "parent_id": r[8]}
            for r in rows
        ]
    finally:
        conn.close()


def index_stats(workspace: str | Path) -> dict:
    """索引统计（供诊断面板）。"""
    conn = _connect(Path(workspace))
    if conn is None:
        return {"available": False, "files": 0, "symbols": 0, "last_updated": None}
    try:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        last = conn.execute("SELECT MAX(updated_at) FROM files").fetchone()[0]
        return {"available": True, "files": files, "symbols": symbols, "last_updated": last}
    except sqlite3.Error:
        return {"available": False, "files": 0, "symbols": 0, "last_updated": None}
    finally:
        conn.close()


def invalidate_file(workspace: str | Path, file_path: str) -> None:
    """主动失效单文件（fs_write / editor_apply_diff 写盘后调用）。

    只删 files 行不删符号：下次 index_workspace 看到 sha1 缺失即重解析。
    这样写盘后无需同步重扫（避免阻塞工具返回），由下次检索前增量补齐。
    """
    conn = _connect(Path(workspace))
    if conn is None:
        return
    try:
        rel = str(file_path).replace("\\", "/")
        conn.execute("DELETE FROM files WHERE file_path = ?", (rel,))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()
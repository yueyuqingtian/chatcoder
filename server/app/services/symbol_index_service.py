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
增量：files 表记录 (mtime, size, sha1)，mtime+size 未变即零 IO 跳过，只重解析变化文件；
文件删除同步清理符号（以磁盘 exists() 为准，部分扫描不会误删）。
容量：默认不设文件数上限（大项目数万源文件全量索引）；仅逐文件 1.5MB 体积阈值跳过超大文件。

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
import os
import re
import sqlite3
import stat
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
def _env_int(name: str, default: int) -> int:
    """读取正整数环境变量（打包 worker 不加载 .env，故用环境变量而非 settings）。"""
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# 文件数上限：默认 0 = 不限制。
# 大项目动辄数万源文件，硬上限会让部分文件永远进不了索引（此前 8000/20000
# 上限叠加"删除差集"逻辑，还造成符号被轮转误删）。
# 如遇极端仓库需要兜底，可设环境变量 CHATCODER_SYMBOL_INDEX_MAX_FILES 为正整数。
_MAX_FILES = _env_int("CHATCODER_SYMBOL_INDEX_MAX_FILES", 0)
# 超大单文件跳过（避免解析卡顿）；默认 1.5MB，需要索引生成型大文件时可调大。
_MAX_FILE_BYTES = _env_int("CHATCODER_SYMBOL_INDEX_MAX_FILE_BYTES", 1_500_000)

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


# 通用语言签名正则：覆盖常见声明形态（捕获组 1 = 名字）。
#
# ⚠ 性能约定（务必遵守，否则会造成索引进程卡死）：
# 1) 行内锚定一律用 [ \t] 而非 \s——\s 匹配换行，配合 re.M 会在大文件上退化成
#    O(n²) 级扫描；
# 2) 修饰符组不要写成 (?:kw|\s)*：\s 与后续空白类量词重叠会产生指数级回溯；
# 3) 任何 [^x]* 都必须排除换行 (\) 并加长度上限。反例：旧 java 方法正则里的
#    \([^;]*\) 会跨行吞掉整个文件去找 ';'——SQL 等含 '(' 但无 ';' 收尾的文件
#    直接触发灾难性回溯（实测单文件可卡死 10+ 分钟，worker 停在 21% 不动）。
_GENERIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("function", re.compile(r"^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+([A-Za-z_$][\w$]*)[ \t]*\(", re.M)),
    ("function", re.compile(r"^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*(?::[^=\n]{0,200})?=[ \t]*(?:async[ \t]*)?(?:\([^)\n]{0,400}\)|[A-Za-z_$][\w$]*)[ \t]*=>", re.M)),
    ("class", re.compile(r"^[ \t]*(?:export[ \t]+)?(?:abstract[ \t]+)?class[ \t]+([A-Za-z_$][\w$]*)", re.M)),
    ("interface", re.compile(r"^[ \t]*(?:export[ \t]+)?interface[ \t]+([A-Za-z_$][\w$]*)", re.M)),
    ("type", re.compile(r"^[ \t]*(?:export[ \t]+)?type[ \t]+([A-Za-z_$][\w$]*)[ \t]*=", re.M)),
    ("enum", re.compile(r"^[ \t]*(?:export[ \t]+)?enum[ \t]+([A-Za-z_$][\w$]*)", re.M)),
    ("function", re.compile(r"^[ \t]*(?:export[ \t]+)?func[ \t]+(?:\([^)\n]{0,200}\)[ \t]*)?([A-Za-z_]\w*)", re.M)),          # go
    ("function", re.compile(r"^[ \t]*(?:pub[ \t]+)?(?:async[ \t]+)?fn[ \t]+([A-Za-z_]\w*)", re.M)),                    # rust
    ("struct", re.compile(r"^[ \t]*(?:pub[ \t]+)?struct[ \t]+([A-Za-z_]\w*)", re.M)),                                # rust/c
    ("enum", re.compile(r"^[ \t]*(?:pub[ \t]+)?enum[ \t]+([A-Za-z_]\w*)", re.M)),
    ("class", re.compile(r"^[ \t]*(?:(?:public|private|protected|internal|open|final|abstract|static)[ \t]+)*class[ \t]+([A-Za-z_]\w*)", re.M)),     # java/kotlin/c#
    ("interface", re.compile(r"^[ \t]*(?:(?:public|private|protected|internal)[ \t]+)*interface[ \t]+([A-Za-z_]\w*)", re.M)),
    # java/c# 方法（保守）：返回类型 token 序列 + 名字 + 参数 + '{'；
    # 参数段排除换行/括号并限长，保证线性回溯。
    ("function", re.compile(r"^[ \t]*(?:(?:public|private|protected|internal|static|final|override|virtual)[ \t]+)*[\w<>\[\],]+(?:[ \t]+[\w<>\[\],]+)*[ \t]+([A-Za-z_]\w*)[ \t]*\([^;()\n]{0,300}\)[ \t]*\{", re.M)),
    ("function", re.compile(r"^[ \t]*func[ \t]+([A-Za-z_]\w*)", re.M)),
    ("function", re.compile(r"^[ \t]*def[ \t]+([A-Za-z_]\w*[!?]?)", re.M)),                                          # ruby
    ("function", re.compile(r"^[ \t]*(?:(?:public|private|protected|static)[ \t]+)*function[ \t]+([A-Za-z_]\w*)", re.M)), # php
    ("function", re.compile(r"^[ \t]*(?:pub[ \t]+)?(?:async[ \t]+)?(?:unsafe[ \t]+)?fn[ \t]+([A-Za-z_]\w*)", re.M)),
]


def _extract_generic(source: str, rel: str) -> list[dict]:
    """正则签名提取（非 Python）。行号精确到声明行，end 以缩进/大括号粗估。

    性能注意：正则在整份 source 上全量 finditer。之所以不会卡死，靠的是
    _GENERIC_PATTERNS 的性能约定（[ \\t] 行内锚定、避免空白类重叠、[^x] 排除换行并限长）——
    Python 无法中断正在执行的正则，所以防线必须建立在正则本身的形态上。
    此处再加一道整体预算：命中病态输入时放弃剩余模式，保住整个索引任务。
    """
    lines = source.splitlines()
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    # 单文件总预算：正常文件远低于此值（实测 200KB 源码 <50ms）。
    # 注意：这只是"兜底"。若某条正则本身发生灾难性回溯，其内部无法被打断，
    # 真正的防线是 _GENERIC_PATTERNS 的性能约定（切勿写出重叠空白量词/
    # 跨行的 [^x]*）。本预算能拦截的是"多条正则累计变慢"的场景。
    deadline = time.monotonic() + 10.0
    for kind, pat in _GENERIC_PATTERNS:
        if time.monotonic() > deadline:
            logger.warning("[symbols] %s 正则提取超预算，跳过剩余模式", rel)
            break
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


class _ScanCancelled(Exception):
    """内部信号：遍历阶段检测到取消文件（让 disable 立即打断长遍历）。"""


def _collect_source_files(workspace: Path, cancel_file: str | None = None,
                         on_progress=None) -> list[tuple[Path, os.stat_result]]:
    """os.walk + 目录剪枝收集源码文件（默认不设文件数上限）。

    - 剪枝：排除目录直接从遍历栈移除。此前用 rglob("*") 会走进
      node_modules/.git 再逐文件丢弃，大型前端仓库每轮扫描白走数十万项；
    - 取消：每 128 个遍历项检查一次 cancel_file。此前只在逐文件阶段检查，
      rglob 卡在大目录时 worker 无法退出，最终被 terminate 强杀
      （Windows 退出码 1，被 manager 误报为 error"worker exited with code 1"）；
    - stat 结果一并带回：调用方不必对每个文件重复 stat（数万文件时省一轮系统调用）；
    - 返回列表而非生成器：无上限后需要先知道总量才能给出准确进度（见 index_workspace）。
    """
    exts = _PY_EXTS | _GENERIC_EXTS
    found: list[tuple[Path, os.stat_result]] = []
    walked = 0
    for root, dirs, names in os.walk(workspace, onerror=lambda _e: None):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
        if cancel_file:
            walked += len(dirs) + len(names)
            if walked >= 128:
                walked = 0
                time.sleep(0)  # 让出 GIL，避免长遍历饿死同进程其它协程
                if Path(cancel_file).exists():
                    raise _ScanCancelled()
        for name in names:
            p = Path(root) / name
            if p.suffix.lower() not in exts:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):  # 跳过目录/设备/断链符号链接
                continue
            if st.st_size > _MAX_FILE_BYTES:
                continue
            found.append((p, st))
            if _MAX_FILES and len(found) >= _MAX_FILES:
                logger.info("[symbols] 文件数达到配置上限 %s，停止收集", _MAX_FILES)
                return found
            if on_progress and len(found) % 512 == 0:
                on_progress(len(found))
    return found


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
        # (sha1, mtime, size)：mtime+size 未变时直接跳过，不再读文件内容算 sha1。
        # 此前每轮增量（含自动巡检）都把全部文件完整读一遍，
        # 大仓库仅增量空转就要读上千个文件，是"索引库耗性能"的主要来源。
        known = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute("SELECT file_path, sha1, mtime, size FROM files")
        }
        current: set[str] = set()

        def _progress(value: int, **extra) -> None:
            if not progress_cb:
                return
            try:
                progress_cb(value, **extra)
            except Exception:
                logger.debug("[symbols] progress callback failed", exc_info=True)

        # 两段式：先收集（phase=scanning，无上限时无法先验总量，
        # 只报已收集文件数与前 0-10% 的估算进度），收集完成后知道 total，
        # 解析阶段（phase=parsing）再报真实百分比（10-95%）。
        try:
            files = _collect_source_files(
                ws, cancel_file=cancel_file,
                on_progress=lambda n: _progress(
                    min(10, 1 + n // 500), phase="scanning", files_scanned=n,
                ),
            )
        except _ScanCancelled:
            conn.rollback()
            stats["cancelled"] = True
            stats["progress"] = 0
            return stats

        total = len(files)
        stats["files_total"] = total
        # 收集完成：立即告知总数（此时切 parsing，进度从 10% 起）
        _progress(10, phase="parsing", files_scanned=0, files_total=total)
        for f, st in files:
            rel = f.relative_to(ws).as_posix()
            current.add(rel)
            stats["files_scanned"] += 1
            if stats["files_scanned"] % 32 == 0:
                _progress(
                    min(95, 10 + stats["files_scanned"] * 85 // max(1, total)),
                    phase="parsing", files_scanned=stats["files_scanned"], files_total=total,
                )
            # 大型仓库的 AST/正则解析在工作线程中运行；周期性让出 GIL，避免
            # Windows 单进程服务的其它 HTTP 协程在全量索引期间长时间得不到调度。
            if stats["files_scanned"] % 4 == 0:
                time.sleep(0)
            # 逐文件取消检查降频到 64 个一次（收集阶段已有每 128 项的快速检查）
            if cancel_file and stats["files_scanned"] % 64 == 0 and Path(cancel_file).exists():
                conn.rollback()
                stats["cancelled"] = True
                stats["progress"] = min(95, 10 + stats["files_scanned"] * 85 // max(1, total))
                return stats
            if st.st_size > _MAX_FILE_BYTES:
                continue
            rec = known.get(rel)
            if not force and rec is not None and rec[1] == st.st_mtime and rec[2] == st.st_size:
                continue  # mtime+size 未变：零 IO 跳过（增量核心）
            try:
                raw = f.read_bytes()
            except OSError:
                continue
            sha1 = hashlib.sha1(raw).hexdigest()
            if not force and rec is not None and rec[0] == sha1:
                # mtime 变了但内容未变：只刷新登记（mtime/size），不重解析
                conn.execute(
                    "UPDATE files SET mtime = ?, size = ? WHERE file_path = ?",
                    (st.st_mtime, st.st_size, rel),
                )
                continue

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
                "INSERT OR REPLACE INTO files"
                " (file_path, mtime, size, sha1, symbol_count, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (rel, st.st_mtime, st.st_size, sha1, len(syms), time.time()),
            )
            stats["files_updated"] += 1
            # 分批提交：此前整轮扫描挂在一个大写事务上直到结束才 commit，
            # 长事务期间其它写入者（并发增量/重建）会 sqlite 超时报
            # "索引写入失败"；分批提交把写锁窗口从分钟级压到毫秒级。
            if stats["files_updated"] % 50 == 0:
                conn.commit()

        # 已删除文件：清理其符号。
        # 守卫：以磁盘为准，只清理确实不存在的文件。current 可能因遍历期
        # OSError/权限、收集阶段被跳过而漏项，直接按差集删除会把"仍存在但
        # 本轮未扫到"的符号误删（曾表现为"重新开启索引后搜索不到函数"）。
        removed = 0
        for rel in set(known) - current:
            try:
                if (ws / rel).exists():
                    continue
            except OSError:
                continue
            conn.execute("DELETE FROM symbols WHERE file_path = ?", (rel,))
            conn.execute("DELETE FROM files WHERE file_path = ?", (rel,))
            removed += 1
        stats["removed_files"] = removed

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
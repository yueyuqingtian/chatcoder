"""数据库驱动适配层（plan-282-1441 #7 内置 MCP「数据库连接」）。

统一三种数据库（MySQL / PostgreSQL / SQL Server）的连接与查询接口。
驱动按需导入：**未安装时给出可操作的提示**，而不是让 MCP 整体崩溃
（打包产物体积与 native 兼容性都要考虑，故不做强制依赖）。

语句安全：`classify_sql` 是唯一的语句类型判定入口，用**首词**判定
（read/write/ddl），并拒绝多语句（分号拼接）以防绕过权限门控。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"mysql": 3306, "postgresql": 5432, "sqlserver": 1433}

# ── 语句分类（唯一入口）──

_READ_HEADS = {"select", "show", "describe", "desc", "explain", "with", "pragma"}
_WRITE_HEADS = {"insert", "update", "delete", "replace", "merge", "upsert"}
_DDL_HEADS = {
    "create", "alter", "drop", "truncate", "rename", "comment",
    "grant", "revoke", "vacuum", "analyze", "reindex",
}


def _strip_leading_comments(sql: str) -> str:
    """去掉开头的空白、行注释与块注释，便于取首词。"""
    s = sql.lstrip()
    while True:
        if s.startswith("--") or s.startswith("#"):
            nl = s.find("\n")
            if nl == -1:
                return ""
            s = s[nl + 1:].lstrip()
            continue
        if s.startswith("/*"):
            end = s.find("*/")
            if end == -1:
                return ""
            s = s[end + 2:].lstrip()
            continue
        return s


def _has_multiple_statements(sql: str) -> bool:
    """是否包含多条语句（分号后可再有内容）。

    目的：防止 `select 1; drop table t` 这类"以只读开头、实际执行 DDL"的绕过。
    简单状态机跳过字符串字面量中的分号，避免误判（如 `where a = ';'`）。
    """
    in_single = in_double = in_backtick = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if in_single:
            if ch == "'":
                # 处理 '' 转义
                if i + 1 < n and sql[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif in_backtick:
            if ch == "`":
                in_backtick = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "`":
                in_backtick = True
            elif ch == ";":
                # 分号后只允许空白/注释
                if _strip_leading_comments(sql[i + 1:]).strip():
                    return True
        i += 1
    return False


def classify_sql(sql: str) -> tuple[str, str]:
    """判定语句类型 → ("read" | "write" | "ddl" | "unknown", reason)。

    多语句一律拒绝（返回 unknown），由调用方据此拒绝执行。
    """
    text = _strip_leading_comments(sql)
    if not text.strip():
        return "unknown", "空语句"
    if _has_multiple_statements(sql):
        return "unknown", "不支持一次提交多条语句（请拆分为单条执行）"
    head = re.split(r"[\s(]+", text, maxsplit=1)[0].lower()
    if head in _READ_HEADS:
        return "read", ""
    if head in _WRITE_HEADS:
        return "write", ""
    if head in _DDL_HEADS:
        return "ddl", ""
    return "unknown", f"无法识别的语句类型（首词 {head!r}）"


# ── 驱动连接 ──

def _missing_driver_hint(kind: str) -> str:
    pkg = {"mysql": "pymysql", "postgresql": "asyncpg", "sqlserver": "python-tds"}.get(kind, "")
    return (
        f"缺少 {kind} 驱动。请安装：pip install {pkg}"
        f"\n（打包版需在 chatcoder-server.spec 的 hiddenimports 中加入 {pkg} 后重新打包）"
    )


async def _run_mysql(cfg: dict, sql: str | None, params: tuple | None, timeout: int) -> dict:
    def _blocking() -> dict:
        try:
            import pymysql  # type: ignore
        except ImportError:
            raise RuntimeError(_missing_driver_hint("mysql")) from None
        conn = pymysql.connect(
            host=cfg["host"], port=int(cfg["port"]), user=cfg.get("username") or "",
            password=cfg.get("password") or "", database=cfg.get("database") or None,
            connect_timeout=timeout, read_timeout=timeout, charset=cfg.get("charset", "utf8mb4"),
            cursorclass=pymysql.cursors.Cursor,
        )
        try:
            with conn.cursor() as cur:
                if sql is None:
                    cur.execute("SELECT VERSION()")
                    row = cur.fetchone()
                    return {"ok": True, "server": f"MySQL {row[0] if row else ''}"}
                cur.execute(sql, params) if params else cur.execute(sql)
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    rows = [list(r) for r in cur.fetchmany(cfg.get("row_limit", 200))]
                    return {"ok": True, "columns": cols, "rows": rows,
                            "row_count": len(rows), "affected": None}
                conn.commit()
                return {"ok": True, "columns": [], "rows": [],
                        "row_count": 0, "affected": cur.rowcount}
        finally:
            conn.close()

    return await asyncio.wait_for(asyncio.to_thread(_blocking), timeout=timeout + 5)


async def _run_postgres(cfg: dict, sql: str | None, params: tuple | None, timeout: int) -> dict:
    try:
        import asyncpg  # type: ignore
    except ImportError:
        raise RuntimeError(_missing_driver_hint("postgresql")) from None
    conn = await asyncio.wait_for(asyncpg.connect(
        host=cfg["host"], port=int(cfg["port"]), user=cfg.get("username") or "",
        password=cfg.get("password") or "", database=cfg.get("database") or "postgres",
        timeout=timeout,
    ), timeout=timeout + 5)
    try:
        if sql is None:
            ver = await conn.fetchval("SELECT version()")
            return {"ok": True, "server": str(ver)[:120]}
        # 只读语句走 fetch，写/DDL 走 execute 并返回影响行数
        head, _ = classify_sql(sql)
        if head == "read":
            records = await asyncio.wait_for(
                conn.fetch(sql), timeout=timeout + 5)
            cols = list(records[0].keys()) if records else []
            rows = [[_jsonable(r[c]) for c in cols] for r in records[: cfg.get("row_limit", 200)]]
            return {"ok": True, "columns": cols, "rows": rows,
                    "row_count": len(rows), "affected": None}
        status = await asyncio.wait_for(conn.execute(sql), timeout=timeout + 5)
        # asyncpg 的 execute 返回形如 "UPDATE 3"
        affected = None
        try:
            affected = int(str(status).split()[-1])
        except (ValueError, IndexError):
            pass
        return {"ok": True, "columns": [], "rows": [], "row_count": 0, "affected": affected}
    finally:
        await conn.close()


async def _run_sqlserver(cfg: dict, sql: str | None, params: tuple | None, timeout: int) -> dict:
    try:
        import pytds  # type: ignore
    except ImportError:
        raise RuntimeError(_missing_driver_hint("sqlserver")) from None

    def _blocking() -> dict:
        conn = pytds.connect(
            server=cfg["host"], port=int(cfg["port"]), database=cfg.get("database") or "",
            user=cfg.get("username") or "", password=cfg.get("password") or "",
            timeout=timeout, login_timeout=timeout,
        )
        try:
            cur = conn.cursor()
            if sql is None:
                cur.execute("SELECT @@VERSION")
                row = cur.fetchone()
                return {"ok": True, "server": str(row[0])[:120] if row else ""}
            cur.execute(sql)
            if cur.description:
                cols = [d[0] for d in cur.description]
                rows = [list(r) for r in cur.fetchmany(cfg.get("row_limit", 200))]
                return {"ok": True, "columns": cols, "rows": rows,
                        "row_count": len(rows), "affected": None}
            conn.commit()
            return {"ok": True, "columns": [], "rows": [],
                    "row_count": 0, "affected": cur.rowcount}
        finally:
            conn.close()

    return await asyncio.wait_for(asyncio.to_thread(_blocking), timeout=timeout + 5)


def _jsonable(v: Any) -> Any:
    """把驱动返回的非 JSON 类型转成可序列化值。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return f"<binary {len(v)} bytes>"
    try:
        import datetime
        if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
            return v.isoformat()
        if isinstance(v, datetime.timedelta):
            return str(v)
    except Exception:  # noqa: BLE001
        pass
    # decimal / uuid 等
    return str(v)


_RUNNERS = {"mysql": _run_mysql, "postgresql": _run_postgres, "sqlserver": _run_sqlserver}


async def run_sql(*, kind: str, host: str, port: int | None, database: str | None,
                  username: str | None, password: str | None, params: dict,
                  sql: str | None, timeout_s: int = 15, row_limit: int = 200) -> dict:
    """执行一条 SQL（sql=None 时只做连通性探测）。"""
    if kind not in _RUNNERS:
        raise RuntimeError(f"不支持的数据库类型: {kind}")
    cfg = {
        "host": host,
        "port": port or _DEFAULT_PORTS.get(kind, 0),
        "database": database,
        "username": username,
        "password": password,
        "row_limit": row_limit,
        **(params or {}),
    }
    started = time.monotonic()
    try:
        result = await _RUNNERS[kind](cfg, sql, None, max(1, int(timeout_s)))
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result
    except asyncio.TimeoutError:
        raise RuntimeError(f"数据库操作超时（{timeout_s}s）") from None


async def execute_probe(**kwargs: Any) -> dict:
    """配置页「测试连接」：只探测版本，不执行用户 SQL。"""
    return await run_sql(sql=None, **kwargs)

"""内置 MCP：数据库连接（plan-282-1441 #7）。

作为独立进程（`python -m app.mcp_servers.database`）由 MCP 客户端拉起，
每次调用时按"当前工作区 → 项目"读取该项目的连接与权限策略。

设计要点：
- **权限在服务端强制**：每个工具先 `db_connection_service.check_permission`，
  再决定是否执行；前端开关只是配置。
- 连接按项目隔离：只暴露当前工作区所属项目的启用连接。
- 语句与结果的结构化数据一并返回（`structuredContent`），供消息流做 SQL 呈现。
- 密码只在进程内解密使用，绝不出现在返回值里。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from app.mcp_servers.base import ToolSpec, run
from app.mcp_servers.db_drivers import classify_sql, run_sql

# 当前工作区：由 MCP 客户端在 args 里以 ${workspaceFolder} 注入（见 mcp_wrapper 的占位符解析）
_WORKSPACE = ""


def _workspace_arg() -> str:
    """从命令行取工作区路径（--workspace <path>），回退到 cwd。"""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a in ("--workspace", "-w") and i + 1 < len(argv):
            return argv[i + 1]
    return os.getcwd()


def _resolve_project_id() -> int | None:
    """当前工作区对应的项目 id。

    直接查库（MCP 是独立进程，不能复用服务端内存态）。路径比较做规范化：
    Windows 下大小写与分隔符都要统一，否则匹配不到。
    """
    from sqlalchemy import select

    from app.persistence.database import async_session_factory
    from app.persistence.models.project import Project

    ws = os.path.normcase(os.path.abspath(_WORKSPACE)) if _WORKSPACE else ""

    async def _query() -> int | None:
        async with async_session_factory() as db:
            res = await db.execute(select(Project))
            for p in res.scalars().all():
                if os.path.normcase(os.path.abspath(p.path or "")) == ws:
                    return p.id
        return None

    return asyncio.run(_query())


def _load_context() -> tuple[int | None, list[dict], dict]:
    """读取（项目 id、启用的连接、权限策略）。"""
    from sqlalchemy import select

    from app.persistence.database import async_session_factory
    from app.persistence.models.db_connection import DbConnection, DbPolicy
    from app.services.db_connection_service import decrypt_password

    pid = _resolve_project_id()
    if pid is None:
        return None, [], {}

    async def _query() -> tuple[list[dict], dict]:
        async with async_session_factory() as db:
            res = await db.execute(
                select(DbConnection).where(
                    DbConnection.project_id == pid, DbConnection.is_active == True,  # noqa: E712
                ).order_by(DbConnection.id.asc())
            )
            conns = []
            for r in res.scalars().all():
                conns.append({
                    "id": r.id, "name": r.name, "kind": r.kind, "host": r.host,
                    "port": r.port, "database": r.database, "username": r.username,
                    "password": decrypt_password(r.password_enc), "params": r.params or {},
                })
            pres = await db.execute(select(DbPolicy).where(DbPolicy.project_id == pid))
            prow = pres.scalars().first()
            policy = {
                "allow_read": bool(prow.allow_read) if prow else True,
                "allow_write": bool(prow.allow_write) if prow else False,
                "allow_ddl": bool(prow.allow_ddl) if prow else False,
                "require_approval": bool(prow.require_approval) if prow else True,
                "row_limit": int(prow.row_limit) if prow else 200,
                "timeout_s": int(prow.timeout_s) if prow else 15,
            }
            return conns, policy

    conns, policy = asyncio.run(_query())
    return pid, conns, policy


def _pick_connection(conns: list[dict], name: str | None) -> dict | None:
    if not conns:
        return None
    if not name:
        return conns[0]
    for c in conns:
        if c["name"] == name:
            return c
    return None


def _fmt_table(columns: list[str], rows: list[list], max_rows: int = 50) -> str:
    """把结果渲染成模型易读的 markdown 表格（截断保护）。"""
    if not columns:
        return "（无结果集）"
    shown = rows[:max_rows]
    lines = ["| " + " | ".join(str(c) for c in columns) + " |",
             "| " + " | ".join("---" for _ in columns) + " |"]
    for r in shown:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    if len(rows) > len(shown):
        lines.append(f"（仅显示前 {len(shown)} 行，共 {len(rows)} 行）")
    return "\n".join(lines)


def _permission_error(reason: str) -> tuple[str, dict]:
    return (f"⛔ 操作被拒绝：{reason}", {"denied": True, "reason": reason})


# ── 工具实现 ──

def tool_list_connections(args: dict) -> tuple[str, dict]:
    pid, conns, policy = _load_context()
    if pid is None:
        return ("当前工作区未匹配到任何项目，无法使用数据库连接。"
                "请在设置中把该目录添加为项目。", {"connections": []})
    if not conns:
        return ("当前项目没有已启用的数据库连接。请在「设置 → 拓展 → 连接器 → 数据库连接」"
                "中为该项目添加并启用连接。", {"connections": []})
    lines = [f"# 项目可用的数据库连接（{len(conns)} 个）"]
    for c in conns:
        lines.append(f"- **{c['name']}**（{c['kind']}） {c['host']}:{c['port']} / {c['database'] or '-'}"
                     f"　账号 {c['username'] or '-'}")
    lines.append(
        f"\n当前权限：读={policy['allow_read']} 写={policy['allow_write']} DDL={policy['allow_ddl']}"
        f" 需审批={policy['require_approval']}"
    )
    safe = [{k: v for k, v in c.items() if k != "password"} for c in conns]
    return "\n".join(lines), {"connections": safe, "policy": policy}


def tool_schema(args: dict) -> tuple[str, dict]:
    _, conns, policy = _load_context()
    from app.services.db_connection_service import check_permission

    ok, reason = check_permission(policy, "read")
    if not ok:
        return _permission_error(reason)
    conn = _pick_connection(conns, args.get("connection"))
    if conn is None:
        return ("当前项目没有可用连接。", {"tables": []})

    kind = conn["kind"]
    if kind == "mysql":
        sql = ("SELECT table_name, column_name, data_type, is_nullable "
               "FROM information_schema.columns WHERE table_schema = DATABASE()")
    elif kind == "postgresql":
        sql = ("SELECT table_name, column_name, data_type, is_nullable "
               "FROM information_schema.columns WHERE table_schema = 'public'")
    else:
        sql = ("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
               "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = SCHEMA_NAME()")
    try:
        res = asyncio.run(run_sql(
            kind=kind, host=conn["host"], port=conn["port"], database=conn["database"],
            username=conn["username"], password=conn["password"], params=conn["params"],
            sql=sql, timeout_s=policy["timeout_s"], row_limit=2000,
        ))
    except Exception as e:  # noqa: BLE001
        return (f"读取表结构失败：{e}", {"error": str(e)})

    rows = res.get("rows") or []
    # 按表聚合，输出更紧凑
    tables: dict[str, list[str]] = {}
    for r in rows:
        if len(r) < 4:
            continue
        tname, col, dtype, nullable = r[0], r[1], r[2], r[3]
        tables.setdefault(str(tname), []).append(
            f"{col} {dtype}{'' if nullable in ('YES', True) else ' NOT NULL'}")
    if not tables:
        return ("没有读到表结构（库可能为空）", {"tables": []})
    lines = [f"# 数据库结构（{len(tables)} 张表）"]
    for t, cols in sorted(tables.items()):
        lines.append(f"\n## {t}")
        for c in cols:
            lines.append(f"- {c}")
    return "\n".join(lines), {
        "tables": [{"name": t, "columns": cols} for t, cols in sorted(tables.items())]
    }


def tool_query(args: dict) -> tuple[str, dict]:
    """只读查询（SELECT/WITH/SHOW/EXPLAIN）。"""
    _, conns, policy = _load_context()
    from app.services.db_connection_service import check_permission

    ok, reason = check_permission(policy, "read")
    if not ok:
        return _permission_error(reason)

    sql = str(args.get("sql") or "").strip()
    if not sql:
        return ("缺少 sql 参数。", {"error": "missing sql"})
    stmt_kind, why = classify_sql(sql)
    if stmt_kind != "read":
        # 只读工具必须严格只读——写入/DDL 请改用对应工具（各自受独立权限门控）
        return (f"⛔ db_query 只允许只读语句。当前语句被判定为 {stmt_kind}。{why}", {
            "denied": True, "reason": f"非只读语句（{stmt_kind}）"})

    conn = _pick_connection(conns, args.get("connection"))
    if conn is None:
        return ("当前项目没有可用连接。", {"error": "no connection"})
    try:
        res = asyncio.run(run_sql(
            kind=conn["kind"], host=conn["host"], port=conn["port"], database=conn["database"],
            username=conn["username"], password=conn["password"], params=conn["params"],
            sql=sql, timeout_s=policy["timeout_s"], row_limit=policy["row_limit"],
        ))
    except Exception as e:  # noqa: BLE001
        return (f"查询失败：{e}", {"error": str(e), "sql": sql})

    cols = res.get("columns") or []
    rows = res.get("rows") or []
    text = (f"连接 {conn['name']}（{conn['kind']}）· 耗时 {res.get('elapsed_ms')}ms\n"
            f"```sql\n{sql}\n```\n{_fmt_table(cols, rows, 50)}")
    return text, {"sql": sql, "columns": cols, "rows": rows,
                  "row_count": res.get("row_count", len(rows)),
                  "elapsed_ms": res.get("elapsed_ms"), "op": "read", "connection": conn["name"]}


def tool_execute(args: dict) -> tuple[str, dict]:
    """数据变更（INSERT/UPDATE/DELETE）。"""
    _, conns, policy = _load_context()
    from app.services.db_connection_service import check_permission

    ok, reason = check_permission(policy, "write")
    if not ok:
        return _permission_error(reason)

    sql = str(args.get("sql") or "").strip()
    if not sql:
        return ("缺少 sql 参数。", {"error": "missing sql"})
    stmt_kind, why = classify_sql(sql)
    if stmt_kind != "write":
        return (f"⛔ db_execute 只允许数据变更语句（INSERT/UPDATE/DELETE）。"
                f"当前语句被判定为 {stmt_kind}。{why}", {"denied": True, "reason": why or stmt_kind})

    conn = _pick_connection(conns, args.get("connection"))
    if conn is None:
        return ("当前项目没有可用连接。", {"error": "no connection"})
    try:
        res = asyncio.run(run_sql(
            kind=conn["kind"], host=conn["host"], port=conn["port"], database=conn["database"],
            username=conn["username"], password=conn["password"], params=conn["params"],
            sql=sql, timeout_s=policy["timeout_s"], row_limit=policy["row_limit"],
        ))
    except Exception as e:  # noqa: BLE001
        return (f"执行失败：{e}", {"error": str(e), "sql": sql})

    affected = res.get("affected")
    text = (f"连接 {conn['name']}（{conn['kind']}）· 影响 {affected if affected is not None else '?'} 行"
            f" · 耗时 {res.get('elapsed_ms')}ms\n```sql\n{sql}\n```")
    return text, {"sql": sql, "affected": affected, "op": "write",
                  "elapsed_ms": res.get("elapsed_ms"), "connection": conn["name"]}


def tool_ddl(args: dict) -> tuple[str, dict]:
    """结构变更（CREATE/ALTER/DROP/TRUNCATE）。"""
    _, conns, policy = _load_context()
    from app.services.db_connection_service import check_permission

    ok, reason = check_permission(policy, "ddl")
    if not ok:
        return _permission_error(reason)

    sql = str(args.get("sql") or "").strip()
    if not sql:
        return ("缺少 sql 参数。", {"error": "missing sql"})
    stmt_kind, why = classify_sql(sql)
    if stmt_kind != "ddl":
        return (f"⛔ db_ddl 只允许结构变更语句（CREATE/ALTER/DROP/TRUNCATE）。"
                f"当前语句被判定为 {stmt_kind}。{why}", {"denied": True, "reason": why or stmt_kind})

    conn = _pick_connection(conns, args.get("connection"))
    if conn is None:
        return ("当前项目没有可用连接。", {"error": "no connection"})
    try:
        res = asyncio.run(run_sql(
            kind=conn["kind"], host=conn["host"], port=conn["port"], database=conn["database"],
            username=conn["username"], password=conn["password"], params=conn["params"],
            sql=sql, timeout_s=policy["timeout_s"], row_limit=policy["row_limit"],
        ))
    except Exception as e:  # noqa: BLE001
        return (f"DDL 执行失败：{e}", {"error": str(e), "sql": sql})

    text = (f"⚠️ 已执行结构变更 · 连接 {conn['name']}（{conn['kind']}）"
            f" · 耗时 {res.get('elapsed_ms')}ms\n```sql\n{sql}\n```")
    return text, {"sql": sql, "op": "ddl", "elapsed_ms": res.get("elapsed_ms"),
                  "connection": conn["name"]}


_CONN_ARG = {
    "type": "string",
    "description": "连接名称（省略则用该项目的第一个启用连接）",
}


def _build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            "db_list_connections",
            "列出当前项目已启用的数据库连接及其权限（不返回密码）。",
            {"type": "object", "properties": {}},
            tool_list_connections,
            risk_level="low",
        ),
        ToolSpec(
            "db_schema",
            "读取当前项目数据库的表与列结构（表名、字段、类型、是否可空）。",
            {"type": "object", "properties": {"connection": _CONN_ARG}},
            tool_schema,
            risk_level="low",
        ),
        ToolSpec(
            "db_query",
            "执行**只读** SQL 查询（仅允许 SELECT/WITH/SHOW/EXPLAIN）；"
            "返回结果并附带语句、耗时与行数，供用户核对。",
            {"type": "object", "properties": {
                "sql": {"type": "string", "description": "单条只读 SQL"},
                "connection": _CONN_ARG,
            }, "required": ["sql"]},
            tool_query,
            risk_level="low",
        ),
        ToolSpec(
            "db_execute",
            "执行**数据变更** SQL（仅 INSERT/UPDATE/DELETE）；需项目开启写入权限。",
            {"type": "object", "properties": {
                "sql": {"type": "string", "description": "单条 DML 语句"},
                "connection": _CONN_ARG,
            }, "required": ["sql"]},
            tool_execute,
            risk_level="medium",
        ),
        ToolSpec(
            "db_ddl",
            "执行**结构变更** SQL（CREATE/ALTER/DROP/TRUNCATE）；"
            "需项目单独开启 DDL 权限，属高危操作。",
            {"type": "object", "properties": {
                "sql": {"type": "string", "description": "单条 DDL 语句"},
                "connection": _CONN_ARG,
            }, "required": ["sql"]},
            tool_ddl,
            risk_level="high",
        ),
    ]


def main() -> None:
    global _WORKSPACE
    _WORKSPACE = _workspace_arg()
    run("chatcoder-database", "1.0.0", _build_tools)


if __name__ == "__main__":
    main()

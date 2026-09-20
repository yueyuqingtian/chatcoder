"""数据库连接服务（plan-282-1441 #7 内置 MCP「数据库连接」）。

职责：
- 连接 CRUD（按项目归属）；密码对称加密存储，接口只回 "是否已设置密码"。
- 权限策略读写（读 / 写 / DDL / 审批 / 行数 / 超时）。
- **服务端强制门控**：`check_permission` 是唯一的放行入口——前端开关不是安全边界。
- 连接测试（真实建连），供配置页「测试连接」按钮使用。
"""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.persistence.models.db_connection import DbConnection, DbPolicy

logger = logging.getLogger(__name__)

SUPPORTED_KINDS = ("mysql", "postgresql", "sqlserver")

_DEFAULT_PORTS = {"mysql": 3306, "postgresql": 5432, "sqlserver": 1433}


# ── 密码加解密（对称，密钥取自应用配置）──

def _secret() -> bytes:
    """派生加解密密钥。

    优先用 settings 里的 JWT secret（已有且随部署变化）；缺失时回退到主机名+salt，
    保证"至少不是明文"，并在日志中提示用户配置。
    """
    raw = str(getattr(settings, "jwt_secret", "") or "")
    if not raw:
        import socket

        raw = f"chatcoder-db-{socket.gethostname()}"
        logger.warning("[db] 未配置 jwt_secret，数据库密码加密使用回退密钥（建议配置）")
    return hashlib.sha256(raw.encode("utf-8")).digest()


def encrypt_password(plain: str | None) -> str | None:
    """加密（XOR + base64 的轻量实现）。

    说明：这里不引入 cryptography 的重型口令 KDF——目标是"避免明文落库"，
    且密钥与应用配置绑定。若后续要提高强度，只需替换本函数与 decrypt_password，
    存量密文按解密失败处理（回退要求重填密码）。
    """
    if plain is None:
        return None
    key = _secret()
    data = plain.encode("utf-8")
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return "v1:" + base64.b64encode(out).decode("ascii")


def decrypt_password(enc: str | None) -> str | None:
    if not enc:
        return None
    if not enc.startswith("v1:"):
        # 旧数据或非本方案写入：按明文处理（兼容），并提示
        logger.warning("[db] 遇到非本方案格式的密码字段，按明文使用")
        return enc
    try:
        blob = base64.b64decode(enc[3:])
        key = _secret()
        out = bytes(b ^ key[i % len(key)] for i, b in enumerate(blob))
        return out.decode("utf-8")
    except Exception:  # noqa: BLE001
        logger.warning("[db] 密码解密失败（密钥可能已变更）", exc_info=True)
        return None


# ── 连接 CRUD ──

def _to_out(row: DbConnection) -> dict:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "name": row.name,
        "kind": row.kind,
        "host": row.host,
        "port": row.port or _DEFAULT_PORTS.get(row.kind, 0),
        "database": row.database,
        "username": row.username,
        # 绝不回传密码本身，只告知是否已配置
        "has_password": bool(row.password_enc),
        "params": row.params or {},
        "is_active": bool(row.is_active),
    }


async def list_connections(db: AsyncSession, project_id: int) -> list[dict]:
    res = await db.execute(
        select(DbConnection).where(DbConnection.project_id == project_id).order_by(DbConnection.id.asc())
    )
    return [_to_out(r) for r in res.scalars().all()]


async def get_connection(db: AsyncSession, conn_id: int) -> DbConnection | None:
    return await db.get(DbConnection, conn_id)


async def create_connection(db: AsyncSession, *, project_id: int, name: str, kind: str,
                            host: str, port: int | None = None, database: str | None = None,
                            username: str | None = None, password: str | None = None,
                            params: dict | None = None, is_active: bool = False) -> int:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"不支持的数据库类型: {kind}（支持 {', '.join(SUPPORTED_KINDS)}）")
    if not name.strip():
        raise ValueError("连接名称不能为空")
    if not host.strip():
        raise ValueError("主机不能为空")
    row = DbConnection(
        project_id=project_id, name=name.strip(), kind=kind, host=host.strip(),
        port=int(port) if port else _DEFAULT_PORTS.get(kind),
        database=(database or None), username=(username or None),
        password_enc=encrypt_password(password), params=params or {},
        is_active=bool(is_active),
    )
    db.add(row)
    await db.commit()
    return row.id


async def update_connection(db: AsyncSession, conn_id: int, **fields: Any) -> bool:
    row = await db.get(DbConnection, conn_id)
    if row is None:
        return False
    if "kind" in fields and fields["kind"] and fields["kind"] not in SUPPORTED_KINDS:
        raise ValueError(f"不支持的数据库类型: {fields['kind']}")
    if "password" in fields:
        # 显式传 password 才更新；None 表示"不改动"
        pwd = fields.pop("password")
        if pwd is not None:
            row.password_enc = encrypt_password(str(pwd))
    for k in ("name", "kind", "host", "port", "database", "username", "params", "is_active"):
        if k in fields and fields[k] is not None:
            setattr(row, k, fields[k])
    await db.commit()
    return True


async def delete_connection(db: AsyncSession, conn_id: int) -> bool:
    row = await db.get(DbConnection, conn_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


# ── 权限策略 ──

def _default_policy(project_id: int) -> DbPolicy:
    # 保守默认：只读开、写与 DDL 关、强制审批
    return DbPolicy(
        project_id=project_id, allow_read=True, allow_write=False,
        allow_ddl=False, require_approval=True, row_limit=200, timeout_s=15,
    )


async def get_policy(db: AsyncSession, project_id: int) -> dict:
    res = await db.execute(select(DbPolicy).where(DbPolicy.project_id == project_id))
    row = res.scalars().first()
    if row is None:
        d = _default_policy(project_id)
        return {
            "project_id": project_id, "allow_read": d.allow_read,
            "allow_write": d.allow_write, "allow_ddl": d.allow_ddl,
            "require_approval": d.require_approval,
            "row_limit": d.row_limit, "timeout_s": d.timeout_s,
        }
    return {
        "project_id": row.project_id, "allow_read": bool(row.allow_read),
        "allow_write": bool(row.allow_write), "allow_ddl": bool(row.allow_ddl),
        "require_approval": bool(row.require_approval),
        "row_limit": int(row.row_limit or 200), "timeout_s": int(row.timeout_s or 15),
    }


async def set_policy(db: AsyncSession, project_id: int, **fields: Any) -> dict:
    res = await db.execute(select(DbPolicy).where(DbPolicy.project_id == project_id))
    row = res.scalars().first()
    if row is None:
        row = _default_policy(project_id)
        db.add(row)
    for k in ("allow_read", "allow_write", "allow_ddl", "require_approval", "row_limit", "timeout_s"):
        if k in fields and fields[k] is not None:
            setattr(row, k, fields[k])
    await db.commit()
    return await get_policy(db, project_id)


def check_permission(policy: dict, op: str) -> tuple[bool, str]:
    """**服务端强制门控**：判断某类操作是否被当前策略允许。

    op: read（查询/结构）| write（DML）| ddl（建删改表）
    返回 (allowed, reason)。
    """
    if op == "read":
        if not policy.get("allow_read", True):
            return False, "该项目未开启数据库读取权限（设置 → 拓展 → 连接器 → 数据库连接）"
        return True, ""
    if op == "write":
        if not policy.get("allow_read", True) or not policy.get("allow_write", False):
            return False, "该项目未开启数据库写入权限（当前仅允许只读）"
        return True, ""
    if op == "ddl":
        if not policy.get("allow_ddl", False):
            return False, "该项目未开启数据库 DDL 权限（建表/改表/删表被禁止）"
        return True, ""
    return False, f"未知操作类型: {op}"


# ── 连接测试 ──

async def test_connection(db: AsyncSession, conn_id: int) -> dict:
    """真实建连并执行一次最轻量的探测查询。"""
    row = await get_connection(db, conn_id)
    if row is None:
        raise ValueError("连接不存在")
    from app.mcp_servers.db_drivers import execute_probe

    return await execute_probe(
        kind=row.kind, host=row.host, port=row.port,
        database=row.database, username=row.username,
        password=decrypt_password(row.password_enc),
        params=row.params or {}, timeout_s=int((await get_policy(db, row.project_id))["timeout_s"]),
    )

"""内置 MCP「数据库连接」测试（plan-282-1441 #7）。

覆盖三块安全/正确性关键点：
1. 语句分类（classify_sql）——权限门控的唯一入口，必须拒绝多语句绕过；
2. 权限门控（check_permission）——服务端强制，不是前端开关说了算；
3. 密码加解密——不得明文落库；以及内置 MCP 的幂等注册。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.mcp_servers.db_drivers import classify_sql
from app.persistence.database import Base
from app.persistence.models import McpServer  # noqa: F401 注册模型
from app.services import db_connection_service as svc


# ── 语句分类：安全边界的唯一入口 ──

@pytest.mark.parametrize("sql,expect", [
    ("SELECT * FROM users", "read"),
    ("select 1", "read"),
    ("WITH t AS (SELECT 1) SELECT * FROM t", "read"),
    ("  -- 注释\nSELECT 1", "read"),
    ("/* 块注释 */ SHOW TABLES", "read"),
    ("EXPLAIN SELECT 1", "read"),
    ("INSERT INTO t VALUES (1)", "write"),
    ("UPDATE t SET a=1", "write"),
    ("DELETE FROM t", "write"),
    ("CREATE TABLE t (id int)", "ddl"),
    ("ALTER TABLE t ADD c int", "ddl"),
    ("DROP TABLE t", "ddl"),
    ("TRUNCATE TABLE t", "ddl"),
    # VACUUM/ANALYZE 会实际改动数据库文件与统计信息，归入 DDL 权限管辖（保守但安全）
    ("VACUUM", "ddl"),
    ("ANALYZE t", "ddl"),
])
def test_classify_sql_kinds(sql, expect):
    kind, _ = classify_sql(sql)
    assert kind == expect, f"{sql!r} 应判定为 {expect}，实际 {kind}"


@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE users",          # 只读开头 + 隐藏 DDL（经典绕过）
    "SELECT 1; DELETE FROM t",
    "select 1 ;\n drop table t",
    "",
    "   ",
    "FOOBAR whatever",                     # 未知首词（谨慎拒绝）
])
def test_classify_sql_rejects_bypass(sql):
    """多语句拼接必须被拒绝——否则 AI 可用"只读开头"绕过权限。"""
    kind, reason = classify_sql(sql)
    assert kind == "unknown", f"{sql!r} 不应被判为可执行类别（得到 {kind}）"
    assert reason


def test_classify_sql_allows_semicolon_in_literal():
    """字符串字面量里的分号不算多语句（避免误杀正常查询）。"""
    kind, _ = classify_sql("SELECT * FROM t WHERE a = ';'")
    assert kind == "read"


# ── 权限门控 ──

def test_check_permission_read_only_default():
    """默认策略：读开、写与 DDL 关。"""
    policy = {"allow_read": True, "allow_write": False, "allow_ddl": False}
    assert svc.check_permission(policy, "read")[0] is True
    assert svc.check_permission(policy, "write")[0] is False
    assert svc.check_permission(policy, "ddl")[0] is False


def test_check_permission_denies_read_when_disabled():
    policy = {"allow_read": False, "allow_write": True, "allow_ddl": True}
    ok, reason = svc.check_permission(policy, "read")
    assert ok is False and reason


def test_check_permission_write_requires_read_too():
    """写权限隐含要求读权限——只给写不给读是矛盾配置，按拒绝处理。"""
    policy = {"allow_read": False, "allow_write": True, "allow_ddl": False}
    assert svc.check_permission(policy, "write")[0] is False


def test_check_permission_ddl_independent():
    policy = {"allow_read": True, "allow_write": True, "allow_ddl": False}
    assert svc.check_permission(policy, "ddl")[0] is False
    policy2 = {"allow_read": True, "allow_write": False, "allow_ddl": True}
    assert svc.check_permission(policy2, "ddl")[0] is True


def test_check_permission_unknown_op_denied():
    assert svc.check_permission({}, "truncate_everything")[0] is False


# ── 密码加解密 ──

def test_password_roundtrip_and_not_plaintext():
    enc = svc.encrypt_password("s3cret!")
    assert enc is not None
    assert "s3cret!" not in enc, "密文不得包含明文"
    assert enc.startswith("v1:")
    assert svc.decrypt_password(enc) == "s3cret!"


def test_password_none_and_empty():
    assert svc.encrypt_password(None) is None
    assert svc.decrypt_password(None) is None


def test_password_empty_string_roundtrip():
    enc = svc.encrypt_password("")
    assert svc.decrypt_password(enc) == ""


# ── 内置 MCP 幂等注册 ──

@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/dbmcp.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


@pytest.mark.asyncio
async def test_seed_builtin_mcp_idempotent_and_defaults_off(db):
    """内置 MCP 幂等注册：重复执行不重复插入，且**默认不启用**。

    默认关闭是产品要求（用户手动开启），必须由测试守住——
    否则一次误改就会让所有用户的数据库 MCP 默认开着。
    """
    from app.persistence.seed import _seed_builtin_mcp

    first = await _seed_builtin_mcp(db)
    assert first == 2, "应注册 database 与 debugger 两个内置 MCP"

    rows = (await db.execute(select(McpServer).where(McpServer.source == "builtin"))).scalars().all()
    assert {r.name for r in rows} == {"database", "debugger"}
    assert all(r.is_active is False for r in rows), "内置 MCP 必须默认关闭"

    # 第二次执行：不新增，且保留用户已开启的状态
    for r in rows:
        r.is_active = True
    await db.commit()
    second = await _seed_builtin_mcp(db)
    assert second == 0
    rows2 = (await db.execute(select(McpServer).where(McpServer.source == "builtin"))).scalars().all()
    assert all(r.is_active is True for r in rows2), "重复播种不得覆盖用户的启停选择"


@pytest.mark.asyncio
async def test_builtin_mcp_entries_are_stdio_with_command(db):
    """内置 MCP 必须带可执行入口（否则启用后握手必然失败）。"""
    from app.persistence.seed import _seed_builtin_mcp

    await _seed_builtin_mcp(db)
    rows = (await db.execute(select(McpServer).where(McpServer.source == "builtin"))).scalars().all()
    for r in rows:
        assert r.transport == "stdio"
        assert r.command, f"{r.name} 缺少 command"
        assert r.args, f"{r.name} 缺少 args"

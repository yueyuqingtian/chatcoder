"""SQLAlchemy 异步引擎与会话。"""
import asyncio
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _pool_kwargs(url: str) -> dict:
    """SQLite 不支持 pool_size/max_overflow,按 dialect 决定。"""
    if url.startswith("sqlite"):
        return {}
    return {"pool_size": 10, "max_overflow": 20}


def _connect_args(url: str) -> dict:
    """SQLite: busy_timeout 让写操作排队等待,避免立即报锁错误。"""
    if url.startswith("sqlite"):
        return {"timeout": 30, "check_same_thread": False}
    return {}


engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    # 问题3: SQLite 用 pool_pre_ping 无意义（每次取连接多一次 SELECT 1），仅对非 SQLite 启用
    pool_pre_ping=not settings.database_url.startswith("sqlite"),
    connect_args=_connect_args(settings.database_url),
    **_pool_kwargs(settings.database_url),
)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, conn_record):
        """SQLite 优化:WAL 提升并发读,busy_timeout=30s 让写操作排队等待(配合串行调度)。"""
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


# 问题3: SQLite 单写者——进程内写锁串行化所有 commit，避免多会话/主子代理并发写
# 击穿 busy_timeout 报 "database is locked"。WAL 下读不需锁，仅锁写提交。
# 通过自定义 AsyncSession 覆盖 commit() 自动加锁：所有经 async_session_factory /
# get_db 得到的会话写提交全被串行化，无需逐点替换调用处的 commit。
_db_write_lock = asyncio.Lock()


class LockedAsyncSession(AsyncSession):
    """commit/flush 自动串行化的 AsyncSession——写提交与写 SQL 均经进程内写锁。

    v0.3.1: flush 同样持锁——SQLite 的写事务始于第一条 INSERT/UPDATE（flush），
    此前仅锁 commit 时，多会话并发 flush 仍会"database is locked"（busy_timeout 等待
    超时后抛错），触发 create_message/usage 落库失败 → rollback 主 db 会话 → 该会话上
    agent/session/turn 等已加载对象全部过期 → 后续属性访问在 asyncio 上下文走同步 reload
    抛 MissingGreenlet（SQLAlchemy xd2s）。锁覆盖 flush 后并发写冲突从源头消除。
    """

    async def commit(self) -> None:
        async with _db_write_lock:
            await super().commit()

    async def flush(self) -> None:
        async with _db_write_lock:
            await super().flush()


async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=LockedAsyncSession)


async def db_commit(db: AsyncSession) -> None:
    """写提交入口（锁已由 LockedAsyncSession.commit 串行化，此处不再重复加锁）。"""
    await db.commit()


async def commit_with_retry(db: AsyncSession, retries: int = 3) -> None:
    """serially commit with bounded exponential backoff on SQLite lock errors.

    message_service 原有 4 次退避重试范式上提：OperationalError locked 为瞬时写竞争，
    退避后重试通常一次即成功。适用于 todo/task/turn 状态/usage 等写路径。
    """
    for attempt in range(retries):
        try:
            await db_commit(db)
            return
        except OperationalError as exc:
            msg = str(getattr(exc, "orig", None) or exc).lower()
            if "locked" not in msg:
                raise
            if attempt >= retries - 1:
                raise
            await asyncio.sleep(0.1 * (1 << attempt))


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每请求一个会话。"""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """v0.3: 启动时创建所有表(MVP 不引入 Alembic)。

    导入所有模型以确保 Base.metadata 注册完整,然后 create_all。
    幂等:已存在的表不会被重建。
    """
    # 触发模型注册(side effect: 注册到 Base.metadata)
    from app.persistence import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

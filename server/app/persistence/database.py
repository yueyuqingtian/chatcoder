"""SQLAlchemy 异步引擎与会话。"""
from collections.abc import AsyncGenerator

from sqlalchemy import event
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
    pool_pre_ping=True,
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


async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


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

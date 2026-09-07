"""SQLAlchemy 异步引擎与会话。

SQLite 在桌面端是单写者数据库。这里统一关闭隐式 autoflush，并把所有
显式 flush/commit 经过同一个进程内协调器；业务代码不得依赖查询触发写入。
"""
import asyncio
import logging
import time
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TypeVar

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def database_info() -> dict[str, object]:
    """返回不含密钥的数据库运行信息，供健康检查/诊断页确认实例归属。"""
    url = settings.database_url
    if url.startswith("sqlite"):
        from pathlib import Path
        raw_path = url.rsplit("///", 1)[-1]
        path = str(Path(raw_path).resolve())
        return {
            "kind": "sqlite",
            "path": path,
            "wal": True,
            "busy_timeout_ms": 10000,
            "autoflush": False,
        }
    return {"kind": "postgresql", "path": "", "autoflush": False}


def _pool_kwargs(url: str) -> dict:
    """SQLite 不支持 pool_size/max_overflow,按 dialect 决定。"""
    if url.startswith("sqlite"):
        return {}
    return {"pool_size": 10, "max_overflow": 20}


def _connect_args(url: str) -> dict:
    """SQLite 连接参数；busy_timeout 只是跨进程锁的最后一道兜底。"""
    if url.startswith("sqlite"):
        return {"timeout": 10, "check_same_thread": False}
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
        """WAL 提升读并发；连接级 busy_timeout 仅兜底跨进程竞争。"""
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# SQLite 的跨进程写锁由文件数据库自身负责；进程内只在真正 commit 的短窗口
# 串行化，避免 A flush 后等待其它协程、B 又无法推进 A commit。
# 按 event loop 保存锁，防止 pytest/重载的旧 asyncio.Lock 被新 loop 复用。
_commit_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()
_flush_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _loop_lock(store: weakref.WeakKeyDictionary) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = store.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        store[loop] = lock
    return lock


def _commit_lock() -> asyncio.Lock:
    return _loop_lock(_commit_locks)


def _flush_lock() -> asyncio.Lock:
    return _loop_lock(_flush_locks)


def _is_sqlite_lock_error(exc: BaseException) -> bool:
    """只把 SQLite 锁冲突视为可诊断的瞬时竞争。"""
    if not isinstance(exc, OperationalError):
        return False
    message = str(getattr(exc, "orig", None) or exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _is_write_statement(statement) -> bool:
    """识别直接 execute 的 DML/DDL，避免绕过 flush 协调器。"""
    if getattr(statement, "is_dml", False) or getattr(statement, "is_ddl", False):
        return True
    sql = str(statement).lstrip().upper()
    return sql.startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ", "ALTER ", "CREATE ", "DROP "))


class LockedAsyncSession(AsyncSession):
    """关闭隐式 autoflush，并记录显式 flush/commit 耗时。"""

    async def execute(self, statement, params=None, *, execution_options=None,
                      bind_arguments=None, **kwargs):
        if settings.database_url.startswith("sqlite") and _is_write_statement(statement):
            lock = _flush_lock()
            await lock.acquire()
            try:
                return await super().execute(
                    statement, params, execution_options=execution_options,
                    bind_arguments=bind_arguments, **kwargs,
                )
            finally:
                lock.release()
        return await super().execute(
            statement, params, execution_options=execution_options,
            bind_arguments=bind_arguments, **kwargs,
        )

    async def commit(self) -> None:
        started = time.monotonic()
        lock = _commit_lock()
        wait_started = time.monotonic()
        await lock.acquire()
        waited = time.monotonic() - wait_started
        if waited >= 0.05:
            logger.info("[db.lock.wait] commit_wait=%.3fs", waited)
        try:
            await super().commit()
        except Exception:
            try:
                await super().rollback()
            except Exception:
                logger.debug("[db.commit] rollback failed", exc_info=True)
            raise
        finally:
            lock.release()
            elapsed = time.monotonic() - started
            if elapsed >= 0.05:
                logger.debug("[db.commit] elapsed=%.3fs", elapsed)

    async def flush(self, objects=None) -> None:
        started = time.monotonic()
        lock = _flush_lock()
        await lock.acquire()
        try:
            if objects is None:
                await super().flush()
            else:
                await super().flush(objects=objects)
        except Exception:
            try:
                await super().rollback()
            except Exception:
                logger.debug("[db.flush] rollback failed", exc_info=True)
            raise
        finally:
            lock.release()
            elapsed = time.monotonic() - started
            if elapsed >= 0.05:
                logger.debug("[db.flush] elapsed=%.3fs", elapsed)


# SQLAlchemy 2.x 的 AsyncSession 不提供 expire_on_rollback 参数；
# 回滚后的 ORM 属性不能作为可靠数据源，业务代码必须使用预先缓存的标量或重新查询。
async_session_factory = async_sessionmaker(
    engine,
    autoflush=False,
    expire_on_commit=False,
    class_=LockedAsyncSession,
)


async def db_commit(db: AsyncSession) -> None:
    """统一提交入口。调用方应先显式 flush，且不得跨长 IO 持有脏事务。"""
    await db.commit()


async def rollback_safely(db: AsyncSession) -> None:
    """回滚并清理事务状态；回滚失败不覆盖原始异常。"""
    try:
        await db.rollback()
    except Exception:
        logger.debug("[db.rollback] rollback failed", exc_info=True)


async def commit_with_retry(db: AsyncSession, retries: int = 3, label: str = "db") -> None:
    """提交当前短事务；锁冲突只做有界等待，不在回滚后假装重试 ORM 对象。"""
    started = time.monotonic()
    try:
        await db_commit(db)
    except Exception as exc:
        if _is_sqlite_lock_error(exc):
            logger.error(
                "[db.commit.locked] label=%s retries=%d elapsed=%.3fs; "
                "caller must rebuild the short transaction",
                label, retries, time.monotonic() - started,
            )
        raise


async def run_write_transaction(
    operation: Callable[[AsyncSession], Awaitable[T]], *,
    label: str = "db",
    retries: int = 3,
) -> T:
    """执行可重放的短写事务，锁失败时重建 Session 和 ORM 状态再重试。

    operation 必须只包含内存组装、显式数据库读写和必要的 flush，禁止包含
    LLM/文件/网络长 IO。这样 rollback 后不会复用已经失效的 ORM 对象。
    """
    from app.persistence.database import async_session_factory

    attempts = max(1, int(retries))
    for attempt in range(attempts):
        started = time.monotonic()
        async with async_session_factory() as db:
            try:
                result = await operation(db)
                await db.flush()
                await db_commit(db)
                if attempt:
                    logger.info("[db.write.retry] label=%s attempts=%d elapsed=%.3fs",
                                label, attempt + 1, time.monotonic() - started)
                return result
            except Exception as exc:
                await rollback_safely(db)
                if not _is_sqlite_lock_error(exc) or attempt >= attempts - 1:
                    raise
                delay = 0.05 * (2 ** attempt)
                logger.warning("[db.write.retry] label=%s attempt=%d delay=%.2fs",
                               label, attempt + 1, delay)
        await asyncio.sleep(delay)
    raise RuntimeError(f"数据库写事务失败: {label}")  # pragma: no cover


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

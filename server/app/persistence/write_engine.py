"""WriteEngine：无锁同步写通道（对齐 deepseek-harness 单写者架构，plan-206-975）。

设计要点：
- **单连接 + 单 worker 线程**：所有写事务在**单个专用线程**中同步执行（FIFO 串行），
  天然互斥、**无需进程内锁**；事件循环在 `await run_write` 期间保持自由（不阻塞
  其它协程——当前 run_sync 的同步 DB IO 会占住事件循环，此为真实改进）。
- **与异步读引擎共库**：同一 SQLite 文件（WAL：读并发保留、写单写者）。同步写
  连接 1 个，异步读连接池多个——读写分离。
- **短事务契约**（对齐 harness）：`operation(sync_session)` 内部 get/add/改属性/
  flush/commit 全部**同步**；纯 DB 操作、禁止 LLM/文件/网络 IO；**返回标量**
  （如新建对象 id），不与 async 侧 ORM 对象混用（SqlAlchemy ORM 对象跨
  async/sync 会话不共享）。持连接时长 = 该事务耗时（毫秒级）。
- 锁冲突（仅剩跨进程/旧实例兜底）按 retries 退避重试；其余异常上抛（保序由
  调用方/GlobalWriteQueue 处理）。
"""
import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

_engine: Engine | None = None
_executor: ThreadPoolExecutor | None = None
_url_override: str | None = None
_foreign_keys: bool = True
_init_lock = threading.Lock()  # 仅用于懒初始化，非写锁


def _resolved_url() -> str:
    if _url_override:
        url = _url_override
    else:
        # 与异步读引擎严格同源（同一库文件，测试/生产一致）
        from app.persistence.database import engine as _async_engine
        url = str(_async_engine.url)
    # 同步 driver：sqlite+aiosqlite → sqlite+pysqlite
    if url.startswith("sqlite+aiosqlite"):
        url = url.replace("+aiosqlite", "+pysqlite", 1)
    return url


def _ensure_engine() -> Engine:
    global _engine
    if _engine is None:
        with _init_lock:
            if _engine is None:
                url = _resolved_url()
                eng = create_engine(
                    url,
                    connect_args={"check_same_thread": False, "timeout": 10},
                )
                if url.startswith("sqlite"):
                    fk = _foreign_keys
                    @event.listens_for(eng, "connect")
                    def _set_pragma(dbapi_conn, _rec):
                        cur = dbapi_conn.cursor()
                        # WAL 是库文件持久属性（异步读引擎首连已设置），此处不再重设
                        # journal_mode——它要求独占访问，可能被其它活动连接锁住。
                        cur.execute("PRAGMA busy_timeout=10000")
                        cur.execute("PRAGMA synchronous=NORMAL")
                        if fk:
                            cur.execute("PRAGMA foreign_keys=ON")
                        cur.close()
                _engine = eng
    return _engine


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _init_lock:
            if _executor is None:
                # 单 worker：全部写事务严格 FIFO 串行（唯一物理写者，无锁）
                _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="write")
    return _executor


def configure(url: str | None = None, foreign_keys: bool | None = None) -> None:
    """显式设置写引擎 URL/外键开关（测试/多实例）。调用后会重建引擎。"""
    global _engine, _url_override, _foreign_keys
    _url_override = url
    if foreign_keys is not None:
        _foreign_keys = foreign_keys
    _engine = None


def _is_sqlite_lock_error(exc: BaseException) -> bool:
    from sqlalchemy.exc import OperationalError
    if isinstance(exc, OperationalError):
        msg = str(getattr(exc, "orig", None) or exc).lower()
        return "database is locked" in msg or "database table is locked" in msg
    return False


async def run_write(operation: Callable[[Session], T], *, label: str = "db",
                    retries: int = 2) -> T:
    """把同步事务函数提交到单写线程执行（FIFO 串行，无锁）。

    - `operation(sync_session)`：纯 DB、无 IO；内部自行 commit；返回标量；
    - await 期间事件循环自由（写事务在 worker 线程，不阻塞其它协程）；
    - 锁冲突（跨进程兜底）按 retries 退避重试。
    """
    eng = _ensure_engine()
    ex = _ensure_executor()
    loop = asyncio.get_running_loop()
    for attempt in range(max(1, int(retries))):
        delay = 0.0
        t_wait = time.monotonic()
        try:
            result = await loop.run_in_executor(ex, _run_tx, eng, operation, label)
            if attempt:
                logger.info("[write_engine] label=%s retry_ok attempts=%d", label, attempt + 1)
            return result
        except Exception as exc:
            if not _is_sqlite_lock_error(exc) or attempt >= max(1, int(retries)) - 1:
                raise
            delay = 0.05 * (2 ** attempt)
            logger.warning("[write_engine] label=%s lock_error=%s delay=%.2fs",
                           label, type(exc).__name__, delay)
        finally:
            waited = time.monotonic() - t_wait
            if waited >= 0.05:
                logger.info("[write_engine] wait=%.3fs label=%s", waited, label)
        if delay:
            await asyncio.sleep(delay)
    raise RuntimeError(f"写引擎事务失败: {label}")  # pragma: no cover


def _run_tx(engine: Engine, operation: Callable[[Session], T], label: str) -> T:
    """在单写线程内同步执行一个事务（worker 线程内运行，天然不与它写交错）。"""
    started = time.monotonic()
    session = Session(engine, autoflush=False, expire_on_commit=False)
    try:
        result = operation(session)
        # 契约：operation 内自行 commit；若未 commit 则回滚并告警（防数据半途丢失）
        if session.new or session.dirty or session.deleted:
            logger.warning("[write_engine] label=%s operation 未 commit 就返回，回滚以保证原子性", label)
            session.rollback()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        hold = time.monotonic() - started
        session.close()
        if hold >= 0.2:
            logger.warning("[write_engine] tx_hold=%.3fs label=%s", hold, label)

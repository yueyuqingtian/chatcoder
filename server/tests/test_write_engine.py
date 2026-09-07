"""WriteEngine（plan-206-975 无锁写通道）单元测试。

覆盖：事务函数跑通（含返回标量）、FIFO 串行（并发提交全部成功、顺序一致）、
未 commit 回滚兜底、锁冲突重试不命中（正常路径无重试）。
"""
import asyncio

import pytest

import app.persistence.write_engine as we
from app.persistence.write_engine import configure, run_write


@pytest.fixture
def write_db(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path}/wengine.db"
    configure(url)
    eng = we._ensure_engine()
    from app.persistence.database import Base  # 复用统一 metadata
    from app.persistence import models  # noqa: F401  # 触发模型注册
    Base.metadata.create_all(eng)
    yield url
    configure(None)
    we._engine = None
    we._executor = None


@pytest.mark.asyncio
async def test_run_write_insert_returns_scalar(write_db):
    from app.persistence.models.message import Session as SessionModel

    def op(s):
        row = SessionModel(title="hello", status="active")
        s.add(row)
        s.flush()
        sid = row.id
        s.commit()
        return sid

    sid = await run_write(op, label="test.insert")
    assert sid is not None and sid > 0
    # 读验证（独立读：仍走 async 读？此处用同步连接可读；用 async engine 亦可，简化用 write 连接读）
    def read(s):
        return s.get(SessionModel, sid)
    row = await run_write(read, label="test.read")
    assert row is not None and row.title == "hello"


@pytest.mark.asyncio
async def test_run_write_fifo_serial_no_conflict(write_db):
    """并发提交 N 个事务函数 → 全部成功、严格 FIFO（单写线程串行，无锁/无冲突）。"""
    from app.persistence.models.message import Session as SessionModel

    order: list[int] = []
    barrier0 = asyncio.Event()

    async def submit(i: int):
        def op(s):
            # 每个事务函数在写线程串行执行，内部行为可测顺序
            row = SessionModel(title=f"s{i}", status="active")
            s.add(row)
            s.flush()
            s.commit()
            order.append(i)
            return row.id
        return await run_write(op, label=f"test.writer{i}")

    tasks = [asyncio.create_task(submit(i)) for i in range(8)]
    ids = await asyncio.gather(*tasks)
    assert len(ids) == 8 and all(i > 0 for i in ids)
    # 严格 FIFO：提交顺序即执行顺序（单写线程串行）
    assert order == list(range(8))


@pytest.mark.asyncio
async def test_run_write_uncommitted_rolls_back(write_db):
    """事务函数未 commit 即返回 → 回滚（原子性兜底）。"""
    from app.persistence.models.message import Session as SessionModel

    def op(s):
        s.add(SessionModel(title="uncommitted", status="active"))
        s.flush()
        return 1  # 未 commit

    await run_write(op, label="test.uncommitted")

    def count(s):
        return s.query(SessionModel).count()
    n = await run_write(count, label="test.count")
    assert n == 0


@pytest.mark.asyncio
async def test_run_write_retries_on_lock_error(write_db):
    """跨进程锁冲突（模拟 SQLITE_BUSY）时按重试退避，成功后返回结果。"""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy import text as _text

    from app.persistence.models.message import Session as SessionModel
    fail = {"n": 0}

    def op(s):
        if fail["n"] < 1:
            fail["n"] += 1
            raise OperationalError("stmt", {}, Exception("database is locked"))
        row = SessionModel(title="retry-ok", status="active")
        s.add(row)
        s.flush()
        s.commit()
        return row.id

    sid = await run_write(op, label="test.retry")
    assert sid is not None and fail["n"] == 1

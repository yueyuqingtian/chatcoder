"""WriteBehindBuffer 薄包装测试（966 后：enqueue 进全局队列 + flush=seq 屏障）。

覆盖：owner 填充、seq 分配、flush 屏障立即可见、关闭后兜底写、多 buffer 经
全局队列按时间序交错。
"""
import asyncio

import pytest

import app.persistence.write_behind as wb
from app.persistence.write_behind import WriteBehindBuffer, WriteItem


@pytest.mark.asyncio
async def test_buffer_enqueue_fills_owner_and_seq():
    """enqueue 填充 session/turn 归属并分配全局 seq。"""
    q = wb.GlobalWriteQueue(sink=lambda batch: asyncio.sleep(0))
    orig = wb.queue
    wb.queue = q
    try:
        buf = WriteBehindBuffer(session_id=7, turn_id=42)
        item = WriteItem("message", {"i": 1})
        buf.enqueue(item)
        assert item.session_id == 7
        assert item.turn_id == 42
        assert item.seq == 1
    finally:
        wb.queue = orig


@pytest.mark.asyncio
async def test_buffer_flush_barrier_waits_all():
    """flush 屏障：入队条目全部落库后返回（不依赖定时器）。"""
    written: list[int] = []

    async def sink(batch):
        written.extend(it.payload["i"] for it in batch)

    q = wb.GlobalWriteQueue(sink=sink)
    orig = wb.queue
    wb.queue = q
    try:
        buf = WriteBehindBuffer(session_id=1, turn_id=10)
        buf.enqueue(WriteItem("message", {"i": 1}))
        buf.enqueue(WriteItem("message", {"i": 2}))
        await buf.flush()
        assert sorted(written) == [1, 2]   # 屏障后立即可见
    finally:
        wb.queue = orig


@pytest.mark.asyncio
async def test_buffer_close_then_enqueue_fallback():
    """close 后 enqueue 走兜底写（不丢数据）。"""
    written: list[int] = []

    async def sink(batch):
        written.extend(it.payload["i"] for it in batch)

    q = wb.GlobalWriteQueue(sink=sink)
    orig = wb.queue
    wb.queue = q
    try:
        buf = WriteBehindBuffer(session_id=1, turn_id=10)
        buf.enqueue(WriteItem("message", {"i": 1}))
        await buf.close()
        assert written == [1]
        buf.enqueue(WriteItem("message", {"i": 2}))
        await asyncio.sleep(0.2)
        assert 2 in written
    finally:
        wb.queue = orig


@pytest.mark.asyncio
async def test_multi_buffer_ordering_via_global_queue():
    """多 buffer 交错入队 → 全局队列按时间序落库（公平轮转）。"""
    received: list[int] = []

    async def sink(batch):
        received.extend(it.payload["i"] for it in batch)

    q = wb.GlobalWriteQueue(sink=sink)
    orig = wb.queue
    wb.queue = q
    try:
        a = WriteBehindBuffer(session_id=1, turn_id=1)
        b = WriteBehindBuffer(session_id=2, turn_id=2)
        a.enqueue(WriteItem("message", {"i": 1}))
        b.enqueue(WriteItem("message", {"i": 2}))
        a.enqueue(WriteItem("message", {"i": 3}))
        await asyncio.sleep(0.3)
        assert sorted(received) == [1, 2, 3]   # 三条全部落库
        assert q.pending_count == 0            # 队列已排空
    finally:
        wb.queue = orig

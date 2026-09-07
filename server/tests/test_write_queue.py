"""GlobalWriteQueue（966 事件驱动懒加载批处理队列）单元测试（mock sink）。

覆盖：懒加载合并（同步密集入队→单批）、双限切批（条数/字节）、时间序保证、
失败保序重试、屏障等待、多会话交错按时间序、屏障超时。
"""
import asyncio

import pytest

from app.persistence.write_behind import GlobalWriteQueue, MAX_BATCH, WriteItem


def _item(i: int, session_id: int = 1, payload=None) -> WriteItem:
    return WriteItem("message", payload if payload is not None else {"i": i},
                     session_id=session_id, turn_id=10)


@pytest.mark.asyncio
async def test_lazy_merge_single_batch():
    """同步密集入队（无 await 间隙）→ 消费者在调度间隙一次取走全部，单批落库。"""
    batches: list[list[int]] = []

    async def sink(batch):
        batches.append([it.payload["i"] for it in batch])

    q = GlobalWriteQueue(sink=sink)
    for i in range(10):
        q.enqueue(_item(i))          # 同步连续入队（无 await）
    await asyncio.sleep(0.2)         # 等消费者被调度并完成
    assert len(batches) == 1
    assert batches[0] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]


@pytest.mark.asyncio
async def test_batch_limit_by_count():
    """超过 MAX_BATCH 条 → 按时间序分批（每批 ≤ MAX_BATCH）。"""
    batches: list[list[int]] = []

    async def sink(batch):
        batches.append([it.payload["i"] for it in batch])

    q = GlobalWriteQueue(sink=sink)
    total = MAX_BATCH + 50
    for i in range(total):
        q.enqueue(_item(i))
    await asyncio.sleep(0.6)
    flat = [x for b in batches for x in b]
    assert flat == list(range(total))          # 全部落库且时间序
    assert all(len(b) <= MAX_BATCH for b in batches)
    assert len(batches) >= 2                   # 确已分批


@pytest.mark.asyncio
async def test_batch_limit_by_bytes():
    """字节上限：大 payload 单条成批/超过上限即切批，依旧完整落库。"""
    batches: list[list[int]] = []

    async def sink(batch):
        batches.append([it.payload["i"] for it in batch])

    q = GlobalWriteQueue(sink=sink, max_batch_bytes=64 * 1024)
    big = "x" * (32 * 1024)
    q.enqueue(_item(1, payload={"i": 1, "blob": big}))
    q.enqueue(_item(2, payload={"i": 2, "blob": big}))
    q.enqueue(_item(3, payload={"i": 3, "blob": big}))
    await asyncio.sleep(0.4)
    flat = [x for b in batches for x in b]
    assert flat == [1, 2, 3]
    # 单条 ~32KB → 每批 1-3 条（约 2 条即达 64KB 上限），且确已切批
    assert all(1 <= len(b) <= 3 for b in batches)
    assert len(batches) >= 2


@pytest.mark.asyncio
async def test_ordering_preserved_across_sessions():
    """多会话混批：批内/跨批顺序 = 全局入队顺序（CPU 轮转式公平）。"""
    received: list[int] = []

    async def sink(batch):
        received.extend(it.seq for it in batch)  # seq 与入队一致

    q = GlobalWriteQueue(sink=sink)
    seqs = []
    for i in range(14):
        si = 1 if i % 2 == 0 else 2
        seqs.append(q.enqueue(_item(i, session_id=si)))
    await asyncio.sleep(0.4)
    assert received == seqs


@pytest.mark.asyncio
async def test_failure_requeue_preserves_order():
    """批失败 → 整批回流队首重试，最终顺序与入队一致。"""
    received: list[int] = []
    fail_once = {"n": 0}

    async def sink(batch):
        if fail_once["n"] == 0:
            fail_once["n"] += 1
            raise RuntimeError("boom")
        received.extend(it.payload["i"] for it in batch)

    q = GlobalWriteQueue(sink=sink)
    for i in range(5):
        q.enqueue(_item(i))
    await asyncio.sleep(0.6)
    assert received == [0, 1, 2, 3, 4]     # 保序（失败批次重试后顺序不变）


@pytest.mark.asyncio
async def test_barrier_waits_until_done():
    """屏障：flush/wait_until 等待该 turn 已入队条目全部落库后返回。"""
    release = asyncio.Event()
    written: list[int] = []

    async def sink(batch):
        await release.wait()
        written.extend(it.payload["i"] for it in batch)

    q = GlobalWriteQueue(sink=sink)
    q.enqueue(_item(1))
    q.enqueue(_item(2))
    waiter = asyncio.create_task(q.wait_until(q.last_seq()))
    await asyncio.sleep(0.1)
    assert not waiter.done()             # sink 挂住 → 屏障未返回
    release.set()
    await asyncio.wait_for(waiter, timeout=2.0)
    assert sorted(written) == [1, 2]


@pytest.mark.asyncio
async def test_wait_until_timeout():
    """屏障超时：sink 永不完成 → wait_until 抛 asyncio.TimeoutError。"""
    never = asyncio.Event()

    async def sink(batch):
        await never.wait()

    q = GlobalWriteQueue(sink=sink)
    q.enqueue(_item(1))
    with pytest.raises(asyncio.TimeoutError):
        await q.wait_until(q.last_seq(), timeout=0.3)

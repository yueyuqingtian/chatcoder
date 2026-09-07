"""全局有序写队列（966：事件驱动懒加载批处理，deepseek-harness coordinator 对等）。

设计要点（对应 ai/chatcoder-plan-206-965/966）：
- **单消费者 + 全局 FIFO**：所有 turn/会话的热路径写（消息/任务态/用量/写盘记录）
  统一 enqueue 到 `GlobalWriteQueue`，消费者按**入队时间序**批量落库——同一时刻
  只有一个写者，多会话写"轮转"（CPU 调度类比：单核 + 多线程时间片）。
- **懒加载批处理（无定时器）**：`enqueue` 是同步的（无 await、无 IO、即时返回）；
  消费者任务在事件循环调度间隙醒来，把当前队列中的全部条目（≤ MAX_BATCH 条且
  ≤ MAX_BATCH_BYTES 字节）一次取走，合并为**一个原子事务**落库（BEGIN → 批量
  写 → COMMIT），随后按入队顺序广播。生产者同步密集入队期间消费者不会被调度，
  天然攒批；条目少时单条也立即执行（低延迟）——"来活就干，来了多少干多少"。
- **seq 屏障**：`barrier(session_id, turn_id)` 等待"该 turn 已入队条目全部落库"
  （turn 收尾/计划确认/取消前强制排空），替代 per-turn 定时器 + flush 状态机——
  消除了此前 timer/deadline/paused 状态机引发"入队滞留"类 bug 的可能性。
- **失败保序**：批失败 → 整批回流队首（时间序不变）+ 退避重试；连续失败暂停
  （指数退避，上限 60s），ERROR 日志留痕；屏障等待方感知失败/超时。
- 落库经 `database.write_tx` 同步闭包（run_sync）——事务内无异步 IO（架构保证）。

缓冲项只携带标量（payload dict），不持有 ORM 对象——跨 session 安全、无过期/
MissingGreenlet 风险。
"""
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# 每事务最大条数 / 最大内容字节（双限切批：任一先到即切，收紧持锁上界）
MAX_BATCH = 100
MAX_BATCH_BYTES = 2 * 1024 * 1024
# 屏障默认超时（秒）：超时抛 asyncio.TimeoutError，调用方按失败处理
BARRIER_TIMEOUT = 15.0


@dataclass
class WriteItem:
    """一条待落库的写事务项。payload 仅含标量字段。"""

    kind: str  # message / task_update / turn_patch / usage / session_usage / rollback_write / rollback_ckpt
    payload: dict = field(default_factory=dict)
    # v966: 归属（由 WriteBehindBuffer.enqueue 填充）与全局队列序号
    session_id: int = 0
    turn_id: int = 0
    seq: int = 0


class GlobalWriteQueue:
    """全局单消费者写队列（事件驱动懒加载批处理）。

    `sink`：批落库回调（默认 `_flush_batch`，测试可注入 mock）。
    """

    def __init__(self, *, max_batch: int = MAX_BATCH, max_batch_bytes: int = MAX_BATCH_BYTES,
                 sink: Callable[[list[WriteItem]], Awaitable[None]] | None = None):
        self._queue: deque[WriteItem] = deque()
        self._seq = 0
        self._done_seq = 0
        self._wake = asyncio.Event()
        self._cond = asyncio.Condition()
        self._consumer: asyncio.Task | None = None
        self._retry_interval = 0.05
        self._max_batch = max(1, max_batch)
        self._max_batch_bytes = max(1, max_batch_bytes)
        self._sink = sink or _flush_batch

    # ── 生产侧 ────────────────────────────────────────────────

    def enqueue(self, item: WriteItem) -> int:
        """同步入队（无 await、无 IO、即时返回），返回全局序号。"""
        self._seq += 1
        item.seq = self._seq
        self._queue.append(item)
        try:
            if self._consumer is None or self._consumer.done():
                self._consumer = asyncio.get_running_loop().create_task(self._consume_loop())
            self._wake.set()  # 首次创建也要唤醒（否则单条入队后消费者永不运行）
        except RuntimeError:
            # 事件循环已关闭（shutdown 后兜底入队）：直接落库
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._write_batch([item]))
            except Exception:
                logger.error("[write_queue] enqueue fallback failed kind=%s", item.kind, exc_info=True)
        return self._seq

    @property
    def pending_count(self) -> int:
        return len(self._queue)

    @property
    def done_seq(self) -> int:
        return self._done_seq

    def last_seq(self) -> int:
        return self._seq

    # ── 屏障 ──────────────────────────────────────────────────

    async def wait_until(self, seq: int, timeout: float = BARRIER_TIMEOUT) -> None:
        """等待序号 <= seq 的条目全部落库（屏障）。超时抛 asyncio.TimeoutError。"""
        if seq <= 0 or self._done_seq >= seq:
            return
        async with self._cond:
            if self._done_seq >= seq:
                return
            await asyncio.wait_for(
                self._cond.wait_for(lambda: self._done_seq >= seq), timeout,
            )

    # ── 消费侧 ────────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        """单消费者懒加载循环：被唤醒时取当前队列全部（双限切批）批量落库。"""
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self._drain()

    async def _drain(self) -> None:
        while self._queue:
            batch = self._take_batch()
            if not batch:
                break
            t0 = time.monotonic()
            done = False
            try:
                await self._write_batch(batch)
                done = True
            except Exception:
                # 失败保序：整批回流队首，退避后重试（连续失败指数退避上限 60s）
                for item in reversed(batch):
                    self._queue.appendleft(item)
                logger.error(
                    "[write_queue] batch failed (%d items, seq<=%d); retry in %.2fs",
                    len(batch), batch[-1].seq, self._retry_interval,
                    exc_info=True,
                )
                self._retry_interval = min(self._retry_interval * 2, 60.0)
                await asyncio.sleep(self._retry_interval)
                continue
            if done:
                self._retry_interval = 0.05
            elapsed = time.monotonic() - t0
            logger.info(
                "[write_queue] batch_size=%d depth=%d elapsed=%.3fs done_seq=%d",
                len(batch), len(self._queue), elapsed, self._done_seq,
            )

    def _take_batch(self) -> list[WriteItem]:
        """按时间序切批：条数与内容字节双限（任一先到即切）。"""
        batch: list[WriteItem] = []
        total_bytes = 0
        while self._queue and len(batch) < self._max_batch:
            nxt = self._queue[0]
            size = _item_bytes(nxt)
            if batch and total_bytes + size > self._max_batch_bytes:
                break
            batch.append(self._queue.popleft())
            total_bytes += size
        return batch

    async def _write_batch(self, batch: list[WriteItem]) -> None:
        """单事务落库 + 按序广播；推进 done_seq 并唤醒屏障等待者。"""
        if not batch:
            return
        await self._sink(batch)
        async with self._cond:
            self._done_seq = max(self._done_seq, batch[-1].seq)
            self._cond.notify_all()

    async def shutdown(self) -> None:
        """关停排空：等待消费者结束当前工作并清空队列。"""
        if self._consumer is not None and not self._consumer.done():
            self._wake.set()
            try:
                await asyncio.wait_for(self._consumer, timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("[write_queue] shutdown 等待消费者超时，强制结束")
                self._consumer.cancel()
        while self._queue:
            batch = self._take_batch()
            if not batch:
                break
            await self._write_batch(batch)


def _item_bytes(item: WriteItem) -> int:
    """估算单条的数据体量（content 序列化后大小），用于对字节数切批。"""
    try:
        import json
        return len(json.dumps(item.payload, ensure_ascii=False))
    except Exception:
        return 1024


class WriteBehindBuffer:
    """薄包装（966）：仅记录该 turn 最后入队序号，flush 转为全局队列屏障。

    原 per-turn 定时器/flusher/deadline 状态机已删除——入队即入全局队列，
    由全局单消费者按时间序批量落库（天然合并 + 无滞留状态机）。
    """

    def __init__(self, *, session_id: int, turn_id: int):
        self.session_id = int(session_id)
        self.turn_id = int(turn_id)
        self._last_seq = 0
        self._closed = False
        self._loop = asyncio.get_running_loop()

    def enqueue(self, item: WriteItem) -> None:
        """入队：填充归属后同步进全局队列（无锁、无 IO、即时返回）。"""
        if self._closed:
            # buffer 已关闭：重新入全局队列兜底落库（不丢数据，消费路径统一）
            queue.enqueue(item)
            return
        item.session_id = self.session_id
        item.turn_id = self.turn_id
        self._last_seq = queue.enqueue(item)

    async def flush(self) -> None:
        """屏障：等待本 turn 已入队条目全部落库（幂等；超时抛 TimeoutError）。"""
        if self._closed:
            return
        if self._last_seq > 0:
            await queue.wait_until(self._last_seq)

    async def close(self) -> None:
        """关闭并排空本 turn 剩余缓冲。"""
        await self.flush()
        self._closed = True


class WriteBehindManager:
    """按 (session_id, turn_id) 维护活跃写缓冲的注册表（薄包装管理）。"""

    def __init__(self) -> None:
        self._buckets: dict[tuple[int, int], WriteBehindBuffer] = {}

    @property
    def buckets(self) -> dict[tuple[int, int], WriteBehindBuffer]:
        return self._buckets

    def get(self, session_id: int, turn_id: int) -> WriteBehindBuffer:
        key = (int(session_id), int(turn_id))
        buf = self._buckets.get(key)
        if buf is None or buf._closed:
            buf = WriteBehindBuffer(session_id=key[0], turn_id=key[1])
            self._buckets[key] = buf
        return buf

    async def detach(self, session_id: int, turn_id: int) -> None:
        """turn 结束：排空并从注册表移除。"""
        key = (int(session_id), int(turn_id))
        buf = self._buckets.pop(key, None)
        if buf is not None:
            await buf.close()

    async def flush_all(self) -> None:
        for buf in list(self._buckets.values()):
            if not buf._closed:
                await buf.flush()

    async def shutdown(self) -> None:
        """应用关停：排空所有缓冲与全局队列。"""
        await self.flush_all()
        for buf in list(self._buckets.values()):
            await buf.close()
        self._buckets.clear()
        await queue.shutdown()


# ── 默认 flusher：单事务落库 + 广播 ──────────────────────────

async def _flush_batch(batch: list[WriteItem]) -> None:
    """把一批 WriteItem 以单事务落库，并按入队顺序广播。

    - 落库：`database.write_tx` + 同步闭包（run_sync）——事务内无异步 IO（架构保证）；
    - 广播：在 commit 后才发出（id/created_at 已就绪，前端去重/乐观替换/清流式语义不变）。
    """
    from functools import partial

    from app.persistence.database import write_tx

    if not batch:
        return
    label = f"wb.turn.{batch[0].turn_id}" if batch[0].turn_id else "wb"
    events = await write_tx(partial(_persist_batch, batch), label=label)
    for ev in events or ():
        sid = ev.pop("__session_id", None)
        if sid is None:
            continue
        try:
            from app.orchestration.agent_events import broadcast
            await broadcast(sid, ev)
        except Exception:
            logger.debug("write-behind broadcast failed", exc_info=True)


def _persist_batch(batch: list[WriteItem], s) -> list[dict]:
    """同步闭包（run_sync 内）：单事务落库 batch，返回待广播事件列表。

    966 优化：所有 add/get+改 先执行，**循环结束后统一一次 flush**（消除每条
    消息一次往返），再构造广播（flush 后 id/created_at 就绪），最后 commit。
    """
    from app.gateway.schemas import MessageOut

    # 延迟导入，避免顶层循环依赖 message_service / 模型。
    from app.persistence.models.message import Message
    from app.persistence.models.task import Task
    from app.persistence.models.turn import Turn
    from app.persistence.models.usage_record import UsageRecord
    from app.services.message_service import enrich_content_abs_path

    events: list[dict] = []
    pending_events: list[tuple[int, dict]] = []  # (batch 序号, 广播事件) 按入队序输出
    created: list[tuple[int, dict]] = []  # (batch 序号, {msg, content}) 待 flush 后构造广播
    for idx, item in enumerate(batch):
        kind = item.kind
        p = item.payload
        sid = item.session_id or int(p.get("session_id") or 0)
        tid = item.turn_id or int(p.get("turn_id") or 0)
        if kind == "message":
            content = p.get("content") or {}
            msg = Message(
                session_id=sid, turn_id=tid,
                thread_id=p.get("thread_id"), sender_type=p.get("sender_type", "agent"),
                sender_id=p.get("sender_id"), msg_type=p.get("msg_type", "text"),
                content=content, token_usage=int(p.get("token_usage") or 0),
            )
            s.add(msg)
            created.append((idx, {"sid": sid, "msg": msg, "content": content}))
        elif kind == "task_update":
            task = s.get(Task, int(p["task_id"]))
            if task is not None:
                task.status = p.get("status", task.status)
                if p.get("note") is not None:
                    task.note = p["note"]
            pending_events.append((idx, {"__session_id": sid,
                                          "event": "task.updated",
                                          "payload": {"task_id": int(p["task_id"]),
                                                      "status": p.get("status"), "note": p.get("note")}}))
        elif kind == "turn_patch":
            t = s.get(Turn, int(p["turn_id"]))
            if t is not None:
                for k, v in p.items():
                    if k in ("status", "summary", "completed_at", "plan_doc_path",
                             "plan_status", "token_usage"):
                        setattr(t, k, v)
        elif kind == "usage":
            s.add(UsageRecord(
                session_id=sid, turn_id=tid,
                agent_id=p.get("agent_id"), model_id=p.get("model_id"),
                model_name=p.get("model_name", ""), provider_name=p.get("provider_name", ""),
                prompt_tokens=int(p.get("prompt_tokens") or 0),
                completion_tokens=int(p.get("completion_tokens") or 0),
                reasoning_tokens=int(p.get("reasoning_tokens") or 0),
                cached_tokens=int(p.get("cached_tokens") or 0),
                usage_source=p.get("usage_source", "api"),
            ))
        elif kind == "session_usage":
            from app.persistence.models.message import Session as SessionModel
            row = s.get(SessionModel, int(p["session_id"]))
            if row is not None:
                row.last_prompt_tokens = int(p.get("last_prompt_tokens") or 0)
                row.last_usage_at = p.get("last_usage_at")
        elif kind == "rollback_write":
            from app.persistence.models.rollback import RollbackWrite
            s.add(RollbackWrite(
                session_id=sid, turn_id=tid,
                tool=p.get("tool", ""), path=p.get("path", ""),
                old_content=p.get("before"), new_content=p.get("after"),
                binary=bool(p.get("binary")),
            ))
        elif kind == "rollback_ckpt":
            # checkpoint 登记：查 turn 快照并追加 file_list/new_files（同 turn 同文件去重）。
            from sqlalchemy import select as _select
            from app.persistence.models.rollback import TurnSnapshot
            snap = s.execute(
                _select(TurnSnapshot).where(TurnSnapshot.turn_id == int(p["turn_id"]))
            ).scalars().first()
            if snap is None:
                continue
            new_file = p.get("new_file")
            if new_file:
                files = list(snap.new_files or [])
                if new_file not in files:
                    files.append(new_file)
                    snap.new_files = files
            else:
                entry = {"ckpt": p.get("checkpoint_path"), "path": p.get("rel_path")}
                items = list(snap.file_list or [])
                if any(isinstance(it, dict) and it.get("path") == p.get("rel_path") for it in items):
                    continue  # 该文件本 turn 已备份，无需重复
                items.append(entry)
                snap.file_list = items
    if created:
        s.flush()  # 统一一次 flush（所有消息 id/created_at 就绪）
        for idx, c in created:
            m = c["msg"]
            out = MessageOut(
                id=m.id, session_id=m.session_id, turn_id=m.turn_id,
                thread_id=m.thread_id, sender_type=m.sender_type,
                sender_id=m.sender_id, msg_type=m.msg_type,
                content=enrich_content_abs_path(c["content"]), token_usage=m.token_usage,
                created_at=str(m.created_at) if m.created_at else None,
            )
            pending_events.append((idx, {"__session_id": c["sid"],
                                         "event": "message.created", "payload": {"msg": out.model_dump()}}))
    elif batch:
        s.flush()  # 其余类型也统一提交（保持单事务不变）
    s.commit()
    events = [ev for _, ev in sorted(pending_events, key=lambda x: x[0])]
    return events


# 全局单例：业务侧通过 write_behind.get(session_id, turn_id).enqueue(...) 写入。
queue = GlobalWriteQueue()
write_behind = WriteBehindManager()

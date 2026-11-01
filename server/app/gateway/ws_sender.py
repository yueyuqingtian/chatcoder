"""plan-20261101-performance：每连接一个有界发送者，慢客户端不能占住写入与广播链路。"""
import asyncio
import json
import time
from collections import deque
from typing import Callable


class SocketSender:
    def __init__(self, ws, on_dead: Callable[[], None], *, max_items: int = 256,
                 max_bytes: int = 8 * 1024 * 1024):
        self.ws = ws
        self.on_dead = on_dead
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.queue: deque[tuple[str, int, float, dict | None]] = deque()
        self.bytes = 0
        self.sent = 0
        self.send_ms = 0.0
        self.closed = False
        self.wake = asyncio.Event()
        self.space = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    def put(self, raw: str, event: dict | None = None) -> bool:
        if self.closed:
            return False
        size = len(raw.encode("utf-8"))
        # 只合并连续同源增量；任何落库、终态或其它事件天然形成屏障。
        if event and event.get("event") in ("token.delta", "thinking.delta") and self.queue:
            old_raw, old_size, queued_at, old = self.queue[-1]
            payload = event.get("payload") or {}
            previous = (old or {}).get("payload") or {}
            if old and old.get("event") == event.get("event") and old_size + size < 16384 \
                    and isinstance(payload.get("delta"), str) and isinstance(previous.get("delta"), str) \
                    and {k: v for k, v in payload.items() if k != "delta"} == {k: v for k, v in previous.items() if k != "delta"}:
                combined = {**event, "payload": {**payload, "delta": previous["delta"] + payload["delta"]}}
                encoded = json.dumps(combined, ensure_ascii=False)
                combined_size = len(encoded.encode("utf-8"))
                if self.bytes - old_size + combined_size <= self.max_bytes:
                    self.queue[-1] = (encoded, combined_size, queued_at, combined)
                    self.bytes += combined_size - old_size
                    return True
        # 单条完整正文允许超过队列预算，不能因长消息永久陷入断开重连。
        if len(self.queue) >= self.max_items or (self.queue and self.bytes + size > self.max_bytes):
            self.close()
            self.on_dead()
            asyncio.create_task(self._close_socket())
            return False
        meta = {**event, "payload": dict(event.get("payload") or {})} if event else None
        self.queue.append((raw, size, time.monotonic(), meta))
        self.bytes += size
        self.wake.set()
        return True

    async def put_replay(self, raw: str) -> None:
        # 重放本身可多于队列容量，只让这条连接等待空位，不阻塞广播或写消费者。
        size = len(raw.encode("utf-8"))
        while not self.closed and self.queue and (len(self.queue) >= self.max_items or self.bytes + size > self.max_bytes):
            self.space.clear()
            await asyncio.wait_for(self.space.wait(), 6.0)
        if self.closed:
            raise ConnectionError("连接已关闭")
        self.put(raw)

    async def _close_socket(self):
        try:
            await asyncio.wait_for(self.ws.close(code=1013, reason="发送积压，请重新同步"), 1.0)
        except Exception:
            pass

    async def _run(self):
        try:
            while True:
                await self.wake.wait()
                while self.queue:
                    raw, size, _, _ = self.queue.popleft()
                    self.bytes -= size
                    self.space.set()
                    started = time.monotonic()
                    await asyncio.wait_for(self.ws.send_text(raw), timeout=5.0)
                    self.sent += 1
                    self.send_ms = (time.monotonic() - started) * 1000
                self.wake.clear()
        except asyncio.CancelledError:
            pass
        except Exception:
            self.closed = True
            self.on_dead()
            await self._close_socket()
        finally:
            self.queue.clear()
            self.bytes = 0
            self.space.set()

    def close(self):
        self.closed = True
        self.space.set()
        self.task.cancel()
        self.queue.clear()
        self.bytes = 0

    def snapshot(self):
        return {"queued": len(self.queue), "bytes": self.bytes, "sent": self.sent,
                "last_send_ms": round(self.send_ms, 2),
                "oldest_ms": round((time.monotonic() - self.queue[0][2]) * 1000, 2) if self.queue else 0}

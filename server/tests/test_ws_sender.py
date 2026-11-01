"""plan-20261101-performance：慢连接隔离、顺序与有界积压。"""
import asyncio
import json
import pytest
from app.gateway.ws import ConnectionManager
from app.gateway.ws_sender import SocketSender


class Socket:
    def __init__(self, blocked=False):
        self.gate = asyncio.Event()
        if not blocked:
            self.gate.set()
        self.frames = []
        self.arrived = asyncio.Event()
        self.closed = False

    async def accept(self):
        pass

    async def send_text(self, raw):
        await self.gate.wait()
        self.frames.append(json.loads(raw))
        self.arrived.set()

    async def close(self, **kwargs):
        self.closed = True


@pytest.mark.asyncio
async def test_slow_socket_does_not_delay_healthy_socket_and_order():
    manager = ConnectionManager()
    slow, fast = Socket(True), Socket()
    await manager.connect(1, slow)
    await manager.connect(1, fast)
    for i in range(10):
        await manager.broadcast(1, {"event": "message.created", "payload": {"msg": {"id": i}}})
    async def all_sent():
        while len(fast.frames) < 10:
            await asyncio.sleep(0)
    await asyncio.wait_for(all_sent(), 0.5)
    assert [f["seq"] for f in fast.frames] == list(range(1, 11))
    assert slow.frames == [] and len(manager.replay_since(1, 0)) == 10
    manager.disconnect(1, fast)
    manager.disconnect(1, slow)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_queue_overflow_disconnects_instead_of_silently_dropping_reliable_events():
    socket = Socket(True)
    dead = []
    sender = SocketSender(socket, lambda: dead.append(True), max_items=2)
    assert sender.put('{"event":"first"}')
    assert sender.put('{"event":"second"}')
    assert not sender.put('{"event":"third"}')
    assert sender.closed and dead and sender.bytes == 0
    await asyncio.sleep(0)
    await asyncio.sleep(0)


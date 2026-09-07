"""WS 断线补偿缓冲测试（v967）。

验证：高频流式增量事件（token.delta/thinking.delta）不入补偿缓冲、不占 seq；
关键事件（message.created/turn.* 等）正常入缓冲并分配单调 seq，可被 replay_since 补发。
"""
import pytest

from app.gateway.ws import ConnectionManager, _STREAM_NO_BUFFER_EVENTS


@pytest.mark.asyncio
async def test_delta_events_not_buffered_and_no_seq():
    """token/thinking delta 不入缓冲、不占 seq。"""
    m = ConnectionManager()
    for name in ("token.delta", "thinking.delta"):
        ev = {"event": name, "payload": {"delta": "x", "agent_id": 1}}
        await m.broadcast(1, ev)
        assert "seq" not in ev, f"{name} 不应占用 seq"
    assert m.replay_since(1, 0) == []


@pytest.mark.asyncio
async def test_key_events_buffered_with_monotonic_seq():
    """关键事件入缓冲并分配严格递增 seq。"""
    m = ConnectionManager()
    await m.broadcast(1, {"event": "message.created", "payload": {"msg": {}}})
    await m.broadcast(1, {"event": "turn.updated", "payload": {}})
    replay = m.replay_since(1, 0)
    assert len(replay) == 2
    assert all("seq" in ev for ev in replay)
    assert replay[0]["seq"] < replay[1]["seq"]


@pytest.mark.asyncio
async def test_replay_since_skips_delta_and_filters_by_last_seq():
    """replay_since 只返回 seq>last_seq 的关键事件，跳过 delta。"""
    m = ConnectionManager()
    await m.broadcast(1, {"event": "message.created", "payload": {"msg": {}}})   # seq=1
    await m.broadcast(1, {"event": "token.delta", "payload": {"delta": "x", "agent_id": 1}})  # 不入
    await m.broadcast(1, {"event": "turn.completed", "payload": {}})             # seq=2
    replay = m.replay_since(1, 1)
    assert len(replay) == 1
    assert replay[0]["event"] == "turn.completed"
    assert replay[0]["seq"] == 2


@pytest.mark.asyncio
async def test_buffer_capacity_bounded():
    """关键事件缓冲仍受 500 条上限约束（环形），delta 挤占问题已消除。"""
    m = ConnectionManager()
    for i in range(600):
        await m.broadcast(1, {"event": "message.created", "payload": {"msg": {"id": i}}})
    assert len(m._buffers[1]) == 500  # maxlen 环形

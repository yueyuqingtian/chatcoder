"""整轮（基底轮 + 后台续跑轮）变更聚合回归测试（plan-20261026-round-changes）。

背景：任务汇报后仍有后台命令在跑，命令结束会唤醒会话、创建新 turn 继续运行。
前端把这类续跑轮并入上一轮展示（`entry.turnIds`），但变更卡原先按轮各渲染一张，
同一轮出现两张卡片。修复后清单 / diff / 撤销都按整组聚合，且聚合范围**只包含**
「这一轮 + 这一轮结束后台事件唤醒的续跑轮」，不能把用户另起的轮次并进来。

本文件锁定：
1. `round_turn_ids` 去重升序，首项为基底轮；
2. `list_turn_changes` 跨轮取「整组首条写前 → 整组末条写后」，不是两轮统计相加；
3. `get_file_diff` 同样跨轮累积；
4. `revert_turn_files` 一次撤销整组（写盘记录与快照登记一并清理）；
5. 路由层 `_round_extra_turn_ids` 只接受同会话、更晚、带系统续跑标记的轮次。
"""
from __future__ import annotations

import difflib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.message import Message, Session
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
from app.persistence.models.turn import Turn
from app.services import rollback_service as rs

PATH = "src/a.py"


@pytest.fixture
async def db(monkeypatch, tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rb_round.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    async with factory() as s:
        yield s
    await engine.dispose()
    _we.configure(None)


def _counts(before: str | None, after: str | None) -> tuple[int, int]:
    b = (before or "").splitlines()
    a = (after or "").splitlines()
    additions = deletions = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a).get_opcodes():
        if tag in ("insert", "replace"):
            additions += j2 - j1
        if tag in ("delete", "replace"):
            deletions += i2 - i1
    return additions, deletions


async def _session(db, workspace) -> Session:
    sess = Session(project_id=None, worktree_path=str(workspace), title="round", status="active")
    db.add(sess)
    await db.flush()
    return sess


async def _turn(db, session, *, wakeup: bool = False,
                content_text: str = "x") -> Turn:
    """建一轮 + 触发消息 + 快照；wakeup=True 表示后台事件唤醒的续跑轮。"""
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()
    content: dict = {"text": content_text}
    if wakeup:
        content["subagent_wakeup"] = True
    msg = Message(session_id=session.id, turn_id=turn.id, sender_type="user",
                  msg_type="text", content=content)
    db.add(msg)
    await db.flush()
    # 真实链路里 turn_service.create_turn 会回填触发消息 id；参数校验靠它找触发消息
    turn.user_message_id = msg.id
    db.add(TurnSnapshot(session_id=session.id, turn_id=turn.id, user_message_id=msg.id,
                        git_head=None, file_list=[], new_files=[]))
    await db.flush()
    await db.commit()
    return turn


async def _record(db, sess, turn, before: str | None, after: str | None, op: str) -> None:
    await rs.record_turn_write(
        db, session_id=sess.id, turn_id=turn.id, tool="fs_write", path=PATH,
        before=before, after=after, op=op,
    )


def test_round_turn_ids_dedupes_and_sorts():
    """入参可能是乱序 / 重复 / 含基底轮本身；返回去重升序，首项即基底轮。"""
    assert rs.round_turn_ids(10, [12, 11, 12, 10]) == [10, 11, 12]
    assert rs.round_turn_ids(10, None) == [10]
    assert rs.round_turn_ids(10, []) == [10]


@pytest.mark.asyncio
async def test_list_turn_changes_aggregates_round(db, tmp_path):
    """整组口径：同一文件在两轮各改一次，只出一条，+N 按整组首尾计算（不是两次相加）。"""
    ws = tmp_path / "ws1"
    ws.mkdir()
    sess = await _session(db, ws)
    base = await _turn(db, sess)
    cont = await _turn(db, sess, wakeup=True)

    await _record(db, sess, base, None, "a\n", rs.OP_CREATE)
    await _record(db, sess, cont, "a\n", "a\nb\nc\n", rs.OP_MODIFY)

    single = await rs.list_turn_changes(db, session_id=sess.id, turn_id=base.id, workspace=str(ws))
    assert len(single) == 1
    assert (single[0]["additions"], single[0]["deletions"]) == _counts(None, "a\n")

    merged = await rs.list_turn_changes(
        db, session_id=sess.id, turn_id=base.id, workspace=str(ws), extra_turn_ids=[cont.id],
    )
    assert len(merged) == 1
    assert merged[0]["path"] == PATH
    assert merged[0]["action"] == "added"
    # 整组首条写前不存在、末条写后在 → 行数按 "a\nb\nc\n" 统计（3 行），不是 1+2 简单相加
    assert (merged[0]["additions"], merged[0]["deletions"]) == _counts(None, "a\nb\nc\n")


@pytest.mark.asyncio
async def test_get_file_diff_aggregates_round(db, tmp_path):
    """审查 diff 必须是整组累积口径：before 取基底轮写前，after 取续跑轮写后。"""
    ws = tmp_path / "ws2"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("a\nb\nc\n", encoding="utf-8")
    sess = await _session(db, ws)
    base = await _turn(db, sess)
    cont = await _turn(db, sess, wakeup=True)

    await _record(db, sess, base, "a\n", "a\nb\n", rs.OP_MODIFY)
    await _record(db, sess, cont, "a\nb\n", "a\nb\nc\n", rs.OP_MODIFY)

    diff = await rs.get_file_diff(
        db, session_id=sess.id, turn_id=base.id, workspace=str(ws), path=PATH,
        extra_turn_ids=[cont.id],
    )
    assert diff is not None
    assert diff["before"] == "a\n"
    assert diff["after"] == "a\nb\nc\n"
    assert (diff["additions"], diff["deletions"]) == _counts("a\n", "a\nb\nc\n")


@pytest.mark.asyncio
async def test_revert_turn_files_covers_whole_round(db, tmp_path, monkeypatch):
    """撤销按整组一次完成：文件回到基底轮之前，两轮写盘记录与快照登记一并清理。"""
    ws = tmp_path / "ws3"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("a\nb\nc\n", encoding="utf-8")  # 磁盘 = 末条记录写后
    sess = await _session(db, ws)
    base = await _turn(db, sess)
    cont = await _turn(db, sess, wakeup=True)

    await _record(db, sess, base, "a\n", "a\nb\n", rs.OP_MODIFY)
    await _record(db, sess, cont, "a\nb\n", "a\nb\nc\n", rs.OP_MODIFY)

    events: list[dict] = []

    async def _fake_broadcast(session_id, payload):
        events.append({"session_id": session_id, **payload})

    from app.orchestration import agent_events
    from app.services import audit_service

    async def _noop_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(agent_events, "broadcast", _fake_broadcast)
    monkeypatch.setattr(audit_service, "log", _noop_audit)

    result = await rs.revert_turn_files(db, turn_id=base.id, extra_turn_ids=[cont.id])

    assert result["ok"] is True, result
    assert result["turn_ids"] == sorted([base.id, cont.id])
    assert (ws / "src" / "a.py").read_text(encoding="utf-8") == "a\n"

    left = await db.execute(
        select(RollbackWrite).where(RollbackWrite.turn_id.in_([base.id, cont.id]))
    )
    assert list(left.scalars().all()) == []

    snaps = await db.execute(
        select(TurnSnapshot).where(TurnSnapshot.turn_id.in_([base.id, cont.id]))
    )
    for snap in snaps.scalars().all():
        assert snap.file_list == []
        assert snap.new_files == []

    reverted = [e for e in events if e.get("event") == "turn.files_reverted"]
    assert len(reverted) == 1
    assert reverted[0]["payload"]["turn_ids"] == sorted([base.id, cont.id])


@pytest.mark.asyncio
async def test_revert_turn_files_rejects_later_turn_outside_round(db, tmp_path):
    """组外还有更晚的可见改动时必须拒绝——整组不能越过更新的轮次先撤。"""
    ws = tmp_path / "ws4"
    ws.mkdir()
    sess = await _session(db, ws)
    base = await _turn(db, sess)
    cont = await _turn(db, sess, wakeup=True)
    later = await _turn(db, sess)

    await _record(db, sess, base, None, "a\n", rs.OP_CREATE)
    await _record(db, sess, cont, "a\n", "a\nb\n", rs.OP_MODIFY)
    await _record(db, sess, later, "a\nb\n", "a\nb\nc\n", rs.OP_MODIFY)

    result = await rs.revert_turn_files(db, turn_id=base.id, extra_turn_ids=[cont.id])
    assert result["ok"] is False
    assert result["code"] == "order_violation"
    assert later.id in result["later_turn_ids"]


@pytest.mark.asyncio
async def test_round_extra_turn_ids_accepts_only_wakeup_turns(db, tmp_path):
    """路由层参数校验：只有「同会话 + 更晚 + 系统续跑」的轮次才并入整组。"""
    from app.gateway.routers.turns import _round_extra_turn_ids

    ws = tmp_path / "ws5"
    ws.mkdir()
    sess = await _session(db, ws)
    base = await _turn(db, sess)
    earlier = await _turn(db, sess, wakeup=True, content_text="earlier")
    wake = await _turn(db, sess, wakeup=True)
    normal = await _turn(db, sess)

    raw = f"{wake.id},{normal.id}"
    got = await _round_extra_turn_ids(db, base.id, sess.id, raw)
    assert got == [wake.id]  # 普通用户轮不并入

    # 更早的轮次（同样带唤醒标记）也不能并入：整组只有「基底轮之后」
    got2 = await _round_extra_turn_ids(db, wake.id, sess.id, f"{earlier.id}")
    assert got2 == []

    # 跨会话 / 非数字一律丢弃
    assert await _round_extra_turn_ids(db, base.id, sess.id, "abc,") == []
    assert await _round_extra_turn_ids(db, base.id, sess.id + 999, f"{wake.id}") == []

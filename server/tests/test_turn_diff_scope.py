"""单轮 diff 口径测试（plan-20261018-turn-changes）。

背景：卡片「审查」按整轮口径取 diff。旧实现 after 直接读当前磁盘，导致点上一轮
审查时看到的是「后续轮次改完之后」的内容，与卡片 +N -M 也不一致。
本文件锁定「每一轮只展示自己那一段改动」这一契约。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.message import Message, Session
from app.persistence.models.rollback import TurnSnapshot
from app.persistence.models.turn import Turn
from app.services.rollback_service import (
    get_file_diff,
    list_turn_changes,
    record_turn_write,
)


@pytest.fixture
async def db(monkeypatch, tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rb_diff_scope.db"
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


async def _turn_with_snapshot(db, session: Session) -> Turn:
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()
    msg = Message(session_id=session.id, turn_id=turn.id, sender_type="user",
                  msg_type="text", content={"text": "改文件"})
    db.add(msg)
    await db.flush()
    db.add(TurnSnapshot(
        session_id=session.id, turn_id=turn.id, user_message_id=msg.id,
        git_head=None, file_list=[], new_files=[],
    ))
    await db.flush()
    await db.commit()  # record_turn_write 经写引擎独立连接写入，需先提交
    return turn


@pytest.mark.asyncio
async def test_each_turn_diff_only_contains_its_own_change(db, tmp_path):
    """两轮改同一文件时，turn1 的 diff 不能混入 turn2 的内容，反之亦然。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "app.ts").write_text("v3\n", encoding="utf-8")

    session = Session(project_id=None, worktree_path=str(workspace), title="diff_scope", status="active")
    db.add(session)
    await db.flush()

    turn1 = await _turn_with_snapshot(db, session)
    await record_turn_write(db, session_id=session.id, turn_id=turn1.id, tool="fs_write",
                            path="app.ts", before="v1\n", after="v2\n")
    turn2 = await _turn_with_snapshot(db, session)
    await record_turn_write(db, session_id=session.id, turn_id=turn2.id, tool="fs_write",
                            path="app.ts", before="v2\n", after="v3\n")

    d1 = await get_file_diff(db, session_id=session.id, turn_id=turn1.id,
                             workspace=str(workspace), path="app.ts")
    assert d1 is not None
    assert d1["before"] == "v1\n"
    assert d1["after"] == "v2\n"          # 关键：不是磁盘里的 v3
    assert d1["additions"] == 1
    assert d1["deletions"] == 1

    d2 = await get_file_diff(db, session_id=session.id, turn_id=turn2.id,
                             workspace=str(workspace), path="app.ts")
    assert d2 is not None
    assert d2["before"] == "v2\n"
    assert d2["after"] == "v3\n"

    # 卡片徽标与 diff 同源：两轮各 1 增 1 删
    c1 = await list_turn_changes(db, session_id=session.id, turn_id=turn1.id, workspace=str(workspace))
    c2 = await list_turn_changes(db, session_id=session.id, turn_id=turn2.id, workspace=str(workspace))
    assert (c1[0]["additions"], c1[0]["deletions"]) == (1, 1)
    assert (c2[0]["additions"], c2[0]["deletions"]) == (1, 1)
    assert (c1[0]["additions"], c1[0]["deletions"]) == (d1["additions"], d1["deletions"])


@pytest.mark.asyncio
async def test_turn_deleting_file_reports_no_after(db, tmp_path):
    """该轮把文件删掉时，after 必须是 None（删除），不能回退成磁盘里的新内容。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # 磁盘上该文件后来又被别的轮次重建，用于验证不会混入
    (workspace / "gone.ts").write_text("recreated by later turn\n", encoding="utf-8")

    session = Session(project_id=None, worktree_path=str(workspace), title="diff_del", status="active")
    db.add(session)
    await db.flush()

    turn = await _turn_with_snapshot(db, session)
    await record_turn_write(db, session_id=session.id, turn_id=turn.id, tool="fs_write",
                            path="gone.ts", before="original\n", after=None)

    diff = await get_file_diff(db, session_id=session.id, turn_id=turn.id,
                               workspace=str(workspace), path="gone.ts")
    assert diff is not None
    assert diff["before"] == "original\n"
    assert diff["after"] is None
    assert diff["deletions"] == 1
    assert diff["additions"] == 0

    changes = await list_turn_changes(db, session_id=session.id, turn_id=turn.id, workspace=str(workspace))
    assert changes[0]["action"] == "deleted"

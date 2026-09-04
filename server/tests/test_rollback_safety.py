"""回滚机制安全性测试（v38）：
- 验证回滚无写盘记录的 turn 时，绝对不执行 git checkout，100% 保持工作区文件完整
- 验证回滚后 RollbackWrite 记录被物理清理，避免已回滚的脏记录干扰后续操作
- 验证有写盘记录时精确回滚功能正常
"""
import pytest
from sqlalchemy import select

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.persistence.database import Base
from app.persistence.models.message import Message, Session
from app.persistence.models.project import Project
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
from app.persistence.models.turn import Turn
from app.services.rollback_service import (
    create_turn_snapshot,
    list_turn_writes,
    record_turn_write,
    rollback_turn,
)


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # plan-644: rollback_turn -> engine.cancel_turn 内部用全局
    # async_session_factory 连库（此前误连真实库文件的旧 schema，模型加列后暴露）；
    # 测试统一指回内存库，保证隔离。
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_rollback_no_writes_does_not_touch_workspace(db, tmp_path):
    """无写盘记录的 turn 回滚时，绝不修改工作区现有文件。"""
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    target_file = workspace / "client" / "src" / "store" / "chat.ts"
    target_file.parent.mkdir(parents=True)
    initial_content = "export const useChatStore = 'active version';"
    target_file.write_text(initial_content, encoding="utf-8")

    session = Session(project_id=None, worktree_path=str(workspace), title="test_session", status="active")
    db.add(session)
    await db.flush()

    # Turn 1: 仅查看文件，无写盘记录
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()

    msg = Message(session_id=session.id, turn_id=turn.id, sender_type="user", msg_type="text", content={"text": "查看chat.ts"})
    db.add(msg)
    await db.flush()

    snap = TurnSnapshot(
        session_id=session.id, turn_id=turn.id, user_message_id=msg.id,
        git_head=None, file_list=[], new_files=[],
    )
    db.add(snap)
    await db.flush()
    await db.commit()

    # 执行回滚
    res = await rollback_turn(db, turn_id=turn.id, restore_to_composer=True)
    await db.commit()

    assert res["ok"] is True
    assert res["file_recovery"]["skipped"] is True
    assert res["file_recovery"]["restored"] == 0
    assert res["file_recovery"]["deleted"] == 0
    # 文件内容必须 100% 保持未被修改
    assert target_file.read_text(encoding="utf-8") == initial_content


@pytest.mark.asyncio
async def test_rollback_cleans_up_rollback_writes(db, tmp_path):
    """回滚执行后，对应 turn 的 RollbackWrite 记录被物理清理。"""
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    file_a = workspace / "file_a.txt"
    file_a.write_text("modified content", encoding="utf-8")

    session = Session(project_id=None, worktree_path=str(workspace), title="test_session", status="active")
    db.add(session)
    await db.flush()

    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()

    msg = Message(session_id=session.id, turn_id=turn.id, sender_type="user", msg_type="text", content={"text": "修改文件"})
    db.add(msg)
    await db.flush()

    snap = TurnSnapshot(
        session_id=session.id, turn_id=turn.id, user_message_id=msg.id,
        git_head=None, file_list=[], new_files=[],
    )
    db.add(snap)
    await db.flush()

    # 记录一次写盘
    await record_turn_write(db, session_id=session.id, turn_id=turn.id, tool="fs_write",
                            path="file_a.txt", before="original content", after="modified content")
    await db.commit()

    # 回滚前能查到 1 条写盘记录
    writes_before = await list_turn_writes(db, session.id, turn.id)
    assert len(writes_before) == 1

    # 执行回滚
    res = await rollback_turn(db, turn_id=turn.id, restore_to_composer=True)
    await db.commit()

    assert res["ok"] is True
    assert file_a.read_text(encoding="utf-8") == "original content"

    # 回滚后 RollbackWrite 记录已清除
    writes_after = await list_turn_writes(db, session.id, turn.id)
    assert len(writes_after) == 0

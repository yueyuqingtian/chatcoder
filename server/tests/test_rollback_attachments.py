"""回滚回填附件测试（v2.2，plan-88 任务 A）。

覆盖：
- rollback_turn(restore_to_composer=True) 返回 user_message 与
  content.attachments 原样（图片等附件一并撤回输入框）。
- restore_to_composer=False 时不回填文字与附件。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.persistence.database import Base
from app.persistence.models import Message, Session, Turn  # noqa: F401  注册全部模型
from app.persistence.models.rollback import TurnSnapshot
from app.services import rollback_service


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


ATTACHMENTS = [
    {"file_id": "abc123", "filename": "shot.png", "path": "abc123/shot.png",
     "url": "/api/uploads/abc123/shot.png", "size": 1024, "mime_type": "image/png",
     "type": "image"},
]


async def _seed_rollback(db, workspace: str, *, attachments=ATTACHMENTS) -> int:
    """构造 session/turn/user 消息/快照，返回 turn_id。"""
    session = Session(project_id=None, worktree_path=workspace, title="t")
    db.add(session)
    await db.flush()
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()
    content: dict = {"text": "请查看图片并修改"}
    if attachments:
        content["attachments"] = attachments
    msg = Message(
        session_id=session.id, turn_id=turn.id,
        sender_type="user", msg_type="text", content=content,
    )
    db.add(msg)
    await db.flush()
    snap = TurnSnapshot(
        session_id=session.id, turn_id=turn.id, user_message_id=msg.id,
        git_head=None, file_list=[], new_files=[],
    )
    db.add(snap)
    await db.flush()
    await db.commit()
    return turn.id


async def test_rollback_returns_attachments_with_composer(db, workspace):
    turn_id = await _seed_rollback(db, str(workspace))
    result = await rollback_service.rollback_turn(db, turn_id=turn_id, restore_to_composer=True)
    assert result["ok"] is True
    assert result["user_message"] == "请查看图片并修改"
    assert result["user_attachments"] == ATTACHMENTS


async def test_rollback_skips_composer_when_disabled(db, workspace):
    turn_id = await _seed_rollback(db, str(workspace))
    result = await rollback_service.rollback_turn(db, turn_id=turn_id, restore_to_composer=False)
    assert result["ok"] is True
    assert result["user_message"] is None
    assert result["user_attachments"] is None


async def test_rollback_attachments_none_when_missing(db, workspace):
    turn_id = await _seed_rollback(db, str(workspace), attachments=None)
    result = await rollback_service.rollback_turn(db, turn_id=turn_id, restore_to_composer=True)
    assert result["ok"] is True
    assert result["user_message"] == "请查看图片并修改"
    assert result["user_attachments"] is None

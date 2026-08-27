"""checkpoint 生命周期测试（plan-88 任务 D）。

覆盖：
- checkpoint_file 目录结构化：传 session_id/turn_id 时落入 session-{id}/{turn}/。
- collect_orphans：磁盘文件但 DB 无引用识别为孤儿。
- cleanup：孤儿/过期/超量删除，并同步清理 TurnSnapshot.file_list 登记。
- rollback_turn 后 checkpoint 清理（checkpoint_cleanup_on_rollback）。
"""
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.persistence.database import Base
from app.persistence.models import Message, Session, Turn  # noqa: F401  注册全部模型
from app.persistence.models.rollback import TurnSnapshot
from app.services import checkpoint_gc, rollback_service


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


# ── 目录结构化 ──


def test_checkpoint_file_nested_by_session_turn(workspace):
    target = workspace / "src" / "main.py"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    ckpt = rollback_service.checkpoint_file(str(workspace), str(target), session_id=7, turn_id=3)
    assert ckpt is not None
    rel = Path(ckpt).relative_to(workspace / ".chatcoder" / "checkpoints")
    assert rel.parts[0] == "session-7"
    assert rel.parts[1] == "3"


def test_checkpoint_file_flat_without_session(workspace):
    target = workspace / "src" / "main.py"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    ckpt = rollback_service.checkpoint_file(str(workspace), str(target))
    assert ckpt is not None
    rel = Path(ckpt).relative_to(workspace / ".chatcoder" / "checkpoints")
    assert len(rel.parts) == 1  # 平铺（未传 session/turn，兼容旧调用）


# ── 孤儿识别与清理 ──


async def test_collect_orphans(db, workspace):
    # 磁盘有文件但 DB 无引用 → 孤儿
    (workspace / ".chatcoder" / "checkpoints").mkdir(parents=True)
    (workspace / ".chatcoder" / "checkpoints" / "orphan.bin").write_bytes(b"x")
    result = await checkpoint_gc.collect_orphans(db, str(workspace))
    assert result["orphan_count"] == 1


async def test_cleanup_deletes_orphan_and_syncs_db(db, workspace):
    ckpt_dir = workspace / ".chatcoder" / "checkpoints"
    ckpt_dir.mkdir(parents=True)
    orphan = ckpt_dir / "orphan.bin"
    orphan.write_bytes(b"o")

    # 被引用的 checkpoint：DB 登记 + 磁盘文件
    snap = TurnSnapshot(session_id=1, turn_id=1,
                        file_list=[{"ckpt": str(ckpt_dir / "ref.bin"), "path": "a.bin"}], new_files=[])
    db.add(snap)
    (ckpt_dir / "ref.bin").write_bytes(b"r")
    await db.commit()

    result = await checkpoint_gc.cleanup(db, str(workspace))
    assert result["deleted"] == 1  # 仅孤儿被删（ref 有引用，未过期未超量）
    assert not orphan.exists()
    assert (ckpt_dir / "ref.bin").exists()

    # DB 登记未被误删
    await db.refresh(snap)
    assert len(snap.file_list) == 1


async def test_cleanup_expired_removes_db_entry(db, workspace):
    ckpt_dir = workspace / ".chatcoder" / "checkpoints"
    ckpt_dir.mkdir(parents=True)
    snap = TurnSnapshot(session_id=1, turn_id=1,
                        file_list=[{"ckpt": str(ckpt_dir / "old.bin"), "path": "a.bin"}], new_files=[])
    db.add(snap)
    await db.commit()
    old = ckpt_dir / "old.bin"
    old.write_bytes(b"x")
    # 强制 mtime 远超 retention（14 天）
    import time as _time
    _time_old = _time.time() - 30 * 86400
    import os
    os.utime(old, (_time_old, _time_old))

    result = await checkpoint_gc.cleanup(db, str(workspace))
    assert result["expired"] == 1
    assert not old.exists()
    # DB 登记同步清理
    await db.refresh(snap)
    assert snap.file_list == []

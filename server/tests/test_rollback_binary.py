"""回滚机制完善测试（plan-88 任务 B）。

覆盖：
- _is_binary_path：NUL 字节 / 非 UTF-8 / 超限判定。
- record_checkpoint_for_turn：同 turn 同文件去重（只保留首次写盘前备份）。
- _rollback_turn_files_precise：binary 记录走 checkpoint 二进制恢复，
  不做文本三路合并（防止损坏二进制文件）。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.persistence.database import Base
from app.persistence.models import Message, Session, Turn  # noqa: F401  注册全部模型
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
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


# ── 二进制判定 ──


def test_is_binary_detects_nul_byte(workspace):
    p = workspace / "a.bin"
    p.write_bytes(b"PK\x03\x04\x00\x00\x00\x00")
    assert rollback_service._is_binary_path(str(workspace), "a.bin") is True


def test_is_binary_detects_non_utf8(workspace):
    p = workspace / "b.bin"
    p.write_bytes(b"\xff\xfe\x00\x41\x00\x42")
    assert rollback_service._is_binary_path(str(workspace), "b.bin") is True


def test_is_binary_false_for_text(workspace):
    p = workspace / "c.txt"
    p.write_text("hello world\n", encoding="utf-8")
    assert rollback_service._is_binary_path(str(workspace), "c.txt") is False


def test_is_binary_true_when_over_max_bytes(workspace):
    p = workspace / "big.txt"
    p.write_bytes(b"x" * 2048)
    assert rollback_service._is_binary_path(str(workspace), "big.txt", max_bytes=1024) is True
    assert rollback_service._is_binary_path(str(workspace), "big.txt", max_bytes=4096) is False


# ── checkpoint 同 turn 同文件去重 ──


async def test_checkpoint_dedup_same_path(db, workspace):
    snap = TurnSnapshot(session_id=1, turn_id=1, file_list=[], new_files=[])
    db.add(snap)
    await db.flush()
    await rollback_service.record_checkpoint_for_turn(db, 1, "/ckpt/first", "src/a.py")
    await rollback_service.record_checkpoint_for_turn(db, 1, "/ckpt/second", "src/a.py")
    await db.refresh(snap)
    assert len(snap.file_list) == 1
    assert snap.file_list[0]["ckpt"] == "/ckpt/first"


async def test_checkpoint_dedup_keeps_distinct_paths(db, workspace):
    snap = TurnSnapshot(session_id=1, turn_id=1, file_list=[], new_files=[])
    db.add(snap)
    await db.flush()
    await rollback_service.record_checkpoint_for_turn(db, 1, "/ckpt/a", "src/a.py")
    await rollback_service.record_checkpoint_for_turn(db, 1, "/ckpt/b", "src/b.py")
    await db.refresh(snap)
    assert len(snap.file_list) == 2


# ── binary 写盘记录走 checkpoint 恢复 ──


async def test_binary_rollback_restores_checkpoint(workspace):
    target = workspace / "assets" / "logo.bin"
    target.parent.mkdir()
    target.write_bytes(b"ORIGINAL-BINARY")
    ckpt = rollback_service.checkpoint_file(str(workspace), str(target))
    target.write_bytes(b"MODIFIED-BY-AI")  # AI 写盘后

    snap = TurnSnapshot(session_id=1, turn_id=1,
                        file_list=[{"ckpt": ckpt, "path": "assets/logo.bin"}], new_files=[])
    rec = RollbackWrite(session_id=1, turn_id=1, tool="fs_write",
                        path="assets/logo.bin", old_content=None, new_content=None, binary=True)
    result = await rollback_service._rollback_turn_files_precise(str(workspace), [rec], snap)
    assert result["restored"] == 1 and result["failed"] == 0
    assert target.read_bytes() == b"ORIGINAL-BINARY"


async def test_binary_rollback_missing_checkpoint_marks_conflict(workspace):
    rec = RollbackWrite(session_id=1, turn_id=1, tool="fs_write",
                        path="assets/x.bin", old_content=None, new_content=None, binary=True)
    snap = TurnSnapshot(session_id=1, turn_id=1, file_list=[], new_files=[])
    result = await rollback_service._rollback_turn_files_precise(str(workspace), [rec], snap)
    assert result["failed"] == 1
    assert result["conflicts"] == ["assets/x.bin"]

"""仅撤销单轮文件改动（plan-20261018-turn-changes）测试。

覆盖：正常撤销与记录清理、顺序门控、撤销后解锁上一轮、手动改动冲突与部分撤销、
撤销锁查询。fixture 与 test_rollback_safety.py 同源（临时 sqlite + 写引擎同库）。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.message import Message, Session
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
from app.persistence.models.turn import Turn
from app.services.rollback_service import (
    list_turn_writes,
    record_turn_write,
    revert_lock,
    revert_turn_files,
)


@pytest.fixture
async def db(monkeypatch, tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rb_revert.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)  # 写引擎与测试库同源（服务写经单写线程）
    async with factory() as s:
        yield s
    await engine.dispose()
    _we.configure(None)


async def _new_session(db, workspace) -> Session:
    sess = Session(project_id=None, worktree_path=str(workspace), title="revert_test", status="active")
    db.add(sess)
    await db.flush()
    return sess


async def _new_turn(db, session, *, rel_path: str, new_files: list | None = None) -> tuple[Turn, TurnSnapshot]:
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()
    msg = Message(session_id=session.id, turn_id=turn.id, sender_type="user",
                  msg_type="text", content={"text": "改动文件"})
    db.add(msg)
    await db.flush()
    snap = TurnSnapshot(
        session_id=session.id, turn_id=turn.id, user_message_id=msg.id,
        git_head=None,
        # 非二进制写盘的 checkpoint 登记（ckpt 为空不影响文本三路合并）
        file_list=[{"ckpt": "", "path": rel_path}],
        new_files=new_files or [],
    )
    db.add(snap)
    await db.flush()
    await db.commit()  # record_turn_write 经写引擎独立连接写入，需先提交
    return turn, snap


@pytest.mark.asyncio
async def test_revert_restores_files_and_clears_records(db, tmp_path):
    """正常撤销：文件恢复原内容，写盘记录与快照登记清空，turn 状态不变。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    file_a = workspace / "file_a.txt"
    file_a.write_text("modified content", encoding="utf-8")

    sess = await _new_session(db, workspace)
    turn, snap = await _new_turn(db, sess, rel_path="file_a.txt")
    await record_turn_write(db, session_id=sess.id, turn_id=turn.id, tool="fs_write",
                            path="file_a.txt", before="original content", after="modified content")
    sess_id, turn_id, snap_id = sess.id, turn.id, snap.id

    res = await revert_turn_files(db, turn_id=turn_id)

    assert res["ok"] is True
    assert res["restored"] == 1
    assert res["deleted"] == 0
    assert res["skipped"] == 0
    assert res["files"] == ["file_a.txt"]
    assert file_a.read_text(encoding="utf-8") == "original content"

    # expire_all 后必须用已保存的 id 查询，避免同步上下文触发 lazy load
    db.expire_all()
    assert await list_turn_writes(db, sess_id, turn_id) == []
    snap_after = await db.get(TurnSnapshot, snap_id)
    assert snap_after.file_list == []
    assert snap_after.new_files == []
    turn_after = await db.get(Turn, turn_id)
    assert turn_after.status == "completed"


@pytest.mark.asyncio
async def test_revert_order_violation(db, tmp_path):
    """顺序门控：存在更晚轮次的写盘记录时拒绝，且不改任何文件。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    file_a = workspace / "file_a.txt"
    file_b = workspace / "file_b.txt"
    file_a.write_text("modified a", encoding="utf-8")
    file_b.write_text("modified b", encoding="utf-8")

    sess = await _new_session(db, workspace)
    turn1, _ = await _new_turn(db, sess, rel_path="file_a.txt")
    turn2, _ = await _new_turn(db, sess, rel_path="file_b.txt")
    await record_turn_write(db, session_id=sess.id, turn_id=turn1.id, tool="fs_write",
                            path="file_a.txt", before="original a", after="modified a")
    await record_turn_write(db, session_id=sess.id, turn_id=turn2.id, tool="fs_write",
                            path="file_b.txt", before="original b", after="modified b")

    res = await revert_turn_files(db, turn_id=turn1.id)

    assert res["ok"] is False
    assert res["code"] == "order_violation"
    assert res["later_turn_ids"] == [turn2.id]
    assert file_a.read_text(encoding="utf-8") == "modified a"
    # 拒绝时不得清理本 turn 的写盘记录（list_turn_writes 是 >= 语义，这里精确过滤）
    left = await db.execute(select(RollbackWrite).where(RollbackWrite.turn_id == turn1.id))
    assert len(list(left.scalars().all())) == 1


@pytest.mark.asyncio
async def test_revert_latest_unlocks_previous_turn(db, tmp_path):
    """从最新一轮往前撤：撤销 turn2 后 turn1 解锁并可成功撤销。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    file_a = workspace / "file_a.txt"
    file_b = workspace / "file_b.txt"
    file_a.write_text("modified a", encoding="utf-8")
    file_b.write_text("modified b", encoding="utf-8")

    sess = await _new_session(db, workspace)
    turn1, _ = await _new_turn(db, sess, rel_path="file_a.txt")
    turn2, _ = await _new_turn(db, sess, rel_path="file_b.txt")
    await record_turn_write(db, session_id=sess.id, turn_id=turn1.id, tool="fs_write",
                            path="file_a.txt", before="original a", after="modified a")
    await record_turn_write(db, session_id=sess.id, turn_id=turn2.id, tool="fs_write",
                            path="file_b.txt", before="original b", after="modified b")

    res2 = await revert_turn_files(db, turn_id=turn2.id)
    assert res2["ok"] is True
    assert file_b.read_text(encoding="utf-8") == "original b"

    # turn2 记录已清理，turn1 不再被门控拦截
    res1 = await revert_turn_files(db, turn_id=turn1.id)
    assert res1["ok"] is True
    assert file_a.read_text(encoding="utf-8") == "original a"

    lock = await revert_lock(db, sess.id)
    assert lock == {"latest_turn_id": None, "pending_turn_ids": []}


@pytest.mark.asyncio
async def test_revert_conflict_default_and_partial(db, tmp_path):
    """手动改动冲突：默认整体拒绝且不改文件；allow_partial=True 时跳过冲突文件。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    file_c = workspace / "file_c.txt"
    file_c.write_text("third party content\n", encoding="utf-8")  # 用户手动改写

    sess = await _new_session(db, workspace)
    turn, _ = await _new_turn(db, sess, rel_path="file_c.txt")
    await record_turn_write(db, session_id=sess.id, turn_id=turn.id, tool="fs_write",
                            path="file_c.txt", before="original line\n", after="ai line\n")

    res = await revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is False
    assert res["code"] == "conflict"
    assert [c["path"] for c in res["conflicts"]] == ["file_c.txt"]
    assert file_c.read_text(encoding="utf-8") == "third party content\n"

    partial = await revert_turn_files(db, turn_id=turn.id, allow_partial=True)
    assert partial["ok"] is True
    assert partial["skipped"] == 1
    assert partial["restored"] == 0
    assert file_c.read_text(encoding="utf-8") == "third party content\n"


@pytest.mark.asyncio
async def test_revert_turn_that_deleted_file_succeeds(db, tmp_path):
    """本轮把文件删掉（磁盘上确实已不存在）时应能撤销，并把文件内容找回。

    回归背景（plan-20261018-turn-changes）：build_plan 旧实现把所有「磁盘上不存在」
    的步骤都标成 conflict，导致任何含删除的轮次整单撤销被拒（界面只看到 409）；
    只改成 noop 又会「撤销成功但文件没找回」。正确语义是恢复删除前的内容。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()

    sess = await _new_session(db, workspace)
    turn, _ = await _new_turn(db, sess, rel_path="gone.txt")
    # 该轮把 gone.txt 删掉：before 有内容、after 为 None；磁盘上确实没有该文件
    await record_turn_write(db, session_id=sess.id, turn_id=turn.id, tool="fs_write",
                            path="gone.txt", before="to be deleted\n", after=None)
    assert not (workspace / "gone.txt").exists()

    res = await revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is True, res
    assert res["restored"] == 1
    assert res["deleted"] == 0
    assert res["conflicts"] == []
    # 关键：删除前的内容被找回
    assert (workspace / "gone.txt").read_text(encoding="utf-8") == "to be deleted\n"

    # 记录已清理，无可撤销内容
    lock = await revert_lock(db, sess.id)
    assert lock == {"latest_turn_id": None, "pending_turn_ids": []}


@pytest.mark.asyncio
async def test_revert_lock_reports_latest_and_pending(db, tmp_path):
    """撤销锁：升序返回仍有写盘记录的 turn，latest 为最大值。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    file_a = workspace / "file_a.txt"
    file_b = workspace / "file_b.txt"
    file_a.write_text("modified a", encoding="utf-8")
    file_b.write_text("modified b", encoding="utf-8")

    sess = await _new_session(db, workspace)
    turn1, _ = await _new_turn(db, sess, rel_path="file_a.txt")
    turn2, _ = await _new_turn(db, sess, rel_path="file_b.txt")
    await record_turn_write(db, session_id=sess.id, turn_id=turn1.id, tool="fs_write",
                            path="file_a.txt", before="original a", after="modified a")
    await record_turn_write(db, session_id=sess.id, turn_id=turn2.id, tool="fs_write",
                            path="file_b.txt", before="original b", after="modified b")

    lock = await revert_lock(db, sess.id)
    assert lock["pending_turn_ids"] == [turn1.id, turn2.id]
    assert lock["latest_turn_id"] == turn2.id

    await revert_turn_files(db, turn_id=turn2.id)
    lock_after = await revert_lock(db, sess.id)
    assert lock_after["pending_turn_ids"] == [turn1.id]
    assert lock_after["latest_turn_id"] == turn1.id

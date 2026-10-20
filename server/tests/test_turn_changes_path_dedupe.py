"""写盘记录路径写法归一（正 / 反斜杠）回归测试。

背景（plan-20261020-path-dedupe）：模型写同一个文件时，路径参数有时是
`clinic\\docs\\a.md`、有时是 `clinic/docs/a.md`，写盘记录表 rollback_writes 里
因此出现两个不同的字符串。旧实现按原始 path 分组，于是同一文件在变更卡片、
右侧「产物」里各出现一次；审核状态也可能因为键不一致而丢失。

rollback_service.write_path_key() 只做写法归一：反斜杠转正斜杠、去掉 `./` 前缀、
合并重复斜杠、去掉尾部斜杠。本测试逐项锁定各读取 / 写入入口都用了这个归一键。

覆盖：
1. record_turn_write 落库时 path 已归一；
2. list_turn_changes 对同一文件的正 / 反斜杠记录合并成一条（净变更口径）；
3. session_product_paths 同一文件只返回一次；
4. reviewed_paths / upsert_file_reviews 归一，且反向写法不产生重复行；
5. build_plan 只按归一路径生成一个分组（steps / finals 不出现重复路径）。
"""
import difflib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.message import Message, Session
from app.persistence.models.review import FileReview
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
from app.persistence.models.turn import Turn
from app.services import rollback_service as rs


# 实际写入记录里的两种写法（源码里 `\\` 表示一个反斜杠）
WIN_PATH = "clinic\\docs\\a.md"
POSIX_PATH = "clinic/docs/a.md"
NORMALIZED = "clinic/docs/a.md"


@pytest.fixture
async def db(monkeypatch, tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rb_path_dedupe.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    # 写盘记录 / 审核记录经 WriteEngine 单写线程落库，必须与异步读引擎同源
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    async with factory() as s:
        yield s
    await engine.dispose()
    _we.configure(None)


async def _session(db, workspace) -> Session:
    sess = Session(project_id=None, worktree_path=str(workspace), title="path_dedupe", status="active")
    db.add(sess)
    await db.flush()
    return sess


async def _turn(db, session) -> Turn:
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()
    msg = Message(session_id=session.id, turn_id=turn.id, sender_type="user",
                  msg_type="text", content={"text": "x"})
    db.add(msg)
    await db.flush()
    db.add(TurnSnapshot(session_id=session.id, turn_id=turn.id, user_message_id=msg.id,
                        git_head=None, file_list=[], new_files=[]))
    await db.flush()
    await db.commit()
    return turn


def _diff_counts(before: str | None, after: str | None) -> tuple[int, int]:
    """独立用 difflib 统计「首条写前 → 末条写后」的整轮净变更，避免硬编码行数。"""
    b = (before or "").splitlines()
    a = (after or "").splitlines()
    additions = deletions = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a).get_opcodes():
        if tag in ("insert", "replace"):
            additions += j2 - j1
        if tag in ("delete", "replace"):
            deletions += i2 - i1
    return additions, deletions


async def _create_then_modify(db, sess, turn) -> None:
    """同一 turn：先用反斜杠新建，再用正斜杠修改同一个文件。"""
    await rs.record_turn_write(
        db, session_id=sess.id, turn_id=turn.id, tool="fs_write", path=WIN_PATH,
        before=None, after="x\ny\n", op=rs.OP_CREATE,
    )
    await rs.record_turn_write(
        db, session_id=sess.id, turn_id=turn.id, tool="fs_write", path=POSIX_PATH,
        before="x\ny\n", after="x\ny\nz\n", op=rs.OP_MODIFY,
    )


async def _writes(db, turn_id: int) -> list[RollbackWrite]:
    res = await db.execute(
        select(RollbackWrite).where(RollbackWrite.turn_id == turn_id).order_by(RollbackWrite.id.asc())
    )
    return list(res.scalars().all())


@pytest.mark.asyncio
async def test_record_turn_write_normalizes_path(db, tmp_path):
    """落库时 path 就已归一，后续任何按 path 的查询 / 分组都不会分叉。"""
    ws = tmp_path / "ws1"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess)

    await rs.record_turn_write(
        db, session_id=sess.id, turn_id=turn.id, tool="fs_write", path=WIN_PATH,
        before=None, after="x\n", op=rs.OP_CREATE,
    )

    rows = await _writes(db, turn.id)
    assert len(rows) == 1
    assert rows[0].path == NORMALIZED


@pytest.mark.asyncio
async def test_list_turn_changes_merges_slash_variants(db, tmp_path):
    """正 / 反斜杠的两条记录属于同一个文件：只出现一条，按整轮净变更统计行数。"""
    ws = tmp_path / "ws2"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess)
    await _create_then_modify(db, sess, turn)

    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))

    assert len(changes) == 1
    change = changes[0]
    assert change["path"] == NORMALIZED
    # 首条记录写前不存在、末条记录写后在 → added
    assert change["action"] == "added"
    expected = _diff_counts(None, "x\ny\nz\n")
    assert (change["additions"], change["deletions"]) == expected


@pytest.mark.asyncio
async def test_session_product_paths_dedupes_slash_variants(db, tmp_path):
    """产物列表按归一路径去重：同一文件只返回一次。"""
    ws = tmp_path / "ws3"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess)
    await _create_then_modify(db, sess, turn)

    paths = await rs.session_product_paths(db, sess.id, str(ws))

    assert paths == [NORMALIZED]


@pytest.mark.asyncio
async def test_reviewed_paths_normalizes_backslash_key(db, tmp_path):
    """反斜杠写入审核状态后，查询键是归一路径。"""
    ws = tmp_path / "ws4"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess)

    n = await rs.upsert_file_reviews(db, turn_id=turn.id, paths=[WIN_PATH], reviewed=True)
    assert n == 1

    reviewed = await rs.reviewed_paths(db, turn.id)
    assert reviewed == {NORMALIZED: True}


@pytest.mark.asyncio
async def test_upsert_file_reviews_no_duplicate_rows_for_slash_variants(db, tmp_path):
    """先正斜杠、再反斜杠 upsert 同一文件：命中同一唯一键，不产生第二行。"""
    ws = tmp_path / "ws4b"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess)

    assert await rs.upsert_file_reviews(db, turn_id=turn.id, paths=[POSIX_PATH], reviewed=True) == 1
    assert await rs.upsert_file_reviews(db, turn_id=turn.id, paths=[WIN_PATH], reviewed=True) == 0

    res = await db.execute(select(FileReview).where(FileReview.turn_id == turn.id))
    rows = list(res.scalars().all())
    assert len(rows) == 1
    assert rows[0].path == NORMALIZED


@pytest.mark.asyncio
async def test_build_plan_single_path_group_for_slash_variants(db, tmp_path):
    """两条不同斜杠写法的 modify 记录只规划成一个文件分组，steps / finals 都用归一路径。"""
    ws = tmp_path / "ws5"
    (ws / "clinic" / "docs").mkdir(parents=True)
    # 磁盘内容 = 末条记录写后的样子，strict 校验才不会判成「被手动改过」
    (ws / "clinic" / "docs" / "a.md").write_text("x\ny\nz\n", encoding="utf-8")

    sess = await _session(db, ws)
    turn = await _turn(db, sess)
    writes = [
        RollbackWrite(session_id=sess.id, turn_id=turn.id, tool="fs_write", path=WIN_PATH,
                      old_content="x\n", new_content="x\ny\n", binary=False, op=rs.OP_MODIFY),
        RollbackWrite(session_id=sess.id, turn_id=turn.id, tool="fs_write", path=POSIX_PATH,
                      old_content="x\ny\n", new_content="x\ny\nz\n", binary=False, op=rs.OP_MODIFY),
    ]

    plan = rs.build_plan(str(ws), writes, lambda rec: None)

    assert {s.path for s in plan.steps} == {NORMALIZED}
    assert set(plan.finals) == {NORMALIZED}
    # 同一文件只落一次终态；步骤里也不能并存两种写法
    assert len(plan.finals) == 1
    assert all("\\" not in s.path for s in plan.steps)

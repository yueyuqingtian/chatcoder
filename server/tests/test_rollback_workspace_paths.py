"""plan-20261109-rollback-grouped-fix：历史路径别名的预览、撤销及越界保护。"""
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models import Session, Turn
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
from app.services import rollback_service as rs


@pytest.fixture
async def db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path}/paths.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    from app.persistence import write_engine
    write_engine.configure(url, foreign_keys=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    write_engine.configure(None)


def write(path, before, after, turn=1, op="modify"):
    return RollbackWrite(session_id=1, turn_id=turn, tool="fs_write", path=path,
                         old_content=before, new_content=after, op=op, binary=False)


@pytest.mark.parametrize("legacy", [False, True])
def test_absolute_and_relative_writes_share_chain(tmp_path, legacy):
    target = tmp_path / "a.txt"
    target.write_text("v2", encoding="utf-8")
    records = [write(str(target), "v0", "v1"), write("./a.txt", "v1", "v2", turn=2)]
    if legacy:
        for rec in records:
            rec.op = None
    plan = rs.build_plan(str(tmp_path), records, lambda _: None)
    assert not plan.conflicts
    assert [row["path"] for row in rs.plan_preview_rows(plan)] == ["a.txt"]
    assert records[0].path == str(target)
    assert rs._apply_plan_sync(plan)["ok"]
    assert target.read_text(encoding="utf-8") == "v0"


def test_absolute_path_does_not_hide_real_later_edit(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("human", encoding="utf-8")
    plan = rs.build_plan(str(tmp_path), [write(str(target), "old", "ai")], lambda _: None)
    assert [c["path"] for c in rs._conflict_list(plan)] == ["a.txt"]
    assert not plan.finals
    assert target.read_text() == "human"


@pytest.mark.parametrize("absolute", [False, True])
def test_outside_workspace_is_never_applied(tmp_path, absolute):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("ai", encoding="utf-8")
    path = str(outside) if absolute else "../outside.txt"
    plan = rs.build_plan(str(root), [write(path, "old", "ai")], lambda _: None)
    assert plan.conflicts and not plan.finals
    assert rs._apply_plan_sync(plan)["ok"]
    assert outside.read_text() == "ai"


def test_symlink_escape_is_never_applied(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("ai", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("当前系统未允许创建符号链接")
    plan = rs.build_plan(str(root), [write("link.txt", "old", "ai")], lambda _: None)
    assert plan.conflicts and not plan.finals
    assert outside.read_text() == "ai"


@pytest.mark.skipif(os.name != "nt", reason="Windows 文件名大小写别名")
def test_windows_case_aliases_share_one_file(tmp_path):
    (tmp_path / "a.txt").write_text("v2", encoding="utf-8")
    plan = rs.build_plan(str(tmp_path), [write("A.txt", "v0", "v1"), write("a.txt", "v1", "v2")], lambda _: None)
    assert not plan.conflicts
    assert list(plan.finals) == ["a.txt"]


@pytest.mark.parametrize("entry", ["revert", "rollback"])
@pytest.mark.parametrize("manual", [False, True])
async def test_snapshot_alias_preview_and_both_revert_entries(db, tmp_path, monkeypatch, entry, manual):
    session = Session(title="paths", worktree_path=str(tmp_path))
    db.add(session)
    await db.flush()
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()
    target = tmp_path / "a.txt"
    target.write_bytes(b"before\r\n")
    checkpoint = rs.checkpoint_file(str(tmp_path), str(target))
    snap = TurnSnapshot(session_id=session.id, turn_id=turn.id, rolled_back=False,
                        file_list=[{"path": str(target), "ckpt": checkpoint}, {"path": "a.txt", "ckpt": checkpoint}],
                        new_files=[])
    rec = write("a.txt", "before\n", "after\n", turn=turn.id)
    rec.session_id = session.id
    db.add_all([snap, rec])
    target.write_bytes(b"human\n" if manual else b"after\n")
    await db.commit()
    preview = await rs.build_turn_preview(db, turn.id)
    assert len(preview["files"]) == 1
    assert preview["files"][0]["path"] == "a.txt"
    assert bool(preview["conflicts"]) == manual
    changes = await rs.list_turn_changes(db, session_id=session.id, turn_id=turn.id, workspace=str(tmp_path))
    assert len(changes) == 1 and changes[0]["path"] == "a.txt"
    if entry == "revert":
        result = await rs.revert_turn_files(db, turn_id=turn.id)
    else:
        from app.orchestration import engine
        monkeypatch.setattr(engine, "cancel_turn", AsyncMock(return_value=True))
        result = await rs.rollback_turn(db, turn_id=turn.id)
    assert result["ok"] != manual
    assert target.read_bytes() == (b"human\n" if manual else b"before\r\n")
    remaining = (await db.execute(select(RollbackWrite))).scalars().all()
    assert bool(remaining) == manual


async def test_absolute_write_matches_relative_snapshot(db, tmp_path):
    session = Session(title="absolute", worktree_path=str(tmp_path))
    db.add(session)
    await db.flush()
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()
    target = tmp_path / "a.txt"
    target.write_text("ai", encoding="utf-8")
    rec = write(str(target), None, "ai", turn=turn.id, op="create")
    rec.session_id = session.id
    db.add_all([rec, TurnSnapshot(session_id=session.id, turn_id=turn.id,
                                 file_list=[], new_files=["a.txt"])])
    await db.commit()
    preview = await rs.build_turn_preview(db, turn.id)
    assert not preview["conflicts"]
    assert len(preview["files"]) == 1 and preview["files"][0]["action"] == "delete"
    result = await rs.revert_turn_files(db, turn_id=turn.id)
    assert result["ok"] and not target.exists()

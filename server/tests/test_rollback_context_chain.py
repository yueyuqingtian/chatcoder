"""plan-20261107-rollback-audit：真实临时库与文件系统验证撤销边界、修改链及上下文。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models import Message, Session, Turn
from app.persistence.models.memory import MemoryEntry
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
from app.services import rollback_service as rs


@pytest.fixture
async def db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path}/rollback-audit.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    from app.persistence import write_engine
    write_engine.configure(url, foreign_keys=False)
    async with factory() as db:
        yield db
    await engine.dispose()
    write_engine.configure(None)


def record(turn, before, after, *, op="modify", binary=False, digest=None):
    return RollbackWrite(session_id=1, turn_id=turn, path="a.txt", tool="fs_write",
                         old_content=before, new_content=after, op=op, binary=binary, after_hash=digest)


@pytest.mark.parametrize("legacy", [False, True])
def test_continuous_chain_restores_target_before_state(tmp_path, legacy):
    (tmp_path / "a.txt").write_text("v3", encoding="utf-8")
    writes = [record(1, "v0", "v1"), record(2, "v1", "v2"), record(3, "v2", "v3")]
    if legacy:
        for rec in writes:
            rec.op = None
    plan = rs.build_plan(str(tmp_path), writes, lambda _: None)
    assert not plan.conflicts
    assert rs._apply_plan_sync(plan)["ok"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v0"


@pytest.mark.parametrize("legacy,same_turn", [(False, False), (True, False), (False, True), (True, True)])
def test_human_edit_between_ai_writes_skips_whole_file(tmp_path, legacy, same_turn):
    (tmp_path / "a.txt").write_text("human\nv2", encoding="utf-8")
    writes = [record(1, "v0", "v1"), record(1 if same_turn else 2, "human\nv1", "human\nv2")]
    if legacy:
        for rec in writes:
            rec.op = None
    plan = rs.build_plan(str(tmp_path), writes, lambda _: None)
    assert plan.conflicts and "a.txt" not in plan.finals
    assert rs._apply_plan_sync(plan)["ok"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "human\nv2"


@pytest.mark.parametrize("end_state", ["manual", None])
def test_human_edit_or_delete_after_last_write_is_kept(tmp_path, end_state):
    if end_state is not None:
        (tmp_path / "a.txt").write_text(end_state, encoding="utf-8")
    plan = rs.build_plan(str(tmp_path), [record(1, "old", "ai")], lambda _: None)
    assert plan.conflicts and not plan.finals


def test_create_delete_recreate_chain_returns_to_absence(tmp_path):
    (tmp_path / "a.txt").write_text("v2", encoding="utf-8")
    writes = [record(1, None, "v1", op="create"), record(2, "v1", None, op="delete"),
              record(3, None, "v2", op="create")]
    plan = rs.build_plan(str(tmp_path), writes, lambda _: None)
    assert not plan.conflicts
    assert rs._apply_plan_sync(plan)["ok"]
    assert not (tmp_path / "a.txt").exists()


def test_binary_without_write_fingerprint_is_not_overwritten(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"manual\x00")
    plan = rs.build_plan(str(tmp_path), [record(1, None, None, binary=True)], lambda _: None)
    assert plan.conflicts and not plan.finals


async def make_turn(db, session, text):
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()
    msg = Message(session_id=session.id, turn_id=turn.id, sender_type="user", msg_type="text", content={"text": text})
    db.add(msg)
    await db.flush()
    db.add(TurnSnapshot(session_id=session.id, turn_id=turn.id, user_message_id=msg.id, file_list=[], new_files=[]))
    return turn, msg


async def test_rollback_clears_tool_history_compaction_and_memory_and_keeps_other_session(db, tmp_path, monkeypatch):
    from app.orchestration import engine
    from app.orchestration.context_memory import _fetch_main_messages, build_main_chat_context
    sess = Session(title="audit", worktree_path=str(tmp_path))
    other = Session(title="other", shared_context={"summary": "other-kept"})
    db.add_all([sess, other])
    await db.flush()
    first, kept = await make_turn(db, sess, "KEPT")
    target, removed = await make_turn(db, sess, "REMOVED-INPUT")
    later, _ = await make_turn(db, sess, "REMOVED-LATER")
    for kind in ["text", "tool_call", "tool_result"]:
        db.add(Message(session_id=sess.id, turn_id=later.id, sender_type="agent", msg_type=kind,
                       content={"text": "REMOVED-TOOL", "tool": "fs_write", "call_key": "gone", "output": "REMOVED-RESULT"}))
    db.add(Message(session_id=sess.id, turn_id=later.id, thread_id=999,
                   sender_type="agent", msg_type="text", content={"text": "REMOVED-OLD-THREAD"}))
    # 无 turn_id 的旧摘要也必须失效，不能借 db.get 绕过 deleted 过滤重新进入 prompt。
    summary = Message(session_id=sess.id, sender_type="system", msg_type="summary", content={"text": "REMOVED-SUMMARY"})
    db.add(summary)
    await db.flush()
    sess.shared_context = {"summary": "REMOVED-CACHE", "compacted_ids": [kept.id, removed.id],
                           "compacted_floor": removed.id, "compactions": [{"summary_message_id": summary.id}],
                           "injected_compactions": ["old"], "unrelated_setting": True}
    db.add_all([MemoryEntry(session_id=sess.id, turn_id=target.id, text="REMOVED-MEMORY"),
                MemoryEntry(session_id=sess.id, turn_id=first.id, text="KEPT-MEMORY"),
                MemoryEntry(session_id=other.id, turn_id=later.id, text="OTHER-MEMORY")])
    await db.commit()
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "cancel_turn", cancel)
    result = await rs.rollback_turn(db, turn_id=target.id)
    assert result["ok"]
    remaining_threads = (await db.execute(select(Message).where(
        Message.session_id == sess.id, Message.thread_id == 999, Message.deleted == False,
    ))).scalars().all()
    assert remaining_threads == []
    assert {call.args[0] for call in cancel.await_args_list} == {target.id, later.id}
    await db.refresh(sess)
    assert sess.shared_context == {"unrelated_setting": True}
    await db.refresh(other)
    assert other.shared_context == {"summary": "other-kept"}
    messages = await _fetch_main_messages(db, sess.id, limit=None)
    assert [m.id for m in messages] == [kept.id]
    # 正常调用时当前用户消息已经落库，装配器会把历史最后一条作为当前输入去重。
    current, _ = await make_turn(db, sess, "NEW-INPUT")
    await db.commit()
    built = await build_main_chat_context(db, sess, "NEW-INPUT")
    prompt = "\n".join(str(m.content) for m in built)
    assert "KEPT" in prompt and "REMOVED" not in prompt
    from app.orchestration import context_manager as cm
    monkeypatch.setattr(cm, "_resolve_ta3_model_meta", AsyncMock(return_value=None))
    monkeypatch.setattr(cm, "_symbol_index_hint", AsyncMock(return_value=""))
    monkeypatch.setattr(cm, "project_structure_brief", AsyncMock(return_value=""))
    bundle = await cm.build_main_context(
        db, agent=SimpleNamespace(id=1, model_id=None, name="main"), session=sess,
        project=None, turn=current, user_message="NEW-INPUT", enable_subagents=False,
    )
    actual_prompt = "\n".join(m.model_dump_json() for m in bundle.to_messages())
    assert "KEPT" in actual_prompt and "REMOVED" not in actual_prompt
    memories = (await db.execute(select(MemoryEntry))).scalars().all()
    assert {m.text for m in memories} == {"KEPT-MEMORY", "OTHER-MEMORY"}
    from app.services.memory_service import save_memories
    assert await save_memories(db, session_id=sess.id, turn_id=target.id,
                               memories=[{"text": "迟到的撤销记忆", "importance": 1}]) == 0


async def test_partial_rollback_uses_all_later_writes_and_preserves_manual_file(db, tmp_path, monkeypatch):
    from app.orchestration import engine
    monkeypatch.setattr(engine, "cancel_turn", AsyncMock(return_value=True))
    sess = Session(title="files", worktree_path=str(tmp_path))
    db.add(sess)
    await db.flush()
    first, _ = await make_turn(db, sess, "target")
    second, _ = await make_turn(db, sess, "later")
    for turn, old, new in [(first, "v0", "v1"), (second, "v1", "v2")]:
        db.add(RollbackWrite(session_id=sess.id, turn_id=turn.id, tool="fs_write", path="safe.txt",
                             old_content=old, new_content=new, op="modify"))
    db.add(RollbackWrite(session_id=sess.id, turn_id=first.id, tool="fs_write", path="manual.txt",
                         old_content="original", new_content="ai", op="modify"))
    (tmp_path / "safe.txt").write_text("v2", encoding="utf-8")
    (tmp_path / "manual.txt").write_text("human", encoding="utf-8")
    await db.commit()
    refused = await rs.rollback_turn(db, turn_id=first.id)
    assert not refused["ok"] and (tmp_path / "safe.txt").read_text() == "v2"
    result = await rs.rollback_turn(db, turn_id=first.id, allow_partial=True)
    assert result["ok"] and result["file_recovery"]["conflicts"] == ["manual.txt"]
    assert (tmp_path / "safe.txt").read_text() == "v0"
    assert (tmp_path / "manual.txt").read_text() == "human"


@pytest.mark.parametrize("later_write", [False, True])
async def test_checkpoint_without_write_record_never_overwrites_user_change(db, tmp_path, monkeypatch, later_write):
    from app.orchestration import engine
    monkeypatch.setattr(engine, "cancel_turn", AsyncMock(return_value=True))
    sess = Session(title="checkpoint", worktree_path=str(tmp_path))
    db.add(sess)
    await db.flush()
    turn, _ = await make_turn(db, sess, "撤销")
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    ckpt = rs.checkpoint_file(str(tmp_path), str(target))
    snap = (await db.execute(select(TurnSnapshot).where(TurnSnapshot.turn_id == turn.id))).scalar_one()
    snap.file_list = [{"path": "a.txt", "ckpt": ckpt}]
    target.write_text("human", encoding="utf-8")
    expected = "human"
    if later_write:
        later, _ = await make_turn(db, sess, "后续修改")
        db.add(RollbackWrite(session_id=sess.id, turn_id=later.id, tool="fs_write", path="a.txt",
                             old_content="human", new_content="ai-later", op="modify"))
        expected = "ai-later"
        target.write_text(expected, encoding="utf-8")
    await db.commit()
    preview = await rs.build_turn_preview(db, turn.id)
    assert preview["conflicts"][0]["path"] == "a.txt"
    result = await rs.rollback_turn(db, turn_id=turn.id, allow_partial=True)
    assert result["ok"] and target.read_text() == expected


async def test_background_exit_failure_does_not_delete_messages_or_restore_files(db, tmp_path, monkeypatch):
    from app.orchestration import engine
    from app.orchestration.tools.bg_process import bg_process_registry
    monkeypatch.setattr(engine, "cancel_turn", AsyncMock(return_value=True))
    monkeypatch.setattr(bg_process_registry, "kill_since", AsyncMock(side_effect=OSError("进程仍占用文件")))
    sess = Session(title="busy", worktree_path=str(tmp_path))
    db.add(sess)
    await db.flush()
    turn, msg = await make_turn(db, sess, "KEPT-BUSY")
    db.add(RollbackWrite(session_id=sess.id, turn_id=turn.id, path="a.txt", tool="fs_write",
                         old_content="old", new_content="ai", op="modify"))
    (tmp_path / "a.txt").write_text("ai", encoding="utf-8")
    await db.commit()
    result = await rs.rollback_turn(db, turn_id=turn.id)
    assert not result["ok"]
    assert (tmp_path / "a.txt").read_text() == "ai"
    await db.refresh(msg)
    assert not msg.deleted


async def test_later_workflow_and_detached_subagent_are_stopped(db, tmp_path, monkeypatch):
    import asyncio
    from app.orchestration import engine
    from app.orchestration.workflow import service as wf_service
    from app.persistence.models.workflow import WorkflowRun
    sess = Session(title="background", worktree_path=str(tmp_path))
    db.add(sess)
    await db.flush()
    before, _ = await make_turn(db, sess, "before")
    target, _ = await make_turn(db, sess, "target")
    earlier_program = WorkflowRun(session_id=sess.id, origin_turn_id=before.id, status="running")
    later_program = WorkflowRun(session_id=sess.id, origin_turn_id=target.id, status="running")
    db.add_all([earlier_program, later_program])
    await db.commit()
    task = asyncio.create_task(asyncio.sleep(60))
    handle = SimpleNamespace(turn_id=target.id, task=task, cancel_requested=False)
    manager = SimpleNamespace(_handles={1: handle})
    runtime = SimpleNamespace(_tasks={}, _meta={}, cancel=AsyncMock())
    monkeypatch.setattr(engine, "peek_subagent_manager", lambda _: manager)
    monkeypatch.setattr(engine, "cancel_turn", AsyncMock(return_value=True))
    monkeypatch.setattr(wf_service, "get_runtime", lambda: runtime)
    result = await rs.rollback_turn(db, turn_id=target.id)
    assert result["ok"] and task.cancelled() and handle.cancel_requested
    runtime.cancel.assert_awaited_once_with(later_program.id)


def test_binary_cross_round_chain_uses_checkpoint_bytes(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"v2\x00")
    (tmp_path / "first.backup").write_bytes(b"v0\x00")
    (tmp_path / "second.backup").write_bytes(b"v1\x00")
    import hashlib
    writes = [record(1, None, None, binary=True, digest=hashlib.sha1(b"v1\x00").hexdigest()),
              record(2, None, None, binary=True, digest=hashlib.sha1(b"v2\x00").hexdigest())]
    checkpoints = {1: "first.backup", 2: "second.backup"}
    plan = rs.build_plan(str(tmp_path), writes, lambda r: checkpoints[r.turn_id])
    assert not plan.conflicts and rs._apply_plan_sync(plan)["ok"]
    assert (tmp_path / "a.txt").read_bytes() == b"v0\x00"

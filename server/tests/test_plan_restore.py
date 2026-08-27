"""plan 模式执行后恢复测试（plan-88 任务 E）。

覆盖：
- session_service.update_session：权限从 plan 切出置 plan_restore_after_turn 标记，
  切回 plan/readonly 清标记。
- engine._maybe_restore_plan_mode：有标记时恢复 permission_mode=plan 并广播；
  无标记时跳过（幂等）。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.persistence.database import Base
from app.persistence.models import Message, Session, Turn  # noqa: F401  注册全部模型
from app.services import session_service


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


async def _mk_session(db, *, permission_mode="plan"):
    s = Session(project_id=None, permission_mode=permission_mode, plan_restore_after_turn=False)
    db.add(s)
    await db.flush()
    return s


# ── update_session 标记管理 ──


async def test_update_plan_to_accept_edits_sets_restore_flag(db):
    s = await _mk_session(db)
    await session_service.update_session(db, s.id, permission_mode="accept_edits")
    await db.refresh(s)
    assert s.permission_mode == "accept_edits"
    assert s.plan_restore_after_turn is True


async def test_update_plan_to_default_sets_restore_flag(db):
    s = await _mk_session(db)
    await session_service.update_session(db, s.id, permission_mode="default")
    await db.refresh(s)
    assert s.plan_restore_after_turn is True


async def test_update_back_to_plan_clears_flag(db):
    s = await _mk_session(db, permission_mode="accept_edits")
    s.plan_restore_after_turn = True
    await db.flush()
    await session_service.update_session(db, s.id, permission_mode="plan")
    await db.refresh(s)
    assert s.permission_mode == "plan"
    assert s.plan_restore_after_turn is False


async def test_update_readonly_clears_flag(db):
    s = await _mk_session(db, permission_mode="accept_edits")
    s.plan_restore_after_turn = True
    await db.flush()
    await session_service.update_session(db, s.id, permission_mode="readonly")
    await db.refresh(s)
    assert s.plan_restore_after_turn is False


async def test_update_unrelated_fields_keeps_flag(db):
    s = await _mk_session(db, permission_mode="accept_edits")
    s.plan_restore_after_turn = True
    await db.flush()
    await session_service.update_session(db, s.id, title="重命名")
    await db.refresh(s)
    assert s.plan_restore_after_turn is True


# ── _maybe_restore_plan_mode ──


async def test_restore_plan_mode_after_execution(db, monkeypatch):
    from app.orchestration import engine

    broadcasted = []
    async def _fake_broadcast(session_id, payload):
        broadcasted.append((session_id, payload))

    monkeypatch.setattr(engine, "broadcast", _fake_broadcast)
    s = await _mk_session(db, permission_mode="accept_edits")
    s.plan_restore_after_turn = True
    await db.commit()

    ok = await engine._maybe_restore_plan_mode(db, s)
    assert ok is True
    await db.refresh(s)
    assert s.permission_mode == "plan"
    assert s.plan_restore_after_turn is False
    assert any(p.get("event") == "session.updated"
               and p.get("payload", {}).get("permission_mode") == "plan"
               for _, p in broadcasted)


async def test_restore_skipped_without_flag(db, monkeypatch):
    from app.orchestration import engine

    monkeypatch.setattr(engine, "broadcast", lambda *a, **k: None)
    s = await _mk_session(db, permission_mode="accept_edits")
    await db.commit()
    ok = await engine._maybe_restore_plan_mode(db, s)
    assert ok is False
    await db.refresh(s)
    assert s.permission_mode == "accept_edits"  # 未被误恢复

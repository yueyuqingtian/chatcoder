"""plan-537-2256：记忆服务层的回归测试。

覆盖上一轮审查确认的缺陷：
- 开关必须挡住唯一写入口（save_memories）；
- 去重按目标 scope 分组（global 跨会话只留一条）；超长文本用截断后的文本去重；
- scope=project 但没有项目时降级为会话级；
- promote 到 project 时按来源会话反查 project_id，反查不到报可读错误；降级/提升到 global 时清 project_id；
- 注入（load_memories）不再改写 usage_count —— 「被看到」不等于「被使用」；
- consolidate_memories 合并重复项且幂等；
- 候选阈值：importance < 0.65 落候选且不注入；
- 删除会话只删该会话私有记忆，project/global 保留。

独立临时库 + write_engine.configure：save/consolidate/delete 都走单写线程，
必须与该库同源，否则写引擎会操作另一份库（沿用 test_session_permanent_delete.py 的做法）。
"""
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models import MemoryEntry, Project, Session  # noqa: F401 注册全部模型
from app.services import memory_service, session_service


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/memory.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


async def _seed_session(db, *, project_id: int | None = None, title: str = "s") -> int:
    row = Session(project_id=project_id, title=title)
    db.add(row)
    await db.flush()
    sid = row.id
    await db.commit()
    return sid


async def _count(db) -> int:
    res = await db.execute(select(func.count()).select_from(MemoryEntry))
    return int(res.scalar() or 0)


async def _save(db, sid: int, text: str, *, scope: str = "session",
                importance: float = 1.0, project_id: int | None = None) -> int:
    n = await memory_service.save_memories(
        db, session_id=sid, turn_id=None,
        memories=[{"text": text, "kind": "fact", "scope": scope, "importance": importance}],
        project_id=project_id,
    )
    await db.commit()
    return n


@pytest.mark.asyncio
async def test_switch_off_blocks_save(db, monkeypatch):
    """开关关闭时，唯一写入口直接返回 0（此前只有自动提取一处检查）。"""
    from app.core.config import settings
    monkeypatch.setattr(settings, "auto_memory_enabled", False, raising=False)

    sid = await _seed_session(db)
    n = await _save(db, sid, "这是一条足够长的记忆内容用于测试")
    assert n == 0
    assert await _count(db) == 0


@pytest.mark.asyncio
async def test_global_dedup_across_sessions(db):
    """global 去重跨会话生效：两个会话写同一内容只留一条。"""
    a = await _seed_session(db, title="a")
    b = await _seed_session(db, title="b")
    text = "用户偏好使用中文回复并且不要使用表情符号"

    assert await _save(db, a, text, scope="global") == 1
    assert await _save(db, b, text, scope="global") == 0
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_long_text_dedup_uses_truncated_text(db):
    """超过 500 字的文本：第二次写入应命中去重（去重键必须用截断后的文本）。"""
    sid = await _seed_session(db)
    long_text = "这是一条很长的记忆内容" + "细节" * 400  # 远超 500 字

    assert await _save(db, sid, long_text) == 1
    assert await _save(db, sid, long_text) == 0
    assert await _count(db) == 1
    row = (await db.execute(select(MemoryEntry))).scalars().first()
    assert len(row.text) == 500


@pytest.mark.asyncio
async def test_project_scope_without_project_degrades_to_session(db):
    """scope=project 但调用方没给 project_id → 降级为会话级，不写脏数据。"""
    sid = await _seed_session(db)
    assert await _save(db, sid, "项目约定：所有接口必须写幂等处理", scope="project", project_id=None) == 1

    row = (await db.execute(select(MemoryEntry))).scalars().first()
    assert row.scope == "session"
    assert row.project_id is None


@pytest.mark.asyncio
async def test_promote_project_resolves_from_source_session(db):
    """提升为项目记忆时不传 project_id：按来源会话反查（修「必然 400」）。"""
    proj = Project(name="p", path="/tmp/p_resolve")
    db.add(proj)
    await db.flush()
    pid = proj.id
    sid = await _seed_session(db, project_id=pid)
    await _save(db, sid, "本项目统一使用 PowerShell 7 执行命令")

    mid = (await db.execute(select(MemoryEntry.id))).scalar()
    ok = await memory_service.promote_memory(db, mid, "project", None)
    await db.commit()
    assert ok is True

    row = (await db.execute(select(MemoryEntry).where(MemoryEntry.id == mid))).scalars().first()
    assert row.scope == "project"
    assert row.project_id == pid
    assert row.candidate is False
    assert row.expires_at is None


@pytest.mark.asyncio
async def test_promote_project_without_project_raises(db):
    """来源会话未绑定项目 → 抛可读错误，而不是 400 无提示。"""
    sid = await _seed_session(db, project_id=None)
    await _save(db, sid, "一条来自无项目会话的记忆内容")
    mid = (await db.execute(select(MemoryEntry.id))).scalar()

    with pytest.raises(ValueError) as exc:
        await memory_service.promote_memory(db, mid, "project", None)
    assert "项目" in str(exc.value)


@pytest.mark.asyncio
async def test_promote_to_global_clears_project_id(db):
    """项目记忆提升为全局后清掉 project_id，避免脏组合。"""
    proj = Project(name="p2", path="/tmp/p_clear")
    db.add(proj)
    await db.flush()
    pid = proj.id
    sid = await _seed_session(db, project_id=pid)
    await _save(db, sid, "项目级记忆用于测试提升到全局", scope="project", project_id=pid)

    mid = (await db.execute(select(MemoryEntry.id))).scalar()
    assert await memory_service.promote_memory(db, mid, "global", None) is True
    await db.commit()

    row = (await db.execute(select(MemoryEntry).where(MemoryEntry.id == mid))).scalars().first()
    assert row.scope == "global"
    assert row.project_id is None


@pytest.mark.asyncio
async def test_load_memories_does_not_touch_usage_count(db):
    """注入不算使用：load_memories 不改 usage_count（此前每轮 +1 导致头部永久锁位）。"""
    sid = await _seed_session(db)
    await _save(db, sid, "注入不应增加使用计数的记忆内容")

    for _ in range(3):
        entries = await memory_service.load_memories(db, sid)
        assert len(entries) == 1

    row = (await db.execute(select(MemoryEntry))).scalars().first()
    assert (row.usage_count or 0) == 0


@pytest.mark.asyncio
async def test_consolidate_merges_duplicates_and_is_idempotent(db):
    """同作用域完全相同的两行 → 合并一条，二次执行不再变更（幂等）。"""
    sid = await _seed_session(db)
    text = "同一作用域内的完全重复记忆内容"
    # 绕过 save_memories 的去重，直接造重复行（模拟历史遗留数据）
    db.add(MemoryEntry(session_id=sid, text=text, kind="fact", scope="session", usage_count=1))
    db.add(MemoryEntry(session_id=sid, text=text, kind="fact", scope="session", usage_count=5))
    await db.commit()
    assert await _count(db) == 2

    first = await memory_service.consolidate_memories(db, session_id=sid, project_id=None)
    await db.commit()
    assert first["merged"] == 1

    alive = (await db.execute(
        select(MemoryEntry).where(MemoryEntry.superseded_by.is_(None))
    )).scalars().all()
    assert len(alive) == 1
    assert (alive[0].usage_count or 0) == 5  # 保留热度更高的那条

    second = await memory_service.consolidate_memories(db, session_id=sid, project_id=None)
    await db.commit()
    assert second["merged"] == 0


@pytest.mark.asyncio
async def test_candidate_below_threshold_not_injected(db):
    """importance < 0.65 落候选：可查但不参与注入。"""
    sid = await _seed_session(db)
    await _save(db, sid, "低置信度的候选记忆内容不应被注入", importance=0.5)

    row = (await db.execute(select(MemoryEntry))).scalars().first()
    assert row.candidate is True
    assert await memory_service.load_memories(db, sid) == []

    listed = await memory_service.list_memories(db, session_id=sid, include_candidate=True)
    assert len(listed) == 1


@pytest.mark.asyncio
async def test_delete_session_keeps_cross_scope_memories(db):
    """删除会话只删它的会话级记忆；project/global 是跨会话资产，必须保留。"""
    proj = Project(name="p3", path="/tmp/p_delete")
    db.add(proj)
    await db.flush()
    pid = proj.id
    sid = await _seed_session(db, project_id=pid)
    other = await _seed_session(db, project_id=pid, title="other")

    await _save(db, sid, "本会话私有的记忆内容应当被删除")
    await _save(db, sid, "跨会话的项目约定必须保留下来", scope="project", project_id=pid)
    await _save(db, sid, "跨项目的全局规范必须保留下来", scope="global")

    await session_service.delete_session_permanent(db, sid)
    await db.commit()

    scopes = sorted(
        (r.scope or "session") for r in
        (await db.execute(select(MemoryEntry))).scalars().all()
    )
    assert scopes == ["global", "project"]

    # 存活下来的记忆仍可被同项目的另一个会话读到
    entries = await memory_service.load_memories(db, other, project_id=pid)
    assert {e.scope for e in entries} == {"project", "global"}

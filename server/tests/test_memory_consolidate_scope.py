"""plan-549-2281：consolidate_memories 规则二的作用域边界单测。

守护点（对应「会话记忆过期」这条规则）：
- 整理会话 A 时，只能把 **A 自己**的过期会话记忆置 `superseded_by=-1`；
  会话 B 的过期记忆必须原样保留（此前查询只按 scope 过滤，一次整理会把全库
  所有过期会话记忆统一打标，跨会话污染且是无界全库写）；
- 规则一的合并同样不跨会话（回归守卫：两条同文本记忆分属两会话时不得合并）；
- 未过期的会话记忆、以及项目/全局作用域的记忆不受规则二影响。

隔离方式与 test_memory_scope.py 一致：每个用例用 `tmp_path` 下的独立库，并把写引擎、
`memory_index` 的库路径一起指向它（`consolidate_memories` 走 `run_write_locked`，
即 write_engine 的写通道，URL 必须同源）。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.persistence import write_engine
from app.persistence.database import Base
from app.persistence.models import MemoryEntry, Project, Session  # noqa: F401 注册全部模型
from app.services import memory_index, memory_service


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    """覆盖 conftest 的全局库隔离 fixture：本文件只用独立临时库（见下方 env）。

    conftest 的同名 fixture 会为每个用例 drop/create 全量表；与 env 里的写引擎短连接
    交错时偶发 `no such table` / `table ... already exists`（与本文件断言无关）。
    """
    yield


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """独立临时库：表、写引擎、FTS 索引路径全部同源。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/memory_consolidate.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    write_engine.configure(db_url, foreign_keys=False)
    monkeypatch.setattr(settings, "database_url", db_url)
    memory_index.reset_probe_cache()
    try:
        yield {"factory": factory, "engine": engine, "url": db_url}
    finally:
        memory_index.reset_probe_cache()
        write_engine.configure(None)
        await engine.dispose()


async def _seed_session(env, project_id: int | None = None, title: str = "s") -> int:
    async with env["factory"]() as db:
        row = Session(project_id=project_id, title=title)
        db.add(row)
        await db.commit()
        return int(row.id)


async def _seed_project(env, name: str) -> int:
    async with env["factory"]() as db:
        row = Project(name=name, path=f"D:/ws/{name}")
        db.add(row)
        await db.commit()
        return int(row.id)


async def _add_entry(
    env, *, session_id: int | None, text: str, scope: str = "session",
    expires_at: str | None = None, project_id: int | None = None,
    usage_count: int = 0, kind: str = "fact",
) -> int:
    """直接落库（绕开 save_memories）：需要精确控制过期时间与作用域。"""
    async with env["factory"]() as db:
        row = MemoryEntry(
            session_id=session_id, turn_id=None, text=text, kind=kind, scope=scope,
            project_id=project_id, candidate=False, expires_at=expires_at,
            usage_count=usage_count,
        )
        db.add(row)
        await db.commit()
        return int(row.id)


async def _entries(env) -> list[MemoryEntry]:
    async with env["factory"]() as db:
        res = await db.execute(select(MemoryEntry))
        return list(res.scalars().all())


def _expired() -> str:
    """明确早于现在的 ISO 时间（2020 年，避免依赖 TTL 常量）。"""
    return "2020-01-01T00:00:00+00:00"


def _future() -> str:
    return "2099-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_expired_rule_does_not_touch_other_sessions(env):
    """核心守卫：整理 A 只打标 A 的过期记忆，B 的过期记忆必须原样保留。"""
    session_a = await _seed_session(env, title="a")
    session_b = await _seed_session(env, title="b")
    id_a = await _add_entry(env, session_id=session_a, text="A 的过期会话记忆", expires_at=_expired())
    id_b = await _add_entry(env, session_id=session_b, text="B 的过期会话记忆", expires_at=_expired())

    async with env["factory"]() as db:
        result = await memory_service.consolidate_memories(db, session_id=session_a)

    assert result["expired"] == 1, "只应处理本会话的一条过期记忆"

    by_id = {e.id: e for e in await _entries(env)}
    assert by_id[id_a].superseded_by == -1, "本会话的过期记忆应被移出有效集"
    assert by_id[id_b].superseded_by is None, "其它会话的过期记忆被跨会话打标（规则二缺 session_id 过滤）"


@pytest.mark.asyncio
async def test_expired_rule_keeps_unexpired_and_cross_layer_memories(env):
    """未过期的会话记忆、以及 project/global 作用域的记忆都不被规则二影响。"""
    project_id = await _seed_project(env, "cp")
    session_id = await _seed_session(env, project_id=project_id)
    live = await _add_entry(env, session_id=session_id, text="未过期的会话记忆", expires_at=_future())
    no_ttl = await _add_entry(env, session_id=session_id, text="无过期时间的会话记忆")
    project_mem = await _add_entry(
        env, session_id=session_id, text="项目记忆不过期", scope="project",
        project_id=project_id,
    )
    global_mem = await _add_entry(
        env, session_id=session_id, text="全局记忆不过期", scope="global",
    )

    async with env["factory"]() as db:
        result = await memory_service.consolidate_memories(db, session_id=session_id)

    assert result["expired"] == 0
    by_id = {e.id: e for e in await _entries(env)}
    for entry_id in (live, no_ttl, project_mem, global_mem):
        assert by_id[entry_id].superseded_by is None


@pytest.mark.asyncio
async def test_merge_rule_still_scoped_to_one_session(env):
    """回归守卫：文本完全相同的两条会话记忆分属两个会话时不得合并。"""
    session_a = await _seed_session(env, title="a")
    session_b = await _seed_session(env, title="b")
    same_text = "同一条事实：构建产物目录已加入 .gitignore"
    await _add_entry(env, session_id=session_a, text=same_text, expires_at=_future())
    await _add_entry(env, session_id=session_b, text=same_text, expires_at=_future())

    async with env["factory"]() as db:
        result = await memory_service.consolidate_memories(db, session_id=session_a)

    assert result["merged"] == 0
    assert all(e.superseded_by is None for e in await _entries(env))


@pytest.mark.asyncio
async def test_merge_rule_merges_duplicates_inside_session(env):
    """正向守卫：同一会话内的重复文本仍然合并（收紧作用域不能把去重也关掉）。"""
    session_id = await _seed_session(env)
    keep = await _add_entry(env, session_id=session_id, text="重复的事实条目", usage_count=5)
    drop = await _add_entry(env, session_id=session_id, text="重复的事实条目", usage_count=1)

    async with env["factory"]() as db:
        result = await memory_service.consolidate_memories(db, session_id=session_id)

    assert result["merged"] == 1
    by_id = {e.id: e for e in await _entries(env)}
    assert by_id[keep].superseded_by is None
    assert by_id[drop].superseded_by == keep, "usage_count 高者保留，低者指向它"


@pytest.mark.asyncio
async def test_consolidate_is_idempotent(env):
    """重复整理不产生新变更（文档承诺的幂等性）。"""
    session_a = await _seed_session(env, title="a")
    session_b = await _seed_session(env, title="b")
    await _add_entry(env, session_id=session_a, text="A 的过期会话记忆", expires_at=_expired())
    await _add_entry(env, session_id=session_b, text="B 的过期会话记忆", expires_at=_expired())

    async with env["factory"]() as db:
        first = await memory_service.consolidate_memories(db, session_id=session_a)
        second = await memory_service.consolidate_memories(db, session_id=session_a)
        third = await memory_service.consolidate_memories(db, session_id=session_b)

    assert first["expired"] == 1
    assert second == {"merged": 0, "expired": 0}, "已打标的行不再参与判断"
    assert third["expired"] == 1, "B 自己的过期记忆仍应由 B 的整理处理"

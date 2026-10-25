"""plan-548-2276 阶段 0：记忆作用域隔离与迁移的单测。

覆盖验收 1/2 项：
- 会话 A 写的 scope=session 记忆，在会话 B 的 memory_search 中不出现；
- 项目 P1 的 scope=project 记忆，在 P2 的会话中不出现；
- scope=global 记忆在所有会话可见；
- 会话未绑定项目时 project 层为空、不报错（写入自动降级为会话级）。

另含：会话→项目反查（resolve_project_id）的正常与容错路径，以及 keywords/origin
两列的迁移幂等性。

隔离方式：每个用例用 `tmp_path` 下的独立库，并把写引擎、`memory_index` 的库路径、
`memory_search` 的连接工厂一起指向它。不复用 conftest 的共享库：那份库在每个用例
前后 drop_all/create_all，与写引擎/索引的短连接交错时会偶发 `no such table`
（DDL 与 INSERT 的 schema 视图竞争），会让断言随机失败。
"""
import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.memory_search import MemorySearchTool
from app.persistence import write_engine
from app.persistence.database import Base
from app.persistence.models import MemoryEntry, Project, Session  # noqa: F401 注册全部模型
from app.services import memory_index, memory_service


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    """覆盖 conftest 的全局库隔离 fixture：本文件只用独立临时库（见下方 env）。

    conftest 的同名 fixture 为每个用例 drop/create 全量表；本文件的 env 同时把写引擎、
    FTS 索引、工具侧连接工厂指向各自的 tmp 库，两边并发操作同一个 sqlite 文件时，
    DDL（drop/create）与短连接的事务会交错，偶发 `no such table` / `table ... already
    exists` 的 teardown 报错（表名随机，与用例断言无关）。
    """
    yield


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """独立临时库：表、写引擎、FTS 索引路径、工具侧连接工厂全部同源。"""
    from app.orchestration.tools import memory_search as memory_search_tool

    db_url = f"sqlite+aiosqlite:///{tmp_path}/memory_scope.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    write_engine.configure(db_url, foreign_keys=False)
    monkeypatch.setattr(settings, "database_url", db_url)
    monkeypatch.setattr(memory_search_tool, "async_session_factory", factory)
    memory_index.reset_probe_cache()
    try:
        yield {"factory": factory, "engine": engine, "url": db_url}
    finally:
        memory_index.reset_probe_cache()
        write_engine.configure(None)
        await engine.dispose()


def _ctx(session_id: int) -> ToolContext:
    return ToolContext(
        workspace_root=".", session_id=session_id, task_id=1, agent_id=1, agent_name="test",
    )


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


async def _save(env, session_id: int, text: str, *, scope: str = "session",
                project_id: int | None = None, keywords: list[str] | None = None,
                origin: str = "manual") -> int:
    async with env["factory"]() as db:
        return await memory_service.save_memories(
            db, session_id=session_id, turn_id=None,
            memories=[{
                "text": text, "kind": "fact", "scope": scope,
                "importance": 1.0, "keywords": keywords,
            }],
            project_id=project_id, origin=origin,
        )


async def _search(env, session_id: int, keyword: str, **kwargs):
    tool = MemorySearchTool()
    return await tool.run({"keyword": keyword, "sources": "memory", **kwargs}, _ctx(session_id))


async def _all_entries(env) -> list[MemoryEntry]:
    async with env["factory"]() as db:
        res = await db.execute(select(MemoryEntry))
        return list(res.scalars().all())


@pytest.mark.asyncio
async def test_session_memory_invisible_to_other_session(env):
    """会话 A 的私有记忆不能被会话 B 检索到（改造前查询只有 text ILIKE，无会话条件）。"""
    session_a = await _seed_session(env, title="a")
    session_b = await _seed_session(env, title="b")
    assert await _save(
        env, session_a, "会话 A 的私有事实：部署脚本放在内网跳板机上",
        keywords=["部署", "跳板机"],
    ) == 1

    other = await _search(env, session_b, "部署脚本")
    assert other.ok is True
    assert "跳板机" not in other.output
    assert other.data["memory"] == 0

    own = await _search(env, session_a, "部署脚本")
    assert own.ok is True
    assert "跳板机" in own.output
    assert own.data["memory"] == 1


@pytest.mark.asyncio
async def test_project_memory_scoped_to_its_own_project(env):
    """项目 P1 的记忆在 P2 的会话里不可见，在 P1 的会话里可见。"""
    project_1 = await _seed_project(env, "p1")
    project_2 = await _seed_project(env, "p2")
    session_1 = await _seed_session(env, project_id=project_1, title="in-p1")
    session_2 = await _seed_session(env, project_id=project_2, title="in-p2")
    assert await _save(
        env, session_1, "订单状态机不允许跨过支付中这一步", scope="project",
        project_id=project_1, keywords=["订单", "状态机"],
    ) == 1

    other = await _search(env, session_2, "状态机")
    assert other.ok is True
    assert "订单状态机" not in other.output
    assert other.data["memory"] == 0

    own = await _search(env, session_1, "状态机")
    assert own.ok is True
    assert "订单状态机" in own.output
    assert own.data["memory"] == 1


@pytest.mark.asyncio
async def test_global_memory_visible_in_every_session(env):
    """scope=global 对所有会话可见（含未绑定项目的会话）。"""
    project_id = await _seed_project(env, "gp")
    session_1 = await _seed_session(env, project_id=project_id, title="with-project")
    session_2 = await _seed_session(env, title="no-project")
    assert await _save(env, session_1, "回复一律使用简体中文，不要中英混杂", scope="global") == 1

    for session_id in (session_1, session_2):
        result = await _search(env, session_id, "简体中文")
        assert result.ok is True
        assert "中文" in result.output
        assert result.data["memory"] == 1


@pytest.mark.asyncio
async def test_unbound_project_scope_degrades_to_session(env):
    """会话没有项目时：写入降级为会话级，检索不报错且能看到这条记忆。"""
    session_id = await _seed_session(env)
    assert await _save(
        env, session_id, "未绑定项目时写入的项目级记忆应降级为会话级", scope="project",
    ) == 1

    entries = await _all_entries(env)
    assert len(entries) == 1
    assert entries[0].scope == "session"
    assert entries[0].project_id is None

    result = await _search(env, session_id, "项目级记忆")
    assert result.ok is True
    assert result.data["memory"] == 1


@pytest.mark.asyncio
async def test_scopes_param_can_exclude_layers(env):
    """scopes 过滤生效：排除 session 层后，本会话记忆不再出现。"""
    session_id = await _seed_session(env)
    assert await _save(env, session_id, "这条记忆只应出现在会话层的检索结果里") == 1

    with_session = await _search(env, session_id, "记忆只应出现")
    assert with_session.data["memory"] == 1

    without = await _search(env, session_id, "记忆只应出现", scopes="global")
    assert without.ok is True
    assert without.data["memory"] == 0


@pytest.mark.asyncio
async def test_resolve_project_id_reads_project_and_swallows_errors(env):
    """反查项目 id：正常返回所属项目；查询异常时返回 None 而不抛。"""
    project_id = await _seed_project(env, "rp")
    session_id = await _seed_session(env, project_id=project_id)

    async with env["factory"]() as db:
        assert await memory_service.resolve_project_id(db, session_id) == project_id

    class _BoomDB:
        async def get(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert await memory_service.resolve_project_id(_BoomDB(), session_id) is None
    assert await memory_service.resolve_project_id(None, None) is None


@pytest.mark.asyncio
async def test_migrations_add_keywords_and_origin_idempotently(tmp_path):
    """旧库补列：一次性加上 keywords/origin，重复执行不再产生变更也不报错。"""
    from app.persistence.migrations import _MIGRATIONS, run_migrations

    declared = {(table, column) for table, column, _ in _MIGRATIONS}
    assert ("memory_entries", "keywords") in declared
    assert ("memory_entries", "origin") in declared

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/old.db")
    async with engine.begin() as conn:
        # 模拟改造前的旧表：只有历史字段
        await conn.execute(sa_text(
            "CREATE TABLE memory_entries ("
            "id INTEGER PRIMARY KEY, session_id BIGINT, text TEXT NOT NULL, "
            "kind VARCHAR(20), usage_count INTEGER, scope VARCHAR(12))"
        ))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        first = await run_migrations(db)
        second = await run_migrations(db)
    async with engine.connect() as conn:
        rows = (await conn.execute(sa_text("PRAGMA table_info(memory_entries)"))).fetchall()
    await engine.dispose()

    columns = {row[1] for row in rows}
    assert {"keywords", "origin"} <= columns
    assert first["errors"] == 0
    assert second["errors"] == 0
    # 第二次执行时两列已存在，必须走 skipped 而不是再 ALTER 一次
    assert second["migrated"] <= first["migrated"]


# ── plan-549-2281：记忆表重建中断后的半态恢复 ──


@pytest.mark.asyncio
async def test_memory_table_half_rebuilt_state_is_recovered(tmp_path, monkeypatch):
    """主表已 DROP、临时表存有全部数据时，下一次迁移必须把它接回主表。

    真实场景：`_memory_session_nullable` 的 SQLite 重建是四步（建临时表 → 拷数据 →
    DROP 主表 → RENAME）；在 DROP 之后、RENAME 之前进程被杀，主表就没了，而临时表持有
    全部记忆。旧实现在 `_table_exists` 上直接 return False，数据永不恢复也不清理。
    """
    from app.persistence.migrations import _MIGRATIONS, _memory_session_nullable

    declared = {(table, column) for table, column, _ in _MIGRATIONS}
    assert ("memory_entries", "keywords") in declared  # 与本文件其它用例保持同一口径

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/half.db")
    async with engine.begin() as conn:
        # 模拟半态：主表不存在，临时表持有数据与索引
        await conn.execute(sa_text(
            "CREATE TABLE memory_entries__nullable_tmp ("
            "id INTEGER PRIMARY KEY, session_id BIGINT, text TEXT NOT NULL, "
            "kind VARCHAR(20), scope VARCHAR(12))"
        ))
        await conn.execute(sa_text(
            "CREATE INDEX ix_tmp_session ON memory_entries__nullable_tmp (session_id)"
        ))
        await conn.execute(sa_text(
            "INSERT INTO memory_entries__nullable_tmp (id, session_id, text, kind, scope) "
            "VALUES (1, 10, '幸存的记忆条目', 'fact', 'session')"
        ))

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        # 先跑一次 helper：恢复分支应在 _table_exists 判定之前把表接回来
        await _memory_session_nullable(db)

    async with engine.connect() as conn:
        tables = {r[0] for r in (await conn.execute(sa_text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))).fetchall()}
        rows = (await conn.execute(sa_text(
            "SELECT id, text FROM memory_entries"
        ))).fetchall()
        indexes = {r[0] for r in (await conn.execute(sa_text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memory_entries'"
        ))).fetchall()}

    # 幂等：「恢复后再次执行」不得报错，也不得重复变更
    async with factory() as db:
        await _memory_session_nullable(db)
    await engine.dispose()

    assert "memory_entries" in tables, "临时表未被接回主表"
    assert "memory_entries__nullable_tmp" not in tables, "临时表应已改名而非留存两份"
    assert [tuple(r) for r in rows] == [(1, "幸存的记忆条目")], "恢复后数据不一致"
    assert indexes, "恢复后索引未重放"


# ── plan-549-2281 复盘修复：去重合并的守卫（防「同主题不同事」被误并） ──
# 旧口径「关键词交集 ≥2 即合并」在关键词大量来自自动派生时会把语义不同的条目
# 静默合并（原文本被更长的一条覆盖，不可逆）；收紧为「交集 ≥2 且覆盖较小集合
# 一半以上且文本长度接近」。这里同时给正反两个方向兜底。


@pytest.mark.asyncio
async def test_same_text_merges_into_one_entry(env):
    sid = await _seed_session(env)
    n1 = await _save(env, sid, "上下文压缩：摘要不短于原文时会放弃压缩",
                     keywords=["压缩", "摘要"])
    n2 = await _save(env, sid, "上下文压缩：摘要不短于原文时会放弃压缩",
                     keywords=["压缩", "摘要"])
    assert (n1, n2) == (1, 0)  # 第二次并入既有行，不新增
    assert len(await _all_entries(env)) == 1


@pytest.mark.asyncio
async def test_same_topic_different_fact_not_merged(env):
    """负向守卫：两条主题相近（共享 2 个关键词）但结论不同的记忆不得合并。"""
    sid = await _seed_session(env)
    await _save(
        env, sid,
        "上下文压缩后细节会归档进会话记忆，检索时用「压缩 原因」能找回当时结论。",
        keywords=["上下文", "压缩", "检索", "细节", "会话记忆"],
    )
    await _save(
        env, sid,
        "上下文压缩的触发阈值默认 0.90，配置低于 0.50 会被夹紧回下限。",
        keywords=["上下文", "压缩", "阈值", "配置", "夹紧"],
    )
    entries = await _all_entries(env)
    assert len(entries) == 2, "交集未过半（2/5）时不得合并——否则原文本被静默覆盖"
    texts = {e.text for e in entries}
    assert any("会话记忆" in t for t in texts)
    assert any("0.90" in t for t in texts)


@pytest.mark.asyncio
async def test_rephrased_memory_with_high_overlap_merges(env):
    """同一件事换个说法（关键词高度重合）仍应合并——收紧判据不能误伤正常去重。"""
    sid = await _seed_session(env)
    await _save(
        env, sid,
        "构建产物目录 dist/ 已加入 .gitignore，不要把构建产物提交进仓库。",
        keywords=["构建", "gitignore", "dist", "提交"],
    )
    n = await _save(
        env, sid,
        "dist/ 构建产物目录已列入 .gitignore，提交前不需要先清理产物。",
        keywords=["构建", "gitignore", "dist", "提交"],
    )
    assert n == 0
    assert len(await _all_entries(env)) == 1

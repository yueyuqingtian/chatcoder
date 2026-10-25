"""memory_search 翻页与真实命中数的回归用例。

缺陷原貌（三条同时存在，缺一条都不会「翻页失效」）：
1. 三级召回各自 `limit(_CANDIDATE_POOL=60)`，候选池并集上界 180；
2. `result["total"] = len(ordered)`——报的是**池大小**而非真实命中数；
3. 页尾提示写「共 N+ 条（命中超出候选池，仅统计前 N 条）」却引导继续 `offset` 翻页，
   于是 `offset=180` 之后必然返回空，模型只能反复加大 offset 空转。

本文件钉住修复后的口径：
- `total` 等于**真实命中数**（`count(*)`，与 text 子串过滤同源），不受池上限影响；
- 第 2 页能拿到条目（池扩大到 200 后 offset=180 仍在范围内）；
- 超出可翻页上限时返回明确的「已到可检索上限」提示，而不是静默空页；
- 同一查询两次翻页不重复、不遗漏。

隔离方式与 `test_memory_search_rank.py` 一致：`tmp_path` 下独立库 + 独立写引擎 +
`memory_index` 路径，**不复用 conftest 的共享库**（那份库在每个用例前后 drop/create，
与写引擎、索引短连接交错时会偶发 `no such table` 的假失败）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.orchestration.tools import memory_search as memory_search_tool
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.memory_search import MemorySearchTool
from app.persistence import write_engine
from app.persistence.database import Base
from app.persistence.models import MemoryEntry, Session  # noqa: F401 注册全部模型
from app.services import memory_index, memory_service

# 造多少条匹配条目：必须 > 旧池上界 180，才能复现「第 181 条之后翻不到」
_SEED_COUNT = 220
# 所有种子条目共用的检索词，保证三级的过滤条件都能命中它们
_KEYWORD = "翻页上限回归"


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    """覆盖 conftest 的全局库隔离 fixture（本文件用 `env` 自建独立库）。"""
    yield


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """独立临时库：表、写引擎、FTS 索引路径、工具侧连接工厂全部同源。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/memory_paging.db"
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


async def _seed(env, count: int = _SEED_COUNT) -> tuple[int, int]:
    """造 count 条都命中 `_KEYWORD` 的会话记忆，返回 (session_id, 真实命中数)。

    直接走 ORM 批量插入（不走 `save_memories` 的去重逻辑）：本用例要的是**条数**，
    去重会把近似文本合并掉；keywords 里显式带上检索词，保证一级召回也命中。
    """
    async with env["factory"]() as db:
        row = Session(title="paging")
        db.add(row)
        await db.commit()
        session_id = int(row.id)

        db.add_all([
            MemoryEntry(
                text=f"{_KEYWORD} 第 {index} 条：分页测试数据",
                kind="fact", scope="session", session_id=session_id,
                keywords=f'["{_KEYWORD}", "条目{index}"]',
                origin="manual",
            )
            for index in range(count)
        ])
        await db.commit()
        total = len((await db.execute(select(MemoryEntry.id))).scalars().all())
    return session_id, total


async def _search(session_id: int, **kwargs):
    return await MemorySearchTool().run(
        {"keyword": _KEYWORD, "sources": "memory", **kwargs}, _ctx(session_id),
    )


def _memory_lines(result) -> list[str]:
    """结果里记忆库段的条目行（排除标题、提示与页面说明）。"""
    return [ln for ln in result.output.splitlines() if ln.startswith("- [")]


@pytest.mark.asyncio
async def test_total_is_real_hit_count_not_pool_size(env):
    """① `total` 必须是真实命中数，等于库里实际匹配条数（旧实现报的是池上界）。"""
    session_id, seeded = await _seed(env)
    assert seeded == _SEED_COUNT

    result = await _search(session_id, limit=10, offset=0)

    assert result.ok is True
    assert result.data["memory_total"] == seeded, (
        f"total 应等于真实命中数 {seeded}，实际 {result.data['memory_total']}"
    )
    assert result.data["memory_total_exact"] is True


@pytest.mark.asyncio
async def test_second_page_beyond_old_pool_boundary_returns_rows(env):
    """② offset 翻到旧池上界（180）之后仍能拿到条目，而不是静默空页。"""
    session_id, seeded = await _seed(env)
    assert seeded > 180, "种子数据必须超过旧候选池上界 180 才能复现缺陷"

    page = await _search(session_id, limit=10, offset=180)

    assert page.ok is True
    lines = _memory_lines(page)
    assert lines, f"offset=180 翻页为空（旧实现池上界 180 的真实表现）：{page.output[:200]}"
    assert page.data["memory"] == len(lines)


@pytest.mark.asyncio
async def test_page_beyond_cap_reports_explicit_limit(env):
    """② offset 超过可翻页上限：`ok=True` 但文案明确说明「已到可检索上限」。"""
    session_id, _seeded = await _seed(env)

    page = await _search(session_id, limit=10, offset=memory_search_tool._MAX_OFFSET)

    assert page.ok is True
    assert page.data["memory"] == 0
    assert "已到可检索上限" in page.output or "已超出可检索上限" in page.output, (
        f"超限页必须给出明确说明，实际输出：{page.output[:300]}"
    )
    # 真实命中数仍如实给出，便于模型判断「是翻过头了」而不是「没有这条记忆」
    assert page.data["memory_total"] == _SEED_COUNT


@pytest.mark.asyncio
async def test_two_pages_do_not_overlap_or_lose_rows(env):
    """③ 同一查询两次翻页不重复、不遗漏。"""
    session_id, _seeded = await _seed(env)

    first = await _search(session_id, limit=20, offset=0)
    second = await _search(session_id, limit=20, offset=20)

    lines_first = _memory_lines(first)
    lines_second = _memory_lines(second)
    assert len(lines_first) == 20
    assert len(lines_second) == 20

    overlap = set(lines_first) & set(lines_second)
    assert not overlap, f"两页出现重复条目：{list(overlap)[:2]}"
    assert len(set(lines_first + lines_second)) == 40, "两页合计应为 40 条不重复条目"


@pytest.mark.asyncio
async def test_paging_hint_names_the_reachable_cap(env):
    """页尾提示必须写清「最多能翻到第几条」，不能给出无效的 offset 指引。"""
    session_id, _seeded = await _seed(env)

    result = await _search(session_id, limit=10, offset=0)

    assert "翻页用 offset=10" in result.output
    # 候选池（每级 200）打满，真实命中 220 > 池并集，故提示里要出现「最多只能翻到第 N 条」
    assert "最多只能翻到第" in result.output or "候选池" in result.output

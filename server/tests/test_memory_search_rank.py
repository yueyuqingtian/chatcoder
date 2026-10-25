"""plan-548-2276 阶段 1/2：记忆写入扩展与检索排序的单测。

覆盖验收 3 项与阶段 2 的写法要求：
- 中文查询「压缩后失忆」能命中关键词为「上下文压缩 / compaction」的记忆（同义词 + 滑窗）；
- 同义词表与切分函数的行为（纯函数断言，不碰数据库）；
- FTS5 不可用时回退到 keywords + LIKE，不报错；
- 注入侧 `load_memories(task_keywords=...)` 的相关性排在 usage_count 之前；
- 写入侧：keywords 落 JSON、origin 落库、keywords 为空自动派生；
- 去重扩展：关键词交集 ≥2 且文本接近 → 合并为一条并取并集；
- 检索结果的 scope/origin/日期标注与「需核对当前代码」提示、offset 翻页。

隔离方式：每个用例用 `tmp_path` 下的独立库，并把写引擎、`memory_index` 的库路径、
`memory_search` 的连接工厂一起指向它。**不复用 conftest 的共享库**——那份库在每个
用例前后 drop_all/create_all，与写引擎/索引的短连接交错时会偶发 `no such table`
（DDL 与 INSERT 的 schema 视图竞争），会把真实行为掩盖成随机失败。
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.memory_search import MemorySearchTool, expand_query
from app.persistence import write_engine
from app.persistence.database import Base
from app.persistence.models import MemoryEntry, Session  # noqa: F401 注册全部模型
from app.services import memory_index, memory_service, memory_synonyms


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    """覆盖 conftest 的全局库隔离 fixture：本文件用 `env` 自建独立库，不需要共享库。

    文件头已声明「不复用 conftest 的共享库」，但此前没有真正覆盖同名 fixture：
    conftest 仍会在每个用例前后对共享库 drop_all/create_all，与写引擎 / memory_index
    的短连接交错时报 `no such table`（表名随机），表现为与用例内容无关的 teardown error。
    """
    yield


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """独立临时库：表、写引擎、FTS 索引路径、工具侧连接工厂全部同源。"""
    from app.orchestration.tools import memory_search as memory_search_tool

    db_url = f"sqlite+aiosqlite:///{tmp_path}/memory_rank.db"
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


async def _seed_session(env, title: str = "rank") -> int:
    async with env["factory"]() as db:
        row = Session(title=title)
        db.add(row)
        await db.commit()
        return int(row.id)


async def _save(env, session_id: int, text: str, *, keywords: list[str] | None = None,
                scope: str = "session", origin: str = "manual") -> int:
    async with env["factory"]() as db:
        return await memory_service.save_memories(
            db, session_id=session_id, turn_id=None,
            memories=[{
                "text": text, "kind": "fact", "scope": scope,
                "importance": 1.0, "keywords": keywords,
            }],
            origin=origin,
        )


async def _entries(env) -> list[MemoryEntry]:
    async with env["factory"]() as db:
        res = await db.execute(select(MemoryEntry).order_by(MemoryEntry.id))
        return list(res.scalars().all())


async def _search(env, session_id: int, keyword: str, **kwargs):
    tool = MemorySearchTool()
    return await tool.run({"keyword": keyword, "sources": "memory", **kwargs}, _ctx(session_id))


# ───────────────────────── 同义词与切分（纯函数）─────────────────────────


def test_expand_terms_covers_groups_and_falls_back():
    """同义词组：命中返回整组，未命中返回自身。"""
    compaction_group = memory_synonyms.expand_terms("压缩")
    assert {"上下文压缩", "compaction", "checkpoint"} <= compaction_group

    # 英文大小写不敏感
    assert memory_synonyms.expand_terms("COMPACTION") == compaction_group

    assert memory_synonyms.expand_terms("完全不存在的词") == {"完全不存在的词"}


def test_tokenize_mixes_bigrams_and_windows():
    """中文切分同时给整块词与 2/3 字滑窗，英文小写并去停用词。"""
    tokens = memory_synonyms.tokenize("上下文压缩 the 记忆检索")
    assert "上下文压缩" in tokens
    assert "上下文" in tokens  # 3 字滑窗（FTS trigram 可用）
    assert "压缩" in tokens    # 2 字滑窗（LIKE 兜底）
    assert "记忆检索" in tokens
    assert "the" not in tokens
    assert len(tokens) <= memory_synonyms.MAX_TERMS

    # 只要整块词时不含滑窗碎片
    words = memory_synonyms.tokenize("记忆检索", with_windows=False)
    assert words == ["记忆检索"]


def test_expand_query_bridges_natural_chinese_to_stored_keywords():
    """查询改写：自然表述「压缩后失忆」能扩到「上下文压缩 / compaction」。"""
    terms = expand_query("压缩后失忆")
    assert "上下文压缩" in terms
    assert "compaction" in terms
    assert len(terms) <= 48


# ───────────────────────── 写入侧：keywords / origin / 去重 ─────────────────────────


@pytest.mark.asyncio
async def test_keywords_persisted_as_json_and_origin_recorded(env):
    """keywords 落 JSON 数组字符串，origin 按调用方传入落库。"""
    session_id = await _seed_session(env)
    assert await _save(
        env, session_id, "压缩后模型会忘掉早期细节，所以摘要必须短于原文",
        keywords=["上下文压缩", "摘要"], origin="extract",
    ) == 1

    entry = (await _entries(env))[0]
    assert json.loads(entry.keywords) == ["上下文压缩", "摘要"]
    assert entry.origin == "extract"


@pytest.mark.asyncio
async def test_keywords_derived_when_missing(env):
    """未给 keywords 时按正文兜底派生，保证字段非空。"""
    session_id = await _seed_session(env)
    assert await _save(env, session_id, "发布前必须跑完 typecheck 与 build 才允许打 tag") == 1

    entry = (await _entries(env))[0]
    assert entry.keywords
    assert json.loads(entry.keywords)
    assert entry.origin == "manual"


@pytest.mark.asyncio
async def test_dedup_by_keyword_overlap_merges_instead_of_inserting(env):
    """关键词交集 ≥2 且文本接近 → 合并到既有条目，不新增重复行。"""
    session_id = await _seed_session(env)
    first = "改 A 枚举时必须同步 B 的映射表"
    second = "修改 A 的枚举要同步 B 的映射表"
    assert await _save(env, session_id, first, keywords=["枚举", "同步", "映射"]) == 1
    assert await _save(env, session_id, second, keywords=["枚举", "同步", "A"]) == 0

    entries = await _entries(env)
    assert len(entries) == 1
    words = set(json.loads(entries[0].keywords))
    assert words == {"枚举", "同步", "映射", "A"}  # 关键词取并集
    assert entries[0].text in (first, second)


@pytest.mark.asyncio
async def test_dedup_does_not_merge_across_scopes(env):
    """同文本不同作用域是两条记忆（global 与 session 各有独立可见范围）。"""
    session_id = await _seed_session(env)
    assert await _save(env, session_id, "提交前必须跑 typecheck，这是全局约定",
                       scope="session") == 1
    assert await _save(env, session_id, "提交前必须跑 typecheck，这是全局约定",
                       scope="global") == 1

    entries = await _entries(env)
    assert {e.scope for e in entries} == {"session", "global"}


# ───────────────────────── 检索侧：相关性与回退 ─────────────────────────


@pytest.mark.asyncio
async def test_chinese_query_hits_stored_keywords(env):
    """验收 3：中文查询「压缩后失忆」命中关键词为「上下文压缩 / compaction」的记忆。"""
    session_id = await _seed_session(env)
    assert await _save(
        env, session_id,
        "上下文压缩之后模型会丢掉早期细节，所以 compaction 的摘要必须比原文短",
        keywords=["上下文压缩", "compaction", "摘要"],
    ) == 1
    assert await _save(env, session_id, "图片消息不参与压缩候选，这是既有取舍") == 1

    result = await _search(env, session_id, "压缩后失忆")
    assert result.ok is True
    assert result.data["memory"] >= 1
    assert "上下文压缩" in result.output


@pytest.mark.asyncio
async def test_fts_unavailable_falls_back_without_error(env, monkeypatch):
    """FTS5 不可用时只走 keywords/LIKE，仍能命中且不抛错。"""
    session_id = await _seed_session(env)
    assert await _save(
        env, session_id, "记忆检索的中文查询需要同义词扩展才能命中",
        keywords=["记忆检索", "同义词"],
    ) == 1

    monkeypatch.setattr(memory_index, "is_available", lambda: False)
    result = await _search(env, session_id, "同义词扩展")
    assert result.ok is True
    assert result.data["memory"] == 1
    assert result.data["fts"] is False


@pytest.mark.asyncio
async def test_fts_tier_participates_when_available(env):
    """FTS 可用时参与召回（数据库未命中 keywords 也能靠子串召回）。"""
    session_id = await _seed_session(env)
    assert await _save(env, session_id, "压缩块里记录了被遮蔽的原始消息与 compaction_id") == 1

    if not memory_index.is_available():
        pytest.skip("当前运行环境不支持 FTS5 trigram")

    result = await _search(env, session_id, "被遮蔽")
    assert result.ok is True
    assert result.data["memory"] == 1
    assert result.data["fts"] is True


@pytest.mark.asyncio
async def test_load_memories_ranks_by_task_keywords_before_usage(env):
    """注入侧：命中本轮任务关键词的记忆排在 usage_count 更高的无关记忆之前。"""
    session_id = await _seed_session(env)
    assert await _save(
        env, session_id, "上下文压缩的摘要必须短于原文，否则会放弃压缩",
        keywords=["上下文压缩", "摘要"],
    ) == 1
    assert await _save(env, session_id, "发布流程要推送 tag 触发打包工作流",
                       keywords=["发布", "tag"]) == 1

    # 让「发布」那条累计高频使用，模拟老记忆霸榜
    async with env["factory"]() as db:
        rows = (await db.execute(select(MemoryEntry))).scalars().all()
        for row in rows:
            row.usage_count = 50 if "发布流程" in row.text else 0
        await db.commit()

    async with env["factory"]() as db:
        ranked = await memory_service.load_memories(
            db, session_id, task_keywords=["上下文压缩", "摘要", "压缩"],
        )
        assert ranked, "应至少返回一条记忆"
        assert "上下文压缩" in ranked[0].text

        # 不传关键词时排序回落到 usage_count，与改造前一致
        by_usage = await memory_service.load_memories(db, session_id)
        assert "发布流程" in by_usage[0].text


@pytest.mark.asyncio
async def test_load_memories_does_not_count_usage(env):
    """注入不计使用：load_memories 不写 usage_count / last_usage_at。"""
    session_id = await _seed_session(env)
    assert await _save(env, session_id, "注入只让模型看到，不算一次使用",
                       keywords=["注入"]) == 1

    async with env["factory"]() as db:
        await memory_service.load_memories(db, session_id, task_keywords=["注入"])
    entry = (await _entries(env))[0]
    assert entry.usage_count == 0
    assert not entry.last_usage_at


@pytest.mark.asyncio
async def test_search_result_carries_scope_origin_date_and_stale_hint(env):
    """每条结果带 [作用域/来源/日期]，并附「需核对当前代码」提示。"""
    session_id = await _seed_session(env)
    assert await _save(
        env, session_id, "会话级记忆需要在结果里标注来源与日期",
        keywords=["会话级", "标注"], origin="manual",
    ) == 1

    result = await _search(env, session_id, "标注")
    assert result.ok is True
    line = next(ln for ln in result.output.splitlines() if "会话级记忆" in ln)
    assert "[会话/手动/" in line
    assert "先核对当前代码" in result.output


@pytest.mark.asyncio
async def test_offset_pages_through_results(env):
    """offset 翻页：两页结果不重复、条数受 limit 约束。"""
    session_id = await _seed_session(env)
    for index in range(3):
        assert await _save(
            env, session_id, f"分页测试记忆第 {index} 条，关键词是记忆检索分页",
            keywords=["分页", f"条目{index}"],
        ) == 1

    first = await _search(env, session_id, "记忆检索分页", limit=1, offset=0)
    second = await _search(env, session_id, "记忆检索分页", limit=1, offset=1)
    assert first.data["memory_total"] == 3
    assert first.data["memory"] == 1
    assert second.data["memory"] == 1
    assert first.output.splitlines() != second.output.splitlines()
    assert "offset=1" in first.output  # 头部给出可用的翻页参数


# ───────────── usage 只作同分 tiebreak（plan-549-2281 复盘修复）─────────────


@pytest.mark.asyncio
async def test_usage_does_not_override_rrf_score(env):
    """usage_count 不得翻转 RRF 排名：只能让**同分**条目更靠前。

    原实现把 `0.001 × usage_count` 加进总分，而 RRF 相邻名次差只有
    1/60-1/61≈0.000273——1 次命中就足以翻盘，「用得多的老记忆」能靠累计计数霸榜。
    """
    session_id = await _seed_session(env)
    # 两条都命中关键词，但 strong 的 keywords 与查询重合更多（RRF 分更高）
    await _save(env, session_id, "记忆检索排序：强命中条目", keywords=["记忆检索", "排序"])
    await _save(env, session_id, "记忆检索排序：弱命中条目", keywords=["记忆检索"])

    async with env["factory"]() as db:
        rows = (await db.execute(select(MemoryEntry).order_by(MemoryEntry.id))).scalars().all()
        # 让弱命中的那条累计高频使用，模拟老记忆霸榜
        weak = next(r for r in rows if "弱命中" in r.text)
        weak.usage_count = 5000
        await db.commit()

    result = await _search(env, session_id, "记忆检索排序")
    assert result.ok is True
    assert result.data["memory"] == 2
    first_line = next(ln for ln in result.output.splitlines() if "命中条目" in ln)
    assert "强命中" in first_line, "usage_count 不该压过更高的 RRF 分"


@pytest.mark.asyncio
async def test_usage_breaks_ties_between_equal_scores(env):
    """同分时 usage_count 更高的排前面（保留「常用记忆更靠前」的可验证行为）。"""
    session_id = await _seed_session(env)
    await _save(env, session_id, "同分测试条目甲，关键词完全一致", keywords=["同分测试"])
    await _save(env, session_id, "同分测试条目乙，关键词完全一致", keywords=["同分测试"])

    async with env["factory"]() as db:
        rows = (await db.execute(select(MemoryEntry).order_by(MemoryEntry.id))).scalars().all()
        target = next(r for r in rows if "条目乙" in r.text)
        target.usage_count = 50
        await db.commit()

    result = await _search(env, session_id, "同分测试")
    assert result.ok is True
    lines = [ln for ln in result.output.splitlines() if "同分测试条目" in ln]
    assert len(lines) == 2
    assert "条目乙" in lines[0], "同分时 usage_count 高的应靠前"


@pytest.mark.asyncio
async def test_offset_paging_stable_across_pages(env):
    """翻页稳定：第 1 页命中会把 usage_count +1，但不得让第 2 页与第 1 页重复。"""
    session_id = await _seed_session(env)
    for index in range(4):
        assert await _save(
            env, session_id, f"稳定翻页记忆第 {index} 条，关键词一致用于同分",
            keywords=["稳定翻页"],
        ) == 1

    seen: list[str] = []
    for offset in (0, 2):
        page = await _search(env, session_id, "稳定翻页", limit=2, offset=offset)
        assert page.ok is True
        lines = [ln for ln in page.output.splitlines() if "稳定翻页记忆第" in ln]
        assert len(lines) == 2
        seen.extend(lines)

    # 四条各不相同、不重不漏
    assert len(seen) == len(set(seen)) == 4, f"翻页出现重复或遗漏: {seen}"


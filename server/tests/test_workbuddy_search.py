"""workbuddy 云搜索适配器（web_search 的默认引擎）回归。

覆盖要点：
- 云端结果 → 本项目统一结构（过滤无 url 项、缺标题回落 url）；
- 不可用路径（无 db / 无 workbuddy 供应商）返回 None，由调用方回退本地抓取——
  这是「未登录也不能让搜索整体失败」的契约。
"""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestration.tools.workbuddy_search import _normalize, search_via_workbuddy
from app.persistence import models  # noqa: F401  （注册全部表，create_all 需要）
from app.persistence.database import Base


def test_normalize_maps_cloud_results():
    data = {
        "provider": "bing",
        "results": [
            {"title": "标题", "url": "https://a.example/1", "snippet": "摘要"},
            {"title": "无 url", "url": "", "snippet": "x"},
            {"url": "https://b.example/2"},
            "not-a-dict",
        ],
        "total_results": 4,
    }
    out = _normalize(data)
    assert [r["url"] for r in out] == ["https://a.example/1", "https://b.example/2"]
    assert out[0] == {"title": "标题", "url": "https://a.example/1", "snippet": "摘要"}
    assert out[1]["title"] == "https://b.example/2"  # 缺标题时回落 url
    assert out[1]["snippet"] == ""


async def test_search_returns_none_without_db_session():
    assert await search_via_workbuddy(None, "q", 5) is None


async def test_search_returns_none_without_workbuddy_provider(tmp_path):
    """未登录 / 无 workbuddy 供应商时返回 None（调用方回退 bing），而不是抛错。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/wb_search.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            assert await search_via_workbuddy(db, "q", 5) is None
    finally:
        await engine.dispose()

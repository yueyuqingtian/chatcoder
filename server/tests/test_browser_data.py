"""内置浏览器持久化底座测试（plan-771-3177）：书签与历史模型、迁移、REST 接口。"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import browser_data
from app.persistence.database import Base, async_session_factory
from app.persistence.migrations import run_migrations


@pytest.fixture
def browser_app() -> FastAPI:
    app = FastAPI()
    app.include_router(browser_data.router, prefix="/api")
    return app


@pytest.mark.asyncio
async def test_models_registered_and_migrations_idempotent():
    """BrowserBookmark / BrowserHistory 应注册在 Base.metadata 中，且迁移可幂等运行。"""
    assert "browser_bookmarks" in Base.metadata.tables
    assert "browser_history" in Base.metadata.tables
    async with async_session_factory() as db:
        res = await run_migrations(db)
        assert isinstance(res, dict)
        assert res["errors"] == 0


@pytest.mark.asyncio
async def test_bookmarks_crud_dedup_and_filter(browser_app: FastAPI):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=browser_app),
        base_url="http://test",
    ) as client:
        # 1. 创建书签（按 sort_order 排序）
        r1 = await client.post("/api/browser/bookmarks", json={
            "title": "Docs",
            "url": "https://example.com/docs",
            "folder": "工作",
            "sort_order": 20,
        })
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["created"] is True
        b1 = d1["item"]
        assert b1["title"] == "Docs"
        assert b1["folder"] == "工作"

        r2 = await client.post("/api/browser/bookmarks", json={
            "title": "Local App",
            "url": "http://localhost:5173",
            "folder": "开发",
            "sort_order": 10,
        })
        assert r2.status_code == 200
        assert r2.json()["created"] is True

        # 2. 同 URL 查重更新而不新增
        r1_dup = await client.post("/api/browser/bookmarks", json={
            "title": "Docs Updated",
            "url": "https://example.com/docs",
            "folder": "文档",
        })
        assert r1_dup.status_code == 200
        d1_dup = r1_dup.json()
        assert d1_dup["created"] is False
        assert d1_dup["item"]["id"] == b1["id"]
        assert d1_dup["item"]["title"] == "Docs Updated"
        assert d1_dup["item"]["folder"] == "文档"

        # 3. 列表与排序（sort_order=10 的 Local App 排在 20 前面）
        list_all = await client.get("/api/browser/bookmarks")
        assert list_all.status_code == 200
        payload = list_all.json()
        assert payload["total"] == 2
        assert [x["url"] for x in payload["items"]] == [
            "http://localhost:5173",
            "https://example.com/docs",
        ]
        assert set(payload["folders"]) == {"开发", "文档"}

        # 4. q 与 folder 过滤
        by_folder = await client.get("/api/browser/bookmarks", params={"folder": "文档"})
        assert by_folder.status_code == 200
        assert by_folder.json()["total"] == 1
        assert by_folder.json()["items"][0]["id"] == b1["id"]

        by_q = await client.get("/api/browser/bookmarks", params={"q": "5173"})
        assert by_q.status_code == 200
        assert by_q.json()["total"] == 1
        assert by_q.json()["items"][0]["title"] == "Local App"

        # 5. PATCH 更新
        upd = await client.patch(f"/api/browser/bookmarks/{b1['id']}", json={
            "title": "API Reference",
            "sort_order": 5,
        })
        assert upd.status_code == 200
        assert upd.json()["item"]["title"] == "API Reference"
        assert upd.json()["item"]["sort_order"] == 5

        upd_missing = await client.patch("/api/browser/bookmarks/999999", json={"title": "x"})
        assert upd_missing.status_code == 404

        # 6. DELETE 删除
        dele = await client.delete(f"/api/browser/bookmarks/{b1['id']}")
        assert dele.status_code == 200
        assert dele.json()["ok"] is True

        dele_missing = await client.delete(f"/api/browser/bookmarks/{b1['id']}")
        assert dele_missing.status_code == 404


@pytest.mark.asyncio
async def test_history_record_dedup_ignore_and_delete(browser_app: FastAPI):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=browser_app),
        base_url="http://test",
    ) as client:
        # 1. 忽略 about:blank 与非 http(s) 地址
        for bad in ("", "about:blank", "file:///C:/test.html", "javascript:void(0)"):
            res = await client.post("/api/browser/history", json={"url": bad, "title": "ignored"})
            assert res.status_code == 200
            assert res.json()["ignored"] is True

        # 2. 首次访问 + 同 URL 累加计数
        r1 = await client.post("/api/browser/history", json={
            "url": "https://example.com/a",
            "title": "Page A",
        })
        assert r1.status_code == 200
        item1 = r1.json()["item"]
        assert item1["visit_count"] == 1

        r1_again = await client.post("/api/browser/history", json={
            "url": "https://example.com/a",
            "title": "Page A v2",
            "favicon_url": "https://example.com/favicon.ico",
        })
        assert r1_again.status_code == 200
        item1_v2 = r1_again.json()["item"]
        assert item1_v2["id"] == item1["id"]
        assert item1_v2["visit_count"] == 2
        assert item1_v2["title"] == "Page A v2"
        assert item1_v2["favicon_url"] == "https://example.com/favicon.ico"

        r2 = await client.post("/api/browser/history", json={
            "url": "http://127.0.0.1:8080/dashboard",
            "title": "Dashboard",
        })
        assert r2.status_code == 200
        item2 = r2.json()["item"]

        # 3. 列表倒序 + q 过滤 + limit
        lst = await client.get("/api/browser/history", params={"limit": 10})
        assert lst.status_code == 200
        assert lst.json()["total"] == 2
        assert lst.json()["items"][0]["id"] == item2["id"]

        filtered = await client.get("/api/browser/history", params={"q": "Dashboard"})
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["url"] == "http://127.0.0.1:8080/dashboard"

        limited = await client.get("/api/browser/history", params={"limit": 1})
        assert limited.status_code == 200
        assert limited.json()["total"] == 1

        # 4. 单条删除与全部清空
        d_one = await client.delete(f"/api/browser/history/{item1['id']}")
        assert d_one.status_code == 200
        assert d_one.json()["ok"] is True

        d_missing = await client.delete(f"/api/browser/history/{item1['id']}")
        assert d_missing.status_code == 404

        d_all = await client.delete("/api/browser/history")
        assert d_all.status_code == 200
        assert d_all.json()["ok"] is True
        assert d_all.json()["deleted"] == 1

        after = await client.get("/api/browser/history")
        assert after.json()["total"] == 0


def test_main_app_mounts_browser_routes():
    """验证主应用正确挂载了 /api/browser/* 路由。"""
    from app.main import create_app

    app = create_app()
    paths = set((app.openapi().get("paths") or {}).keys())
    assert "/api/browser/bookmarks" in paths
    assert "/api/browser/bookmarks/{bookmark_id}" in paths
    assert "/api/browser/history" in paths
    assert "/api/browser/history/{history_id}" in paths


@pytest.mark.asyncio
async def test_bookmark_import_export_and_credential_url_rejection(browser_app: FastAPI):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser_app), base_url="http://test") as client:
        item = {"title": "文档", "url": "https://example.com/reference", "folder": "开发", "sort_order": -10}
        rejected = await client.post("/api/browser/bookmarks/import", json={
            "items": [item, {"url": "https://user:password@example.com/"}],
        })
        assert rejected.status_code == 400
        assert (await client.post("/api/browser/bookmarks/import", json={"items": [item]})).json() == {"imported": 1}
        assert (await client.post("/api/browser/bookmarks/import", json={"items": [item]})).json() == {"imported": 0}
        exported = (await client.get("/api/browser/bookmarks/export")).json()
        assert exported["version"] == 1
        assert {key: exported["items"][0][key] for key in item} == item
        record = await client.post("/api/browser/history", json={"url": "https://user:password@example.com/"})
        assert record.json()["ignored"] is True

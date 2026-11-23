"""内置浏览器持久化路由（plan-771-3177）：书签与浏览历史（跨会话全局共享）。

设计要点：
- 所有写操作经 `run_write_locked` 提交到 WriteEngine 单写线程执行，遵循项目无锁单写者规范；
- 仅持久化书签与访问历史，**严禁**在后端存储任何网站账号密码（凭据仅存 Electron 主进程 safeStorage）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db, run_write_locked
from app.persistence.models.browser import BrowserBookmark, BrowserHistory

router = APIRouter(prefix="/browser", tags=["browser"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_http_url(url: str) -> bool:
    u = url.strip()
    if not u or u.lower() == "about:blank":
        return False
    try:
        parsed = urlparse(u)
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname) and not (parsed.username or parsed.password)


def _bookmark_dict(row: BrowserBookmark) -> dict:
    return {
        "id": int(row.id),
        "title": row.title,
        "url": row.url,
        "folder": row.folder or "",
        "favicon_url": row.favicon_url,
        "sort_order": int(row.sort_order or 0),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _history_dict(row: BrowserHistory) -> dict:
    return {
        "id": int(row.id),
        "url": row.url,
        "title": row.title or "",
        "favicon_url": row.favicon_url,
        "visit_count": int(row.visit_count or 1),
        "last_visited_at": row.last_visited_at,
    }


class BookmarkCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=300)
    folder: str = Field(default="", max_length=120)
    favicon_url: str | None = Field(default=None, max_length=2000)
    sort_order: int = 0


class BookmarkUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    url: str | None = Field(default=None, max_length=2000)
    folder: str | None = Field(default=None, max_length=120)
    favicon_url: str | None = Field(default=None, max_length=2000)
    sort_order: int | None = None


class BookmarkImport(BaseModel):
    items: list[BookmarkCreate] = Field(max_length=2000)


class HistoryRecordIn(BaseModel):
    url: str = Field(default="", max_length=2000)
    title: str = Field(default="", max_length=300)
    favicon_url: str | None = Field(default=None, max_length=2000)


# ── 书签 ──────────────────────────────────────────────


@router.get("/bookmarks")
async def list_bookmarks(
    q: str | None = None,
    folder: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """列出全局书签（支持关键词 q 与分组 folder 过滤，按 sort_order/id 升序）。"""
    stmt = select(BrowserBookmark)
    if folder is not None:
        stmt = stmt.where(BrowserBookmark.folder == folder.strip())
    kw = (q or "").strip()
    if kw:
        like = f"%{kw}%"
        stmt = stmt.where(
            or_(
                BrowserBookmark.title.ilike(like),
                BrowserBookmark.url.ilike(like),
                BrowserBookmark.folder.ilike(like),
            )
        )
    stmt = stmt.order_by(BrowserBookmark.sort_order.asc(), BrowserBookmark.id.asc())
    rows = (await db.execute(stmt)).scalars().all()

    # 返回所有已知非空分组名，供书签栏与编辑器下拉选择
    folder_rows = (
        await db.execute(
            select(BrowserBookmark.folder)
            .where(BrowserBookmark.folder != "")
            .distinct()
            .order_by(BrowserBookmark.folder.asc())
        )
    ).scalars().all()
    items = [_bookmark_dict(r) for r in rows]
    return {"items": items, "total": len(items), "folders": list(folder_rows)}


@router.post("/bookmarks")
async def create_or_update_bookmark(body: BookmarkCreate) -> dict:
    """新增书签；若同 URL 已存在则更新标题/分组并返回现有条目。"""
    url = body.url.strip()[:2000]
    if not _is_http_url(url):
        raise HTTPException(400, "请输入有效的 HTTP 或 HTTPS 书签地址")
    title = (body.title.strip() or url)[:300]
    folder = body.folder.strip()[:120]
    favicon_url = body.favicon_url.strip()[:2000] if body.favicon_url else None
    sort_order = int(body.sort_order or 0)
    now = _now_iso()

    def _persist(s) -> tuple[dict, bool]:
        existing = s.execute(
            select(BrowserBookmark).where(BrowserBookmark.url == url).limit(1)
        ).scalars().first()
        if existing is not None:
            if body.title.strip():
                existing.title = title
            if body.folder != "":
                existing.folder = folder
            if favicon_url is not None:
                existing.favicon_url = favicon_url
            if body.sort_order:
                existing.sort_order = sort_order
            existing.updated_at = now
            s.commit()
            return _bookmark_dict(existing), False

        row = BrowserBookmark(
            title=title,
            url=url,
            folder=folder,
            favicon_url=favicon_url,
            sort_order=sort_order,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        s.flush()
        out = _bookmark_dict(row)
        s.commit()
        return out, True

    item, created = await run_write_locked(_persist, label="browser.bookmark.save")
    return {"created": created, "item": item}


@router.patch("/bookmarks/{bookmark_id}")
async def update_bookmark(bookmark_id: int, body: BookmarkUpdate) -> dict:
    """更新书签标题、地址、分组或排序位。"""
    now = _now_iso()
    if body.url is not None:
        new_url = body.url.strip()[:2000]
        if not _is_http_url(new_url):
            raise HTTPException(400, "请输入有效的 HTTP 或 HTTPS 书签地址")
    else:
        new_url = None

    if body.title is not None and not body.title.strip():
        raise HTTPException(400, "书签标题不能为空")

    def _persist(s) -> dict | None:
        row = s.get(BrowserBookmark, bookmark_id)
        if row is None:
            return None
        if body.title is not None:
            row.title = body.title.strip()[:300]
        if new_url is not None:
            row.url = new_url
        if body.folder is not None:
            row.folder = body.folder.strip()[:120]
        if body.favicon_url is not None:
            row.favicon_url = body.favicon_url.strip()[:2000] or None
        if body.sort_order is not None:
            row.sort_order = int(body.sort_order)
        row.updated_at = now
        out = _bookmark_dict(row)
        s.commit()
        return out

    item = await run_write_locked(_persist, label=f"browser.bookmark.update.{bookmark_id}")
    if item is None:
        raise HTTPException(404, "书签不存在")
    return {"item": item}


@router.delete("/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: int) -> dict:
    """删除书签。"""
    def _persist(s) -> bool:
        row = s.get(BrowserBookmark, bookmark_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True

    ok = await run_write_locked(_persist, label=f"browser.bookmark.delete.{bookmark_id}")
    if not ok:
        raise HTTPException(404, "书签不存在")
    return {"ok": True}


@router.get("/bookmarks/export")
async def export_bookmarks(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(BrowserBookmark).order_by(BrowserBookmark.sort_order, BrowserBookmark.id))).scalars().all()
    return {"version": 1, "items": [_bookmark_dict(row) for row in rows]}


@router.post("/bookmarks/import")
async def import_bookmarks(body: BookmarkImport) -> dict:
    entries = {entry.url.strip(): entry for entry in body.items}
    if any(not _is_http_url(url) for url in entries):
        raise HTTPException(400, "导入文件包含无效书签地址")

    def _persist(s) -> int:
        existing = set(s.execute(select(BrowserBookmark.url)).scalars())
        now = _now_iso()
        rows = [
            BrowserBookmark(url=url, title=(entry.title.strip() or url)[:300], folder=entry.folder.strip(),
                            favicon_url=entry.favicon_url, sort_order=entry.sort_order, created_at=now, updated_at=now)
            for url, entry in entries.items() if url not in existing
        ]
        s.add_all(rows)
        s.commit()
        return len(rows)

    return {"imported": await run_write_locked(_persist, label="browser.bookmark.import")}


# ── 浏览历史 ──────────────────────────────────────────────


@router.get("/history")
async def list_history(
    q: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """列出浏览历史（支持关键词模糊匹配 url/title，按最近访问时间倒序）。"""
    safe_limit = max(1, min(int(limit or 50), 500))
    stmt = select(BrowserHistory)
    kw = (q or "").strip()
    if kw:
        like = f"%{kw}%"
        stmt = stmt.where(
            or_(
                BrowserHistory.title.ilike(like),
                BrowserHistory.url.ilike(like),
            )
        )
    stmt = stmt.order_by(
        BrowserHistory.last_visited_at.desc(),
        BrowserHistory.id.desc(),
    ).limit(safe_limit)
    rows = (await db.execute(stmt)).scalars().all()
    items = [_history_dict(r) for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/history")
async def record_history(body: HistoryRecordIn) -> dict:
    """记录网页访问：忽略 about:blank 与非 http(s) 空地址；同 URL 累加访问次数。"""
    url = body.url.strip()[:2000]
    if not _is_http_url(url):
        return {"ok": True, "ignored": True, "item": None}

    title = body.title.strip()[:300]
    favicon_url = body.favicon_url.strip()[:2000] if body.favicon_url else None
    now = _now_iso()

    def _persist(s) -> dict:
        existing = s.execute(
            select(BrowserHistory).where(BrowserHistory.url == url).limit(1)
        ).scalars().first()
        if existing is not None:
            existing.visit_count = int(existing.visit_count or 0) + 1
            if title:
                existing.title = title
            if favicon_url:
                existing.favicon_url = favicon_url
            existing.last_visited_at = now
            out = _history_dict(existing)
            s.commit()
            return out

        row = BrowserHistory(
            url=url,
            title=title or url[:300],
            favicon_url=favicon_url,
            visit_count=1,
            last_visited_at=now,
        )
        s.add(row)
        s.flush()
        out = _history_dict(row)
        s.commit()
        return out

    item = await run_write_locked(_persist, label="browser.history.record")
    return {"ok": True, "ignored": False, "item": item}


@router.delete("/history/{history_id}")
async def delete_history_item(history_id: int) -> dict:
    """删除单条浏览历史。"""
    def _persist(s) -> bool:
        row = s.get(BrowserHistory, history_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True

    ok = await run_write_locked(_persist, label=f"browser.history.delete.{history_id}")
    if not ok:
        raise HTTPException(404, "历史记录不存在")
    return {"ok": True}


@router.delete("/history")
async def clear_history(days: int | None = Query(None, ge=1, le=365)) -> dict:
    """清空全部浏览历史，或删除最近指定天数的记录。"""
    def _persist(s) -> int:
        stmt = delete(BrowserHistory)
        if days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            stmt = stmt.where(BrowserHistory.last_visited_at >= cutoff)
        res = s.execute(stmt)
        deleted = int(res.rowcount or 0)
        s.commit()
        return deleted

    deleted = await run_write_locked(_persist, label="browser.history.clear")
    return {"ok": True, "deleted": deleted}

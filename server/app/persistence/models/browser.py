"""内置浏览器跨会话持久化模型（plan-771-3177）：书签与浏览历史。

设计要点：
- 全局用户级数据（不绑定 session_id），所有会话的浏览器面板共享同一份书签与历史；
- 时间字段统一使用 ISO-8601 字符串，与桌面操控路线等现有表口径一致；
- 账号密码凭据**不存后端数据库**，由 Electron 主进程 safeStorage 加密存本地（browser-vault.enc）。
"""
from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class BrowserBookmark(Base):
    """网页书签表。"""

    __tablename__ = "browser_bookmarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    folder: Mapped[str] = mapped_column(String(120), default="", server_default="", nullable=False)
    favicon_url: Mapped[str | None] = mapped_column(String(2000))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        Index("ix_browser_bookmarks_url", "url"),
        Index("ix_browser_bookmarks_folder", "folder"),
    )


class BrowserHistory(Base):
    """网页浏览历史表（按 URL 归并访问次数与最近访问时间）。"""

    __tablename__ = "browser_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(2000), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", server_default="", nullable=False)
    favicon_url: Mapped[str | None] = mapped_column(String(2000))
    visit_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    last_visited_at: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        Index("ix_browser_history_last_visited_at", "last_visited_at"),
    )

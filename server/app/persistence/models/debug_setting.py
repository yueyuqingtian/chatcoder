"""调试面板配置（plan-282-1441 #8 内置 MCP「开发调试」的配置持久化）。

为什么需要这张表
----------------
「开发调试」面板里的 Web 调试端口、JDWP 主机/端口此前只存在前端组件 state 中，
用户改完即丢（重新进入面板又回到 9222 / 127.0.0.1 / 5005 的硬编码默认值）。
这里把它们落库。

为什么是**单行全局**配置
------------------------
该面板没有项目选择器（与「数据库连接」面板不同，后者按项目隔离）；
这些值描述的是"本机调试环境"（浏览器调试端口、要附加的 JDWP 端点），
不随项目变化，因此固定一行即可，由服务层保证只有一行。
"""
from sqlalchemy import BigInteger, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base

# 默认值（服务层与前端共用同一套口径）
DEFAULT_WEB_PORT = 9222
DEFAULT_JDWP_HOST = "127.0.0.1"
DEFAULT_JDWP_PORT = 5005


class DebugSetting(Base):
    """调试配置（单行）。"""

    __tablename__ = "debug_settings"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    # 浏览器 CDP 调试端口（Chrome/Edge 以 --remote-debugging-port=<port> 启动）
    web_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_WEB_PORT)
    # JDWP 附加目标（-agentlib:jdwp=... 的 address）
    jdwp_host: Mapped[str] = mapped_column(String(120), default=DEFAULT_JDWP_HOST)
    jdwp_port: Mapped[int] = mapped_column(Integer, default=DEFAULT_JDWP_PORT)
    updated_at: Mapped[str | None] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

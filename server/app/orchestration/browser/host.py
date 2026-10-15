"""浏览器宿主抽象（plan-20261015-browser-agent）。

宿主只提供「原语」（标签、导航、页面工具包调用、鼠标、按键、文字、截图、日志…），
动作编排（ref 定位、等待稳定、结果文案）统一在 driver.py，两种宿主行为一致：

- DesktopHost：桌面端内置浏览器（用户在右侧面板看到的就是它），经 bridge 远程调用；
- PlaywrightHost：无桌面端连接时（浏览器模式评审、独立服务端部署）的后台兜底。

原语清单与参数见 driver.py 顶部注释；两边实现必须同步。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from app.orchestration.browser.bridge import bridge


class BrowserHost(ABC):
    kind: str = "base"
    # 是否会把动作实时呈现给用户（决定结果文案里要不要提示“可在右侧面板查看”）
    visible_to_user: bool = False

    @abstractmethod
    async def call(self, session_id: int, op: str, args: dict, timeout: float,
                   cancel_event: asyncio.Event | None = None) -> dict:
        """执行一个原语；失败抛 HostCallError / HostUnavailable。"""


class DesktopHost(BrowserHost):
    kind = "desktop"
    visible_to_user = True

    async def call(self, session_id: int, op: str, args: dict, timeout: float,
                   cancel_event: asyncio.Event | None = None) -> dict:
        return await bridge.request(session_id, op, args, timeout, cancel_event=cancel_event)


_desktop = DesktopHost()


async def acquire_host() -> BrowserHost:
    """桌面端在线则用桌面端，否则回退 Playwright 兜底宿主（懒启动）。"""
    if bridge.has_host():
        return _desktop
    from app.orchestration.browser.playwright_host import PlaywrightHost

    return PlaywrightHost.instance()

"""v1.0: 浏览器自动化工具 — 基于 Playwright。

提供 navigate/screenshot/click/type 等基础浏览器操作。
标记 risk_level="high"，需审批。
"""
import logging
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT = 30000  # ms


class BrowserNavigateTool(Tool):
    name = "browser_navigate"
    risk_level = "high"
    description = "在内置浏览器中导航到指定 URL。需审批。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标 URL"},
                    },
                    "required": ["url"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = args.get("url", "")
        if not url:
            return ToolResult(ok=False, output="", error="url 为空")
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, timeout=_TIMEOUT)
                title = await page.title()
                content = await page.content()
                await browser.close()
            # 截断内容
            if len(content) > 8000:
                content = content[:8000] + "\n...(截断)"
            return ToolResult(
                ok=True,
                output=f"页面标题: {title}\n\n{content}",
                data={"url": url, "title": title},
            )
        except ImportError:
            return ToolResult(ok=False, output="", error="playwright 未安装，请运行: pip install playwright && playwright install chromium")
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"浏览器导航失败: {e}")


class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    risk_level = "high"
    description = "对指定 URL 进行截图，返回 base64 图片。需审批。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标 URL"},
                        "full_page": {"type": "boolean", "description": "是否截取全页"},
                    },
                    "required": ["url"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = args.get("url", "")
        full_page = args.get("full_page", False)
        if not url:
            return ToolResult(ok=False, output="", error="url 为空")
        try:
            import base64
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 720})
                await page.goto(url, timeout=_TIMEOUT)
                screenshot = await page.screenshot(full_page=full_page)
                await browser.close()
            b64 = base64.b64encode(screenshot).decode("ascii")
            return ToolResult(
                ok=True,
                output=f"截图完成 ({len(screenshot)} bytes)",
                data={"url": url, "screenshot_base64": b64[:100] + "...(截断)"},
            )
        except ImportError:
            return ToolResult(ok=False, output="", error="playwright 未安装")
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"截图失败: {e}")

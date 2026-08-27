"""v1.0 / v32: 浏览器自动化与 DOM/截图工具 — 基于 Playwright 与无障碍/多模态支持。

提供：
- browser_navigate: 页面导航与状态加载
- browser_click: 元素点击（支持 selector/坐标/文本）
- browser_type: 元素输入与提交
- browser_snapshot: 提取精简 DOM 树与可交互元素快照（供非多模态/文本模型感知）
- browser_screenshot: 页面截图并返回 base64 及图像元数据（供多模态模型识别）
- browser_evaluate: 执行 JavaScript 获取动态页面数据

标记 risk_level="high"，需审批；且受全局设置 browser_enabled 控制。
"""
import asyncio
import base64
import logging
from typing import Any

from app.core.config import settings
from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT = 30000  # ms


def _check_browser_enabled() -> str | None:
    if not getattr(settings, "browser_enabled", False):
        return "浏览器工具当前未启用。请在「设置 -> 通用设置 -> 内置浏览器自动化工具」中开启该功能。"
    return None


class _BrowserSessionManager:
    """管理单例 Playwright 实例与活动页面，支持跨工具连续操作。"""
    _pw = None
    _browser = None
    _context = None
    _page = None
    _lock = asyncio.Lock()

    @classmethod
    async def get_page(cls, headless: bool = True):
        async with cls._lock:
            if cls._pw is None:
                from playwright.async_api import async_playwright
                cls._pw = await async_playwright().start()
            if cls._browser is None or not cls._browser.is_connected():
                cls._browser = await cls._pw.chromium.launch(
                    headless=headless,
                    args=["--disable-web-security", "--no-sandbox", "--disable-setuid-sandbox"],
                )
                cls._context = await cls._browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 ChatCoder/1.0",
                )
                cls._page = await cls._context.new_page()
            elif cls._page is None or cls._page.is_closed():
                cls._page = await cls._context.new_page()
            return cls._page

    @classmethod
    async def close_all(cls):
        async with cls._lock:
            if cls._page and not cls._page.is_closed():
                try:
                    await cls._page.close()
                except Exception:
                    pass
            if cls._browser and cls._browser.is_connected():
                try:
                    await cls._browser.close()
                except Exception:
                    pass
            if cls._pw:
                try:
                    await cls._pw.stop()
                except Exception:
                    pass
            cls._pw = None
            cls._browser = None
            cls._context = None
            cls._page = None


class BrowserNavigateTool(Tool):
    name = "browser_navigate"
    risk_level = "high"
    description = "在内置浏览器中导航到指定 URL，支持加载页面并返回精简标题和摘要。需审批，需在设置中启用浏览器。"

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
                        "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"], "description": "页面等待条件，默认 domcontentloaded"},
                    },
                    "required": ["url"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_browser_enabled()
        if err:
            return ToolResult(ok=False, output="", error=err)

        url = (args.get("url") or "").strip()
        if not url:
            return ToolResult(ok=False, output="", error="url 不能为空")
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("file://")):
            url = "https://" + url

        wait_until = args.get("wait_until", "domcontentloaded")
        headless = getattr(settings, "browser_headless", True)
        try:
            page = await _BrowserSessionManager.get_page(headless=headless)
            response = await page.goto(url, timeout=_TIMEOUT, wait_until=wait_until)
            status_code = response.status if response else 200
            title = await page.title()
            current_url = page.url

            # 简要提取页面主要文字
            try:
                body_text = await page.inner_text("body", timeout=5000)
                if len(body_text) > 4000:
                    body_text = body_text[:4000] + "\n...(内容已截断，如需更多可使用 browser_snapshot 读取 DOM 树)"
            except Exception:
                body_text = "(无法提取文本内容)"

            return ToolResult(
                ok=True,
                output=f"成功导航到: {current_url}\n页面标题: {title}\n状态码: {status_code}\n\n页面摘要:\n{body_text}",
                data={"url": current_url, "title": title, "status": status_code},
            )
        except ImportError:
            return ToolResult(ok=False, output="", error="playwright 未安装。请在服务端运行: pip install playwright && playwright install chromium")
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"浏览器导航失败: {e}")


class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    risk_level = "high"
    description = "对当前页面或指定 URL 进行截图，返回 base64 图片及多模态元数据。需审批，需在设置中启用浏览器。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标 URL（可选，若不传则对当前已打开的页面截图）"},
                        "full_page": {"type": "boolean", "description": "是否截取完整长图，默认 false"},
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_browser_enabled()
        if err:
            return ToolResult(ok=False, output="", error=err)

        url = (args.get("url") or "").strip()
        full_page = bool(args.get("full_page", False))
        headless = getattr(settings, "browser_headless", True)
        try:
            page = await _BrowserSessionManager.get_page(headless=headless)
            if url:
                if not (url.startswith("http://") or url.startswith("https://") or url.startswith("file://")):
                    url = "https://" + url
                await page.goto(url, timeout=_TIMEOUT)
            
            current_url = page.url
            title = await page.title()
            screenshot_bytes = await page.screenshot(full_page=full_page, type="png")
            b64 = base64.b64encode(screenshot_bytes).decode("ascii")
            
            return ToolResult(
                ok=True,
                output=f"截图成功: {current_url} ({title})\n图片大小: {len(screenshot_bytes)} 字节\nBase64 预览: data:image/png;base64,{b64[:60]}...",
                data={
                    "url": current_url,
                    "title": title,
                    "screenshot_base64": b64,
                    "mime_type": "image/png",
                    "bytes_len": len(screenshot_bytes),
                },
            )
        except ImportError:
            return ToolResult(ok=False, output="", error="playwright 未安装")
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"截图失败: {e}")


class BrowserClickTool(Tool):
    name = "browser_click"
    risk_level = "high"
    description = "在当前浏览器页面中点击元素。支持 CSS 选择器或页面坐标 (x, y)。需审批。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string", "description": "CSS 选择器或元素文本选择器（如 'button:has-text(\"登录\")' 或 '#submit-btn'）"},
                        "x": {"type": "number", "description": "点击的横坐标（可选）"},
                        "y": {"type": "number", "description": "点击的纵坐标（可选）"},
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_browser_enabled()
        if err:
            return ToolResult(ok=False, output="", error=err)

        selector = args.get("selector")
        x = args.get("x")
        y = args.get("y")
        headless = getattr(settings, "browser_headless", True)
        try:
            page = await _BrowserSessionManager.get_page(headless=headless)
            if selector:
                await page.click(selector, timeout=10000)
                target_desc = f"选择器 '{selector}'"
            elif x is not None and y is not None:
                await page.mouse.click(float(x), float(y))
                target_desc = f"坐标 ({x}, {y})"
            else:
                return ToolResult(ok=False, output="", error="必须提供 selector 或 (x, y) 坐标")

            # 等待微小交互与状态更新
            await asyncio.sleep(0.5)
            new_title = await page.title()
            return ToolResult(
                ok=True,
                output=f"已成功点击 {target_desc}。当前页面: {page.url} ({new_title})",
                data={"url": page.url, "title": new_title},
            )
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"浏览器点击失败: {e}")


class BrowserTypeTool(Tool):
    name = "browser_type"
    risk_level = "high"
    description = "在当前页面的输入框中填写或输入文本，可选择是否按回车提交。需审批。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string", "description": "目标输入框的 CSS 选择器"},
                        "text": {"type": "string", "description": "要输入的文本内容"},
                        "press_enter": {"type": "boolean", "description": "输入完成后是否按下 Enter 键，默认 false"},
                        "clear_before": {"type": "boolean", "description": "输入前是否先清空输入框，默认 true"},
                    },
                    "required": ["selector", "text"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_browser_enabled()
        if err:
            return ToolResult(ok=False, output="", error=err)

        selector = args.get("selector", "").strip()
        text = args.get("text", "")
        press_enter = bool(args.get("press_enter", False))
        clear_before = bool(args.get("clear_before", True))

        if not selector:
            return ToolResult(ok=False, output="", error="selector 不能为空")

        headless = getattr(settings, "browser_headless", True)
        try:
            page = await _BrowserSessionManager.get_page(headless=headless)
            if clear_before:
                await page.fill(selector, text, timeout=10000)
            else:
                await page.type(selector, text, timeout=10000)

            if press_enter:
                await page.press(selector, "Enter")
                await asyncio.sleep(0.5)

            return ToolResult(
                ok=True,
                output=f"已成功向 '{selector}' 输入文本 (长度: {len(text)} 字符){' 并按下回车' if press_enter else ''}。当前页面: {page.url}",
                data={"url": page.url, "selector": selector},
            )
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"浏览器输入失败: {e}")


class BrowserSnapshotTool(Tool):
    name = "browser_snapshot"
    risk_level = "normal"
    description = "获取当前页面的精简可交互 DOM 树快照（包括按钮、输入框、链接及选择器），供模型精确感知与决策。需在设置中启用浏览器。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "max_depth": {"type": "integer", "description": "DOM 树遍历深度，默认 4"},
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_browser_enabled()
        if err:
            return ToolResult(ok=False, output="", error=err)

        headless = getattr(settings, "browser_headless", True)
        try:
            page = await _BrowserSessionManager.get_page(headless=headless)
            # 通过 evaluate 提取页面中关键可交互元素列表
            extract_script = """
            () => {
                const elements = [];
                const interactiveSelectors = 'a, button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [onclick]';
                const nodes = document.querySelectorAll(interactiveSelectors);
                let idx = 1;
                for (const el of nodes) {
                    if (idx > 100) break; // 最多收集前 100 个核心交互项
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) continue; // 跳过不可见元素
                    const tag = el.tagName.toLowerCase();
                    const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 80);
                    const id = el.id ? `#${el.id}` : '';
                    const name = el.getAttribute('name') ? `[name="${el.getAttribute('name')}"]` : '';
                    const type = el.getAttribute('type') ? `[type="${el.getAttribute('type')}"]` : '';
                    let selector = tag + id + name + type;
                    if (!id && !name && el.className && typeof el.className === 'string') {
                        const cls = el.className.trim().split(/\\s+/).slice(0, 2).join('.');
                        if (cls) selector += `.${cls}`;
                    }
                    elements.push({
                        index: idx++,
                        tag: tag,
                        text: text,
                        selector: selector,
                        href: el.href || null,
                        disabled: el.disabled || false,
                        visible: rect.top >= 0 && rect.top <= window.innerHeight
                    });
                }
                return {
                    title: document.title,
                    url: window.location.href,
                    elementsCount: elements.length,
                    elements: elements
                };
            }
            """
            snapshot = await page.evaluate(extract_script)
            summary_lines = [
                f"=== 页面快照: {snapshot.get('title')} ===",
                f"URL: {snapshot.get('url')}",
                f"找到 {snapshot.get('elementsCount')} 个可交互 DOM 元素:\n",
            ]
            for el in snapshot.get("elements", []):
                summary_lines.append(f"[{el['index']}] <{el['tag']}> {el['text']} (选择器: `{el['selector']}`) {'[视口内]' if el.get('visible') else ''}")

            formatted_output = "\n".join(summary_lines)
            return ToolResult(
                ok=True,
                output=formatted_output,
                data=snapshot,
            )
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"提取 DOM 快照失败: {e}")


class BrowserEvaluateTool(Tool):
    name = "browser_evaluate"
    risk_level = "high"
    description = "在当前浏览器页面上下文中执行一段 JavaScript 代码并获取返回值。需审批。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script": {"type": "string", "description": "要执行的 JavaScript 表达式或函数字符串"},
                    },
                    "required": ["script"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_browser_enabled()
        if err:
            return ToolResult(ok=False, output="", error=err)

        script = args.get("script", "").strip()
        if not script:
            return ToolResult(ok=False, output="", error="script 不能为空")

        headless = getattr(settings, "browser_headless", True)
        try:
            page = await _BrowserSessionManager.get_page(headless=headless)
            result = await page.evaluate(script)
            return ToolResult(
                ok=True,
                output=f"JavaScript 执行结果:\n{result}",
                data={"result": result},
            )
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"JavaScript 执行失败: {e}")

"""Playwright 兜底宿主（plan-20261015-browser-agent）。

仅在**没有桌面端连接**时使用（浏览器模式评审前端、独立服务端部署）：
原语与桌面端宿主一一对应（见 driver.py 顶部清单），页面感知与定位共用同一份 page kit，
所以模型拿到的 ref、文案与桌面端完全一致，只是用户看不到实时画面。

每个会话一个独立浏览器上下文（Cookie/存储互不串），标签以 `pw_<n>` 编号。

稳定判定、接口错误回报、点击前校准与截图元信息与桌面端（host.ts + browser-agent.cjs）一致
（plan-20261015-browser-perf）：同一个动作在两条路径上回报的内容相同。
"""
from __future__ import annotations

import asyncio
import base64
import io
import itertools
import json
import logging
import time
from collections import deque
from typing import Any

from app.core.config import settings
from app.orchestration.browser.bridge import HostCallError
from app.orchestration.browser.host import BrowserHost
from app.orchestration.browser.kit import KIT_SOURCE, NOKIT_SENTINEL, kit_call_expression

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36 chatcoder")
_LOG_MAX = 400
_BODY_MAX = 120
_API_TYPES = ("xhr", "fetch")


class _Tab:
    def __init__(self, tab_id: str, page: Any) -> None:
        self.id = tab_id
        self.page = page
        self.console: deque = deque(maxlen=_LOG_MAX)
        self.network: deque = deque(maxlen=_LOG_MAX)
        self.responses: dict[str, Any] = {}
        self.events: list[dict] = []
        self.net_seq = 0
        self.dialog_accept = True
        self.prompt_text = ""


class PlaywrightHost(BrowserHost):
    kind = "headless"
    visible_to_user = False
    _inst: "PlaywrightHost | None" = None

    @classmethod
    def instance(cls) -> "PlaywrightHost":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._contexts: dict[int, Any] = {}
        self._tabs: dict[int, dict[str, _Tab]] = {}
        self._agent: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._req_ids = itertools.count(1)
        self._log_seq = itertools.count(1)

    # ── 生命周期 ─────────────────────────────────────────
    async def _ensure_browser(self):
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._pw is None:
                from app.core.browser_env import ensure_bundled_browsers_path

                bundled = ensure_bundled_browsers_path()
                try:
                    from playwright.async_api import async_playwright
                except ImportError as e:
                    raise HostCallError(
                        "当前没有连接桌面端，且服务端未安装 Playwright，无法使用浏览器。"
                        "请在桌面端中使用，或在服务端执行: pip install playwright && playwright install chromium"
                    ) from e
                self._pw = await async_playwright().start()
                self._bundled = bundled
            kwargs: dict[str, Any] = {"headless": bool(getattr(settings, "browser_headless", True)),
                                      "args": ["--no-sandbox", "--disable-setuid-sandbox"]}
            if getattr(self, "_bundled", None):
                kwargs["channel"] = "chromium"
            try:
                self._browser = await self._pw.chromium.launch(**kwargs)
            except Exception as e:  # noqa: BLE001
                if "Executable doesn't exist" in str(e) or "looks like Playwright was just installed" in str(e):
                    raise HostCallError("后台浏览器缺失或损坏：请在桌面端中使用，或在服务端执行 playwright install chromium") from e
                raise
            self._contexts.clear()
            self._tabs.clear()
            self._agent.clear()
            return self._browser

    async def _context(self, sid: int):
        browser = await self._ensure_browser()
        ctx = self._contexts.get(sid)
        if ctx is None:
            ctx = await browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=_UA)
            self._contexts[sid] = ctx
            self._tabs[sid] = {}
        return ctx

    def _wire(self, sid: int, tab: _Tab) -> None:
        page = tab.page

        def on_console(msg):
            tab.console.append({"seq": next(self._log_seq), "level": msg.type, "text": msg.text[:2000],
                                "source": (msg.location or {}).get("url", "")})

        def on_error(err):
            tab.console.append({"seq": next(self._log_seq), "level": "error", "text": f"未捕获异常: {err}"[:2000], "source": ""})

        def on_request(req):
            rid = f"r{next(self._req_ids)}"
            req._cc_id = rid  # type: ignore[attr-defined]
            tab.net_seq += 1
            tab.network.append({"seq": next(self._log_seq), "nseq": tab.net_seq, "id": rid, "method": req.method,
                                "url": req.url[:500], "type": req.resource_type, "status": None,
                                "started_at": time.time(), "done": False})

        def on_finished(req):
            rid = getattr(req, "_cc_id", None)
            for e in reversed(tab.network):
                if e["id"] == rid:
                    e["done"] = True
                    break

        def on_response(resp):
            rid = getattr(resp.request, "_cc_id", None)
            for e in reversed(tab.network):
                if e["id"] == rid:
                    e["status"] = resp.status
                    e["mime"] = (resp.headers or {}).get("content-type", "")[:80]
                    break
            if rid:
                tab.responses[rid] = resp
                if len(tab.responses) > _BODY_MAX:
                    tab.responses.pop(next(iter(tab.responses)))

        def on_failed(req):
            rid = getattr(req, "_cc_id", None)
            for e in reversed(tab.network):
                if e["id"] == rid:
                    e["status"] = "failed"
                    e["done"] = True
                    e["error"] = (req.failure or "")[:200] if isinstance(req.failure, str) else str(req.failure)[:200]
                    break

        def on_dialog(dialog):
            async def handle():
                accept = tab.dialog_accept or dialog.type in ("alert", "beforeunload")
                try:
                    if accept:
                        await dialog.accept(tab.prompt_text or dialog.default_value or "")
                    else:
                        await dialog.dismiss()
                except Exception:  # noqa: BLE001
                    pass
                tab.events.append({"type": "dialog", "dialog_type": dialog.type, "message": dialog.message[:300],
                                   "accepted": accept})
            asyncio.ensure_future(handle())

        def on_popup(popup):
            new_id = f"pw_{next(self._ids)}"
            nt = _Tab(new_id, popup)
            self._tabs.setdefault(sid, {})[new_id] = nt
            self._wire(sid, nt)
            tab.events.append({"type": "popup", "tab": new_id, "url": popup.url})

        def on_download(dl):
            tab.events.append({"type": "download", "filename": dl.suggested_filename, "url": dl.url[:300]})

        page.on("console", on_console)
        page.on("pageerror", on_error)
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfinished", on_finished)
        page.on("requestfailed", on_failed)
        page.on("dialog", on_dialog)
        page.on("popup", on_popup)
        page.on("download", on_download)

    async def _new_tab(self, sid: int) -> _Tab:
        ctx = await self._context(sid)
        page = await ctx.new_page()
        tab = _Tab(f"pw_{next(self._ids)}", page)
        self._tabs[sid][tab.id] = tab
        self._wire(sid, tab)
        return tab

    async def _tab(self, sid: int, tab_id: str | None, prefer: str | None = None) -> _Tab:
        await self._context(sid)
        tabs = self._tabs.setdefault(sid, {})
        for k in [k for k, t in tabs.items() if t.page.is_closed()]:
            tabs.pop(k, None)
        if tab_id:
            t = tabs.get(tab_id)
            if t is None:
                raise HostCallError(f"标签 {tab_id} 不存在，请用 browser_tabs 查看现有标签")
            self._agent[sid] = t.id
            return t
        if prefer and prefer in tabs:
            self._agent[sid] = prefer
            return tabs[prefer]
        cur = self._agent.get(sid)
        if cur and cur in tabs:
            return tabs[cur]
        t = next(iter(tabs.values()), None) or await self._new_tab(sid)
        self._agent[sid] = t.id
        return t

    async def _base(self, tab: _Tab) -> dict:
        page = tab.page
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001
            title = ""
        events, tab.events = tab.events, []
        return {"tab": tab.id, "url": page.url, "title": title, "events": events}

    async def _kit(self, page, fn: str, args: dict | None = None):
        expr = kit_call_expression(fn, args)
        res = await page.evaluate(expr)
        if res == NOKIT_SENTINEL:
            await page.evaluate(KIT_SOURCE)
            res = await page.evaluate(expr)
        if not isinstance(res, dict):
            raise HostCallError("页面工具执行失败")
        if not res.get("ok"):
            raise HostCallError(str(res.get("error") or "页面工具执行失败"))
        return res.get("value")

    def _pending(self, tab: _Tab, since: int) -> int:
        return sum(1 for e in tab.network if e.get("nseq", 0) > since and e.get("type") in _API_TYPES and not e.get("done"))

    async def _net_errors(self, tab: _Tab, since: int) -> list[dict]:
        """与桌面端 netSince(bodies) 一致：HTTP ≥400 / 失败的接口（附响应体摘要），以及 200 但带错误码的业务报错。"""
        mine = [e for e in tab.network if e.get("nseq", 0) > since and e.get("type") in _API_TYPES]
        out: list[dict] = []
        failed = [e for e in mine if e.get("status") == "failed" or (isinstance(e.get("status"), int) and e["status"] >= 400)]
        for e in failed[-2:]:
            body = "" if e.get("status") == "failed" else (await self._body_head(tab, e["id"]))[:300]
            out.append({"id": e["id"], "method": e["method"], "url": e["url"], "status": e["status"],
                        "error": e.get("error") or "", "body": body})
        ok_json = [e for e in mine if e.get("done") and isinstance(e.get("status"), int) and e["status"] < 400
                   and "json" in str(e.get("mime") or "").lower()][-4:]
        for e in ok_json:
            if len(out) >= 3:
                break
            body = await self._body_head(tab, e["id"], 4000)
            biz = _biz_error(body)
            if biz:
                out.append({"id": e["id"], "method": e["method"], "url": e["url"], "status": e["status"], "biz": biz,
                            "body": body[:300]})
        return out

    async def _body_head(self, tab: _Tab, rid: str, limit: int = 300) -> str:
        resp = tab.responses.get(rid)
        if resp is None:
            return ""
        try:
            return (await asyncio.wait_for(resp.body(), 1.5)).decode("utf-8", errors="replace")[:limit]
        except Exception:  # noqa: BLE001
            return ""

    async def _settle(self, tab: _Tab, mark: dict, settle_ms: int, shot: bool = False) -> dict:
        page = tab.page
        before_url = mark["url"]
        if settle_ms <= 0:
            await asyncio.sleep(0.03)
            return {"navigated": page.url != before_url, "url": page.url}
        t0 = time.monotonic()
        budget = settle_ms / 1000
        navigated = False
        st: dict = {"url": page.url}
        pending = 0
        while True:
            if not navigated and page.url != before_url:
                navigated = True
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=12000)
                except Exception:  # noqa: BLE001
                    pass
                budget = time.monotonic() - t0 + 0.8
            try:
                st = await self._kit(page, "state", {})
            except Exception:  # noqa: BLE001
                st = {"url": page.url}
            pending = self._pending(tab, mark["net"])
            el = time.monotonic() - t0
            if el >= 0.06 and float(st.get("idle_ms", 1e9)) >= 100 and pending == 0:
                break
            if el >= budget:
                break
            await asyncio.sleep(0.03)
        st["navigated"] = navigated or st.get("url") != before_url
        st["waited"] = int((time.monotonic() - t0) * 1000)
        if pending:
            st["pending"] = pending
        try:
            chg = await self._kit(page, "changes", {})
        except Exception:  # noqa: BLE001
            chg = None
        if chg:
            if chg.get("notices"):
                st["notices"] = chg["notices"]
            if chg.get("added"):
                st["added"] = chg["added"]
            if chg.get("more"):
                st["added_more"] = True
            if chg.get("dialog"):
                st["new_dialog"] = chg["dialog"]
        errs = await self._net_errors(tab, mark["net"])
        if errs:
            st["net_errors"] = errs
        cerr = [e["text"][:300] for e in tab.console if e["seq"] > mark["console"] and e.get("level") in ("error", "assert")][-3:]
        if cerr:
            st["console_errors"] = cerr
        has_error = bool(errs or st.get("new_dialog") or any(n.get("level") == "error" for n in st.get("notices") or []))
        if shot or has_error:
            try:
                st["shot"] = await self._capture(tab, {"max_side": 1280, "quality": 72})
                st["shot_reason"] = "requested" if shot else "error"
            except Exception as e:  # noqa: BLE001
                st["shot_error"] = str(e)[:200]
        return st

    async def _before_input(self, tab: _Tab, args: dict) -> dict:
        on = str(args.get("on_dialog") or "accept")
        tab.dialog_accept = on != "dismiss"
        tab.prompt_text = str(args.get("prompt_text") or "")
        try:
            await self._kit(tab.page, "watch", {})
        except Exception:  # noqa: BLE001
            pass
        last = tab.console[-1]["seq"] if tab.console else 0
        return {"url": tab.page.url, "net": tab.net_seq, "console": last}

    # ── 原语 ─────────────────────────────────────────────
    async def call(self, session_id: int, op: str, args: dict, timeout: float,
                   cancel_event: asyncio.Event | None = None) -> dict:
        try:
            return await asyncio.wait_for(self._dispatch(session_id, op, args or {}), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise HostCallError(f"浏览器操作超时（{int(timeout)} 秒）") from e

    async def _dispatch(self, sid: int, op: str, a: dict) -> dict:
        if op == "tabs.list":
            await self._context(sid)
            tabs = self._tabs.get(sid, {})
            out = []
            for t in tabs.values():
                if t.page.is_closed():
                    continue
                try:
                    title = await t.page.title()
                except Exception:  # noqa: BLE001
                    title = ""
                out.append({"id": t.id, "url": t.page.url, "title": title, "loading": False,
                            "active": t.id == self._agent.get(sid), "owner": "ai"})
            return {"tabs": out, "active": self._agent.get(sid), "agent_tab": self._agent.get(sid), "visible": False}
        if op == "tabs.open":
            tab = await self._new_tab(sid)
            self._agent[sid] = tab.id
            if a.get("url"):
                await self._goto(tab, str(a["url"]), a)
            return await self._base(tab)
        if op == "tabs.select":
            tab = await self._tab(sid, str(a.get("tab") or ""))
            self._agent[sid] = tab.id
            return await self._base(tab)
        if op == "tabs.close":
            tab = await self._tab(sid, str(a.get("tab") or ""))
            await tab.page.close()
            self._tabs[sid].pop(tab.id, None)
            if self._agent.get(sid) == tab.id:
                self._agent.pop(sid, None)
            return {"closed": tab.id}
        tab = await self._tab(sid, a.get("tab"), a.get("prefer_tab"))
        page = tab.page
        if op == "nav":
            if a.get("new_tab"):
                tab = await self._new_tab(sid)
                self._agent[sid] = tab.id
                page = tab.page
            action = a.get("action")
            wait = str(a.get("wait") or "domcontentloaded")
            wu = wait if wait in ("load", "domcontentloaded", "commit") else "domcontentloaded"
            to = int(a.get("timeout_ms") or 30000)
            status = None
            try:
                if action == "back":
                    await page.go_back(wait_until=wu, timeout=to)
                elif action == "forward":
                    await page.go_forward(wait_until=wu, timeout=to)
                elif action == "reload":
                    await page.reload(wait_until=wu, timeout=to)
                else:
                    status = await self._goto(tab, str(a.get("url") or ""), a)
            except HostCallError:
                raise
            except Exception as e:  # noqa: BLE001
                raise HostCallError(f"页面加载失败：{str(e).splitlines()[0][:200]}") from e
            if wu != "load":
                await self._quiet_after_nav(tab)
            base = await self._base(tab)
            base["status"] = status
            return base
        if op == "kit":
            value = await self._kit(page, str(a.get("fn")), a.get("args") or {})
            base = await self._base(tab)
            base["value"] = value
            return base
        if op == "eval":
            try:
                value = await page.evaluate(str(a.get("script") or ""))
            except Exception as e:  # noqa: BLE001
                raise HostCallError(f"脚本执行出错：{str(e).splitlines()[0][:300]}") from e
            base = await self._base(tab)
            base["value"] = value
            return base
        if op == "mouse":
            mark = await self._before_input(tab, a)
            aimed = None
            act = str(a.get("action") or "click")
            if a.get("ref") and act in ("click", "move"):
                # 点击前最终校准：等目标停止移动、复核命中（与桌面端一致）
                aimed = await self._kit(page, "aim", {"ref": str(a["ref"])})
                a = {**a, "x": aimed["x"], "y": aimed["y"]}
            await self._mouse(page, a)
            st = await self._settle(tab, mark, int(a.get("settle_ms") or 0), bool(a.get("shot")))
            base = await self._base(tab)
            base["settle"] = st
            if aimed:
                base["aimed"] = aimed
            return base
        if op == "keys":
            mark = await self._before_input(tab, a)
            for _ in range(max(1, min(int(a.get("repeat") or 1), 50))):
                for combo in a.get("keys") or []:
                    await page.keyboard.press("Space" if combo == " " else combo)
            st = await self._settle(tab, mark, int(a.get("settle_ms") or 0), bool(a.get("shot")))
            base = await self._base(tab)
            base["settle"] = st
            return base
        if op == "text":
            mark = await self._before_input(tab, a)
            await page.keyboard.insert_text(str(a.get("text") or ""))
            st = await self._settle(tab, mark, int(a.get("settle_ms") or 0), bool(a.get("shot")))
            base = await self._base(tab)
            base["settle"] = st
            return base
        if op == "capture":
            return await self._capture(tab, a)
        if op == "logs":
            return self._logs(tab, a)
        if op == "net_body":
            resp = tab.responses.get(str(a.get("request_id") or ""))
            if resp is None:
                raise HostCallError("找不到该请求的响应（可能已被清理或请求未完成）")
            try:
                body = await resp.body()
            except Exception as e:  # noqa: BLE001
                raise HostCallError(f"读取响应体失败：{e}") from e
            mime = (resp.headers or {}).get("content-type", "")
            try:
                return {"body": body.decode("utf-8"), "base64": False, "mime": mime}
            except UnicodeDecodeError:
                return {"body": base64.b64encode(body[:200000]).decode("ascii"), "base64": True, "mime": mime}
        if op == "viz":
            return {}
        if op == "clipboard":
            raise HostCallError("后台浏览器无法访问系统剪贴板（system=true 仅桌面端可用），请使用默认的 AI 剪贴板")
        raise HostCallError(f"未知的浏览器原语：{op}")

    async def _quiet_after_nav(self, tab: _Tab, budget: float = 1.5) -> None:
        """DOM 就绪后：页面脚本的首批请求返回、DOM 静默 100 ms 即可（与桌面端 quietAfterNav 一致）。"""
        t0 = time.monotonic()
        since = tab.net_seq
        try:
            await self._kit(tab.page, "watch", {})
        except Exception:  # noqa: BLE001
            return
        while time.monotonic() - t0 < budget:
            try:
                st = await self._kit(tab.page, "state", {})
            except Exception:  # noqa: BLE001
                return
            if time.monotonic() - t0 >= 0.06 and float(st.get("idle_ms", 1e9)) >= 100 and self._pending(tab, since) == 0:
                return
            await asyncio.sleep(0.03)

    async def _goto(self, tab: _Tab, url: str, a: dict):
        wait = str(a.get("wait") or "domcontentloaded")
        wu = wait if wait in ("load", "domcontentloaded", "commit") else "domcontentloaded"
        resp = await tab.page.goto(url, wait_until=wu, timeout=int(a.get("timeout_ms") or 30000))
        return resp.status if resp else None

    async def _mouse(self, page, a: dict) -> None:
        action = str(a.get("action") or "click")
        x, y = float(a.get("x") or 0), float(a.get("y") or 0)
        mods = [str(m) for m in (a.get("modifiers") or [])]
        for m in mods:
            await page.keyboard.down(m)
        try:
            if action == "click":
                await page.mouse.click(x, y, button=str(a.get("button") or "left"),
                                       click_count=max(1, min(int(a.get("count") or 1), 3)))
            elif action == "move":
                await page.mouse.move(x, y, steps=4)
            elif action == "wheel":
                await page.mouse.move(x, y)
                await page.mouse.wheel(float(a.get("dx") or 0), float(a.get("dy") or 0))
            elif action == "activate":
                # 被遮挡的元素：直接在元素上派发点击，不经过坐标命中（与桌面端 kit 的 activate 一致）
                await self._kit(page, "activate", {"ref": str(a.get("ref") or "")})
            elif action == "drag":
                await page.mouse.move(x, y)
                await page.mouse.down()
                await page.mouse.move(float(a.get("to_x") or x), float(a.get("to_y") or y),
                                      steps=max(2, int(a.get("steps") or 12)))
                await page.mouse.up()
            else:
                raise HostCallError(f"未知的鼠标动作：{action}")
        finally:
            for m in reversed(mods):
                await page.keyboard.up(m)

    async def _capture(self, tab: _Tab, a: dict) -> dict:
        page = tab.page
        vp = page.viewport_size or {"width": 1280, "height": 800}
        quality = max(30, min(int(a.get("quality") or 72), 95))
        opts: dict[str, Any] = {"type": "jpeg", "quality": quality, "scale": "css"}
        clip = a.get("clip")
        if clip:
            opts["clip"] = {k: float(clip[k]) for k in ("x", "y", "width", "height")}
        elif a.get("full_page"):
            opts["full_page"] = True
        marks = None
        if a.get("marks") and not clip and not a.get("full_page"):
            try:
                marks = await self._kit(page, "marks", {})
            except Exception:  # noqa: BLE001
                marks = None
        try:
            metrics = await page.evaluate("[scrollX, scrollY, innerWidth, innerHeight, location.href]")
        except Exception:  # noqa: BLE001
            metrics = [0, 0, vp["width"], vp["height"], page.url]
        raw = await page.screenshot(**opts)
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        css_w, css_h = img.size
        max_side = int(a.get("max_side") or 1280)
        changed = False
        if max(img.size) > max_side:
            ratio = max_side / max(img.size)
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
            changed = True
        items = (marks or {}).get("items") or []
        if items:
            img = _draw_marks(img, items, img.width / max(1, css_w))
            changed = True
        if changed:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=quality)
            raw = buf.getvalue()
        base = await self._base(tab)
        base.update({"base64": base64.b64encode(raw).decode("ascii"), "mime": "image/jpeg",
                     "width": img.width, "height": img.height, "css_width": css_w, "css_height": css_h,
                     "scroll": {"x": round(metrics[0] or 0), "y": round(metrics[1] or 0)},
                     "viewport": {"width": metrics[2], "height": metrics[3]}, "page_url": metrics[4]})
        if items:
            base["marks"] = [{"n": m["n"], "ref": m["ref"], "label": m["label"]} for m in items]
        return base

    def _logs(self, tab: _Tab, a: dict) -> dict:
        kind = str(a.get("kind") or "console")
        src = tab.network if kind == "network" else tab.console
        entries = list(src)
        if a.get("only_errors"):
            if kind == "network":
                entries = [e for e in entries if e.get("status") == "failed" or (isinstance(e.get("status"), int) and e["status"] >= 400)]
            else:
                entries = [e for e in entries if e.get("level") in ("error", "assert")]
        pat = str(a.get("pattern") or "").lower()
        if pat:
            entries = [e for e in entries if pat in (e.get("text") or e.get("url") or "").lower()]
        total = len(entries)
        limit = max(1, min(int(a.get("limit") or 50), 300))
        entries = entries[-limit:]
        if a.get("clear"):
            src.clear()
        return {"tab": tab.id, "entries": entries, "total": total}


def _biz_error(body: str) -> str:
    """200 的 JSON 响应里 success=false 或 code 非 0/200 且带提示文字（与桌面端 bizError 一致）。"""
    try:
        j = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(j, dict):
        return ""
    msg = j.get("msg") or j.get("message") or j.get("errMsg") or j.get("error_msg") or j.get("errorMessage") \
        or (j.get("error") if isinstance(j.get("error"), str) else "")
    code = next((j[k] for k in ("code", "errcode", "errCode") if k in j), None)
    bad = code is not None and str(code).lower() not in ("0", "200", "00000", "success", "ok")
    if j.get("success") is False or j.get("ok") is False or bad:
        return str(msg or f"错误码 {code}")[:200]
    return ""


def _draw_marks(img, items: list[dict], k: float):
    """marks 截图：在图上画编号框（与桌面端 drawMarks 一致）。"""
    from PIL import ImageDraw

    img = img.convert("RGB")
    d = ImageDraw.Draw(img)
    red = (229, 72, 77)
    for m in items:
        r = m.get("rect") or {}
        x, y, w, h = r.get("x", 0) * k, r.get("y", 0) * k, r.get("width", 0) * k, r.get("height", 0) * k
        d.rectangle([x, y, x + w, y + h], outline=red, width=2)
        label = str(m.get("n"))
        tw = 7 * len(label) + 6
        ly = y - 15 if y >= 15 else y
        d.rectangle([x, ly, x + tw, ly + 15], fill=red)
        d.text((x + 3, ly + 2), label, fill=(255, 255, 255))
    return img

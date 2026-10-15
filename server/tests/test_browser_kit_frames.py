"""页面工具包的框架（iframe）行为：在真实 Chromium 中验证（plan-20261015-082-polish）。

覆盖：同源框架展开与 ref、顶层坐标换算后的真实点击、被透明框架遮挡时的 activate、
框架内输入与选区、嵌套框架与框架内滚动容器、跨域框架的标注与坐标操作。

依赖本机安装的 Playwright 浏览器；找不到浏览器可执行文件时跳过。
"""
from __future__ import annotations

import http.server
import os
import re
import socketserver
import threading
from contextlib import asynccontextmanager

import pytest

from app.orchestration.browser.kit import KIT_SOURCE, kit_call_expression

playwright_api = pytest.importorskip("playwright.async_api")

_EXT_PAGE = """<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">
<button id="ext-btn" onclick="parent.postMessage('ext-clicked', '*')">外部按钮</button></body></html>"""

_INDEX_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>管理系统</title>
<style>
html,body{margin:0;height:100%;font:14px sans-serif;background:#fff}
.top{height:50px;background:#eee;display:flex;gap:10px;align-items:center;padding:0 10px}
#pay{position:absolute;left:300px;top:150px;width:120px;height:40px}
iframe{position:absolute;border:1px solid #888;background:#fff}
#wb{left:0;top:220px;width:600px;height:260px}
#cover{left:280px;top:130px;width:220px;height:120px;background:transparent}
#ext{left:640px;top:220px;width:300px;height:200px}
</style></head><body>
<div class="top"><button id="m1">日常工作</button><a href="#x">首页</a></div>
<button id="pay" onclick="window.__pay=(window.__pay||0)+1">付款</button>
<iframe id="wb" title="工作台" src="/frame.html"></iframe>
<iframe id="cover" title="遮罩框架" src="/cover.html"></iframe>
<iframe id="ext" title="外部站点" src="__EXT_URL__"></iframe>
<script>window.addEventListener('message', (e) => { window.__extMsg = e.data; });</script>
</body></html>"""

_FRAME_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>工作台</title></head>
<body style="margin:0;font:14px sans-serif">
<div style="padding:10px"><button id="v3" onclick="parent.window.__v3=(parent.window.__v3||0)+1">系统设置v3</button>
<label>搜索 <input id="q" aria-label="搜索框" placeholder="关键字"></label></div>
<iframe id="inner" title="嵌套" src="/inner.html" style="width:300px;height:80px;border:0"></iframe>
<div id="scroller" style="height:120px;width:300px;overflow:auto;border:1px solid #999"><div style="height:800px"><button id="far">远处按钮</button></div></div>
<div style="height:600px"></div>
</body></html>"""

_INNER_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8"></head>
<body style="margin:0"><button id="nb" onclick="window.top.__nested=(window.top.__nested||0)+1">嵌套按钮</button></body></html>"""

_COVER_PAGE = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:transparent"><div style="width:100%;height:100%;background:rgba(255,0,0,0.1)"></div></body></html>"""


def _handler(pages: dict[str, str]):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静默测试日志
            pass

        def do_GET(self):  # noqa: N802
            body = pages.get(self.path.split("?")[0])
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture(scope="module")
def site_url():
    """本地两个源：主页面（同源框架）与外部站点（另一端口，即跨域框架）。"""
    ext = _Server(("127.0.0.1", 0), _handler({"/ext.html": _EXT_PAGE}))
    ext_url = f"http://127.0.0.1:{ext.server_address[1]}/ext.html"
    pages = {
        "/index.html": _INDEX_PAGE.replace("__EXT_URL__", ext_url),
        "/frame.html": _FRAME_PAGE,
        "/inner.html": _INNER_PAGE,
        "/cover.html": _COVER_PAGE,
    }
    main = _Server(("127.0.0.1", 0), _handler(pages))
    for srv in (ext, main):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{main.server_address[1]}"
    main.shutdown()
    ext.shutdown()


@asynccontextmanager
async def _open_page(site_url: str):
    async with playwright_api.async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # 本机没有 Playwright 浏览器时跳过
            pytest.skip(f"没有可用的 Playwright Chromium：{exc}")
        try:
            ctx = await browser.new_context(viewport={"width": 1000, "height": 700})
            page = await ctx.new_page()
            await page.goto(site_url + "/index.html", wait_until="load")
            await page.evaluate(KIT_SOURCE)
            yield page
        finally:
            await browser.close()


async def _call(page, fn: str, args: dict | None = None) -> dict:
    res = await page.evaluate(kit_call_expression(fn, args or {}))
    assert res.get("ok"), res
    return res["value"]


async def test_snapshot_expands_same_origin_frames_and_marks_cross_origin(site_url):
    async with _open_page(site_url) as page:
        snap = await _call(page, "snapshot", {"max_chars": 20000})
    text = snap["text"]
    assert "框架「工作台」" in text
    assert 'button "系统设置v3"' in text and 'textbox "搜索框"' in text
    assert "框架「嵌套」" in text and 'button "嵌套按钮"' in text
    assert "框架「外部站点」（跨域" in text
    assert snap["cross_frames"] >= 1


async def test_frame_button_click_uses_top_level_coordinates(site_url):
    async with _open_page(site_url) as page:
        target = await _call(page, "resolve", {"text": "系统设置v3"})
        assert target["label"] == "按钮「系统设置v3」" and target.get("ref")
        assert not target.get("covered_by")
        # 坐标已换算到顶层视口：与框架内元素的真实位置一致
        real = await page.evaluate("""(() => {
            const f = document.getElementById('wb'); const b = f.contentDocument.getElementById('v3');
            const r = b.getBoundingClientRect(); const fr = f.getBoundingClientRect();
            return { x: Math.round(fr.left + f.clientLeft + r.left + r.width / 2), y: Math.round(fr.top + f.clientTop + r.top + r.height / 2) };
        })()""")
        assert (target["x"], target["y"]) == (real["x"], real["y"])
        await page.mouse.click(target["x"], target["y"])
        assert await page.evaluate("window.__v3 || 0") == 1
        # 同一元素的 ref 在重复定位时保持稳定
        assert (await _call(page, "resolve", {"ref": target["ref"]}))["ref"] == target["ref"]


async def test_covered_button_is_reported_and_activate_clicks_it(site_url):
    async with _open_page(site_url) as page:
        pay = await _call(page, "resolve", {"text": "付款"})
        assert pay.get("covered_by") == "框架「遮罩框架」"
        await _call(page, "activate", {"ref": pay["ref"]})
        assert await page.evaluate("window.__pay || 0") == 1


async def test_keyboard_input_and_selection_inside_frame(site_url):
    async with _open_page(site_url) as page:
        box = await _call(page, "resolve", {"text": "搜索框"})
        assert box["label"] == "输入框「搜索框」"
        await _call(page, "focus", {"ref": box["ref"]})
        await page.keyboard.type("hello")
        value = await page.evaluate("document.getElementById('wb').contentDocument.getElementById('q').value")
        assert value == "hello"
        assert "输入框「搜索框」" in (await _call(page, "state")).get("focus", "")
        await _call(page, "selectAll", {"ref": box["ref"]})
        assert (await _call(page, "selection"))["text"] == "hello"


async def test_nested_frame_and_scroller_inside_frame(site_url):
    async with _open_page(site_url) as page:
        nested = await _call(page, "resolve", {"text": "嵌套按钮"})
        await page.mouse.click(nested["x"], nested["y"])
        assert await page.evaluate("window.__nested || 0") == 1

        scroller = await _call(page, "resolve", {"selector": "#scroller"})
        before = await _call(page, "scrollPos", {"x": scroller["x"], "y": scroller["y"]})
        assert before["inner"] is True
        await page.mouse.move(scroller["x"], scroller["y"])
        await page.mouse.wheel(0, 150)
        await page.wait_for_timeout(250)
        after = await _call(page, "scrollPos", {"x": scroller["x"], "y": scroller["y"]})
        assert after["top"] > before["top"]


async def test_driver_actions_on_playwright_host(site_url, monkeypatch):
    """驱动层 → 后台浏览器宿主 → 页面工具包：框架内点击与被遮挡后的强制点击（端到端）。"""
    from app.orchestration.browser import driver as drv
    from app.orchestration.browser.playwright_host import PlaywrightHost

    host = PlaywrightHost()

    async def _acquire():
        return host

    monkeypatch.setattr(drv, "acquire_host", _acquire)
    sid = 987654
    saved_browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    try:
        out = await drv.execute(sid, "navigate", {"url": site_url + "/index.html"})
        if not out.ok and "浏览器" in (out.error or ""):
            pytest.skip(out.error)
        assert out.ok, out.error
        snap = await drv.execute(sid, "snapshot", {})
        assert snap.ok, snap.error
        match = re.search(r'button "系统设置v3" \[(ref_\d+)\]', snap.text)
        assert match, snap.text
        out = await drv.execute(sid, "click", {"ref": match.group(1)})
        assert out.ok, out.error
        out = await drv.execute(sid, "evaluate", {"script": "window.__v3 || 0"})
        assert out.ok and "1" in out.text, out.text

        out = await drv.execute(sid, "click", {"text": "付款"})
        assert not out.ok and "遮挡" in out.error
        out = await drv.execute(sid, "click", {"text": "付款", "force": True})
        assert out.ok and "直接在元素上触发点击" in out.text, out.error
        out = await drv.execute(sid, "evaluate", {"script": "window.__pay || 0"})
        assert out.ok and "1" in out.text, out.text
    finally:
        browser = getattr(host, "_browser", None)
        pw = getattr(host, "_pw", None)
        if browser is not None:
            await browser.close()
        if pw is not None:
            await pw.stop()
        # PlaywrightHost 会把内置浏览器目录写入进程环境变量；测试结束后还原，避免影响同进程的其它浏览器测试
        if saved_browsers is None:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = saved_browsers


async def test_cross_origin_frame_is_marked_and_reachable_by_coordinates(site_url):
    async with _open_page(site_url) as page:
        res = await page.evaluate(kit_call_expression("resolve", {"text": "外部按钮"}))
        assert res["ok"] is False  # 跨域内容不可读，文字查找不到
        box = await page.locator("#ext").bounding_box()
        x, y = box["x"] + 20, box["y"] + 14
        point = await _call(page, "resolve", {"x": x, "y": y})
        assert point["label"] == "框架「外部站点」"
        await page.mouse.click(x, y)
        await page.wait_for_timeout(150)
        assert await page.evaluate("window.__extMsg || ''") == "ext-clicked"
        assert (await _call(page, "text"))["cross_frames"] >= 1

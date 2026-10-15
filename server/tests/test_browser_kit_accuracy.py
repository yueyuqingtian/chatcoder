"""内置浏览器操作准确性与报错回报：在真实 Chromium 中验证（plan-20261015-browser-perf）。

覆盖：动画菜单的点击前校准、提示文字捕获（一闪而过也保留）、新出现元素回报、旧 ref 行内容校验、
文字歧义报错、坐标就近吸附、截图坐标守卫、噪声节点过滤、marks 编号；以及经 Playwright 兜底宿主的
端到端动作（接口错误与业务报错写进结果并自动附图）。

依赖本机 Chromium 内核浏览器（Playwright 自带或 Edge / Chrome）；都没有时跳过。
"""
from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
from contextlib import asynccontextmanager

import pytest

from app.orchestration.browser.kit import KIT_SOURCE, kit_call_expression

playwright_api = pytest.importorskip("playwright.async_api")

from tests.test_browser_kit_frames import launch_any  # noqa: E402

_LAB = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>实验页</title>
<style>
body{margin:0;padding:20px;font:14px sans-serif}
.card{position:relative;border:1px solid #ddd;padding:14px;margin-bottom:12px}
.menu{position:absolute;left:14px;top:52px;width:160px;background:#fff;border:1px solid #ccc;z-index:10;
  transform-origin:top;transition:transform 500ms ease,opacity 500ms ease}
.menu.enter{transform:translateY(-90px) scaleY(0.5);opacity:.2}
.mi{padding:8px 12px;cursor:pointer}
.toast{position:fixed;top:16px;left:50%;padding:8px 16px}
#msgbox{position:fixed;top:60px;right:20px;padding:8px;display:none}
.icon-btn{position:absolute;left:520px;top:14px;width:24px;height:24px;padding:0}
.spacer{height:1600px}
</style></head><body>
<div class="card" style="height:240px">
  <button id="menu-btn">打开菜单</button>
  <div id="menu" class="menu" hidden>
    <div class="mi" role="menuitem" data-v="1">选项一</div><div class="mi" role="menuitem" data-v="2">选项二</div>
    <div class="mi" role="menuitem" data-v="3">选项三</div><div class="mi" role="menuitem" data-v="4">选项四</div>
  </div>
  <button class="icon-btn" id="icon-btn" aria-label="设置">⚙</button>
  <span id="clock">0</span>
</div>
<div class="card"><button id="save-btn">保存</button><button id="order-btn">提交订单</button></div>
<div id="msgbox" class="message"></div>
<div class="card"><table id="tbl">
  <tr><td class="nm">张三</td><td><button class="del">删除</button></td></tr>
  <tr><td class="nm">王五</td><td><button class="del">删除</button></td></tr>
  <tr><td class="nm">赵六</td><td><button class="del">删除</button></td></tr>
</table><button id="next-page">下一页</button></div>
<div class="spacer"></div>
<script>
window.__lab = { menu: [], deleted: [], icon: 0 };
const menu = document.getElementById('menu');
document.getElementById('menu-btn').addEventListener('click', () => {
  menu.hidden = false; menu.classList.add('enter'); void menu.offsetHeight;
  requestAnimationFrame(() => menu.classList.remove('enter'));
});
menu.addEventListener('click', (e) => { const mi = e.target.closest('.mi'); if (!mi) return; window.__lab.menu.push(mi.dataset.v); menu.hidden = true; });
document.getElementById('icon-btn').addEventListener('click', () => { window.__lab.icon++; });
document.getElementById('save-btn').addEventListener('click', async () => {
  const r = await fetch('/api/fail', { method: 'POST', body: '{}' }); const j = await r.json();
  const t = document.createElement('div'); t.className = 'toast toast-error'; t.setAttribute('role', 'alert');
  t.textContent = '保存失败：' + j.msg; document.body.appendChild(t); setTimeout(() => t.remove(), 1200);
});
document.getElementById('order-btn').addEventListener('click', async () => {
  const r = await fetch('/api/biz'); const j = await r.json();
  if (j.code !== 0) { const b = document.getElementById('msgbox'); b.textContent = j.msg; b.style.display = 'block'; setTimeout(() => { b.style.display = 'none'; }, 1000); }
});
const pages = [['张三', '王五', '赵六'], ['李四', '钱七', '孙八']]; let pg = 0;
document.getElementById('next-page').addEventListener('click', () => {
  pg = (pg + 1) % 2; document.querySelectorAll('#tbl .nm').forEach((td, i) => { td.textContent = pages[pg][i]; });
});
document.querySelectorAll('#tbl .del').forEach((b) => b.addEventListener('click', () => {
  window.__lab.deleted.push(b.closest('tr').querySelector('.nm').textContent);
}));
let n = 0; setInterval(() => { document.getElementById('clock').textContent = String(++n); }, 16);
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: str, ctype: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/lab.html"):
            self._send(200, _LAB, "text/html; charset=utf-8")
        elif self.path.startswith("/api/biz"):
            time.sleep(0.25)
            self._send(200, json.dumps({"code": 40001, "msg": "库存不足，无法下单"}, ensure_ascii=False), "application/json")
        else:
            self._send(404, "", "text/plain")

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(0.3)
        self._send(500, json.dumps({"code": 500, "msg": "名称不能为空"}, ensure_ascii=False), "application/json")


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture(scope="module")
def lab_url():
    srv = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/lab.html"
    srv.shutdown()


@asynccontextmanager
async def _open(lab_url: str):
    async with playwright_api.async_playwright() as pw:
        browser = await launch_any(pw)
        try:
            ctx = await browser.new_context(viewport={"width": 900, "height": 700})
            page = await ctx.new_page()
            await page.goto(lab_url, wait_until="load")
            await page.evaluate(KIT_SOURCE)
            yield page
        finally:
            await browser.close()


async def _call(page, fn: str, args: dict | None = None):
    return await page.evaluate(kit_call_expression(fn, args or {}))


async def _ok(page, fn: str, args: dict | None = None):
    res = await _call(page, fn, args)
    assert res.get("ok"), res
    return res["value"]


async def test_aim_waits_for_menu_animation(lab_url):
    async with _open(lab_url) as page:
        for _ in range(5):
            btn = await _ok(page, "resolve", {"selector": "#menu-btn"})
            await page.mouse.click(btn["x"], btn["y"])
            # 展开动画刚开始就定位：此时的坐标与动画结束后的位置不同
            target = await _ok(page, "resolve", {"text": "选项三"})
            aimed = await _ok(page, "aim", {"ref": target["ref"]})
            await page.mouse.click(aimed["x"], aimed["y"])
        assert await page.evaluate("window.__lab.menu") == ["3"] * 5


async def test_flash_toast_and_api_error_are_captured(lab_url):
    async with _open(lab_url) as page:
        await _ok(page, "watch")
        btn = await _ok(page, "resolve", {"selector": "#save-btn"})
        await page.mouse.click(btn["x"], btn["y"])
        await page.wait_for_timeout(1700)  # 提示 1.2 秒后已移除
        chg = await _ok(page, "changes")
        assert chg["notices"] and "名称不能为空" in chg["notices"][0]["text"]
        assert chg["notices"][0]["level"] == "error" and chg["notices"][0]["gone"] is True

        await _ok(page, "watch")
        btn = await _ok(page, "resolve", {"selector": "#order-btn"})
        await page.mouse.click(btn["x"], btn["y"])
        await page.wait_for_timeout(500)
        chg = await _ok(page, "changes")
        assert any("库存不足" in n["text"] and n["level"] == "error" for n in chg["notices"])


async def test_new_menu_items_are_reported(lab_url):
    async with _open(lab_url) as page:
        await _ok(page, "watch")
        btn = await _ok(page, "resolve", {"selector": "#menu-btn"})
        await page.mouse.click(btn["x"], btn["y"])
        await page.wait_for_timeout(600)
        chg = await _ok(page, "changes")
        assert any('menuitem "选项三" [ref_' in line for line in chg["added"])


async def test_stale_ref_after_in_place_rerender_is_rejected(lab_url):
    async with _open(lab_url) as page:
        snap = await _ok(page, "snapshot", {})
        line = next(ln for ln in snap["text"].splitlines() if "删除" in ln)
        ref = line.split("[")[1].split("]")[0]
        nxt = await _ok(page, "resolve", {"selector": "#next-page"})
        await page.mouse.click(nxt["x"], nxt["y"])
        res = await _call(page, "resolve", {"ref": ref})
        assert not res["ok"] and "所在行" in res["error"] and "李四" in res["error"]
        # 重新读取后同一个 ref 恢复可用（不会一直报错）
        await _ok(page, "snapshot", {})
        await _ok(page, "resolve", {"ref": ref})


async def test_ambiguous_text_lists_candidates(lab_url):
    async with _open(lab_url) as page:
        res = await _call(page, "resolve", {"text": "删除"})
        assert not res["ok"] and "3 个" in res["error"] and "张三" in res["error"] and "ref_" in res["error"]
        assert await page.evaluate("window.__lab.deleted") == []
        ok = await _ok(page, "resolve", {"text": "提交订单"})
        assert ok["label"] == "按钮「提交订单」"


async def test_coordinate_near_small_button_snaps(lab_url):
    async with _open(lab_url) as page:
        r = await page.evaluate("(() => { const b = document.getElementById('icon-btn').getBoundingClientRect(); return {x: b.right + 5, y: b.top + b.height / 2}; })()")
        info = await _ok(page, "resolve", {"x": r["x"], "y": r["y"]})
        assert info.get("snapped") and "设置" in info["label"]
        await page.mouse.click(info["x"], info["y"])
        assert await page.evaluate("window.__lab.icon") == 1
        # 空白处（附近没有可交互元素）照常按原坐标
        blank = await _ok(page, "resolve", {"x": 400, "y": 600})
        assert not blank.get("snapped") and (blank["x"], blank["y"]) == (400, 600)


async def test_shot_guard_compensates_scroll_and_rejects_stale(lab_url):
    async with _open(lab_url) as page:
        r = await page.evaluate("(() => { const b = document.getElementById('save-btn').getBoundingClientRect(); return {x: b.left + 10, y: b.top + 8}; })()")
        shot = {"kind": "viewport", "sx": 0, "sy": 0, "vw": 900, "vh": 700, "url": page.url}
        await page.evaluate("window.scrollTo(0, 70)")
        info = await _ok(page, "resolve", {"x": r["x"], "y": r["y"], "shot": shot})
        assert "滚动" in info.get("note", "") and info["label"] == "按钮「保存」"
        bad = await _call(page, "resolve", {"x": 10, "y": 10, "shot": {**shot, "vw": 800}})
        assert not bad["ok"] and "视口尺寸" in bad["error"]
        bad = await _call(page, "resolve", {"x": 10, "y": 10, "shot": {**shot, "url": "http://other.test/"}})
        assert not bad["ok"] and "跳转" in bad["error"]


async def test_noisy_clock_does_not_block_idle(lab_url):
    async with _open(lab_url) as page:
        await _ok(page, "watch")
        await page.wait_for_timeout(2500)  # 时钟每 16ms 更新：过滤后应能静默
        st = await _ok(page, "state")
        assert st["idle_ms"] >= 100


async def test_marks_list_viewport_elements(lab_url):
    async with _open(lab_url) as page:
        m = await _ok(page, "marks")
        labels = [i["label"] for i in m["items"]]
        assert m["items"][0]["n"] == 1 and any('"保存"' in x for x in labels)
        assert all(i["ref"].startswith("ref_") for i in m["items"])


async def test_driver_click_reports_api_error_with_screenshot(lab_url, monkeypatch, tmp_path):
    """经 Playwright 兜底宿主的端到端：点击后等接口返回，报错提示、HTTP 500 与业务报错进结果，并自动附图。"""
    from app.core.config import settings
    from app.orchestration.browser import driver as drv
    from app.orchestration.browser.playwright_host import PlaywrightHost

    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path), raising=False)
    drv._states.clear()
    async with playwright_api.async_playwright() as pw:
        host = PlaywrightHost()
        host._pw = pw
        host._browser = await launch_any(pw)

        async def _acquire():
            return host

        monkeypatch.setattr(drv, "acquire_host", _acquire)
        try:
            out = await drv.execute(901, "navigate", {"url": lab_url})
            assert out.ok and "视口内的可交互元素" in out.text
            out = await drv.execute(901, "click", {"selector": "#save-btn"})
            assert out.ok, out.error
            assert "名称不能为空" in out.text and "HTTP 500" in out.text and out.image_b64
            out = await drv.execute(901, "click", {"selector": "#order-btn"})
            assert "库存不足" in out.text and out.image_b64
            out = await drv.execute(901, "click", {"text": "删除"})
            assert not out.ok and "3 个" in out.error
            out = await drv.execute(901, "batch", {"actions": [{"action": "click", "selector": "#menu-btn"},
                                                              {"action": "click", "text": "选项三"}]})
            assert out.ok, out.error
            assert await host._tabs[901][host._agent[901]].page.evaluate("window.__lab.menu") == ["3"]
        finally:
            await host._browser.close()
            drv._states.clear()

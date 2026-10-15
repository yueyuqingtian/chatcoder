"""内置浏览器操作：延迟基准 + 准确性场景（真实 Electron 宿主链路，plan-20261015-browser-perf）。

链路与生产一致：driver.execute → bridge → /ws/global → 前端 host.ts → IPC → browser-agent.cjs → webview。
测量前确认测试窗口没有被其它窗口完全遮挡（被遮挡时宿主定时器会被节流，结果会量化到 1 秒级，见交接文档 §3.1）；
需要专门测「后台」时再把窗口遮住或最小化，两种情况分开记录。

用法（先按交接文档 §6 启动 server.py 与 main.cjs）：
    server/.venv/Scripts/python.exe docs/handoff/browser-perf-e2e/bench.py 22973 after.json
对比基线：docs/handoff/browser-perf-e2e/baseline-20261015.json（改造前，测试窗口被遮挡状态）
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
import time
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 22973
OUT = sys.argv[2] if len(sys.argv) > 2 else "bench.json"
CDP = 9339
BASE = f"http://127.0.0.1:{PORT}"
SITE = f"{BASE}/e2e-site/index.html"
LAB = f"{BASE}/e2e-site/lab.html"

results: dict = {"latency": {}, "accuracy": {}, "notes": []}


def post(path: str, body: dict, timeout: float = 90) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run(action: str, args: dict | None = None) -> tuple[dict, float]:
    t0 = time.perf_counter()
    r = post("/e2e/run", {"action": action, "args": args or {}})
    return r, (time.perf_counter() - t0) * 1000


def page(script: str):
    r = post("/e2e/eval_page", {"script": script})
    return r.get("value")


def lat(name: str, action: str, args: dict | None = None, n: int = 5, before=None) -> dict | None:
    times = []
    last = None
    for _ in range(n):
        if before:
            before()
        r, ms = run(action, args)
        last = r
        if not r.get("ok"):
            print(f"  [{name}] 失败：{r.get('error')}")
        times.append(ms)
    med = statistics.median(times)
    results["latency"][name] = {"median_ms": round(med), "all": [round(t) for t in times]}
    print(f"  {name:<22} 中位 {med:6.0f} ms   {[round(t) for t in times]}")
    return last


def find_ref(snap: str, needle: str, nth: int = 0) -> str:
    hits = [ln for ln in snap.splitlines() if needle in ln]
    if len(hits) <= nth:
        return ""
    m = re.search(r"ref_\d+", hits[nth])
    return m.group(0) if m else ""


async def set_zoom(factor: float) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP}")
        try:
            for ctx in browser.contexts:
                for p in ctx.pages:
                    if p.url.startswith("file:"):
                        return await p.evaluate("""async (f) => {
                          const wv = Array.from(document.querySelectorAll('webview')).find((w) => (w.getURL() || '').includes('lab.html'));
                          if (!wv) return 'no-webview';
                          const r = await window.chatcoderAPI.browserAgentExec(wv.getWebContentsId(), 'setZoom', { factor: f });
                          return JSON.stringify(r);
                        }""", factor)
        finally:
            await browser.close()
    return "no-page"


def main() -> None:
    r = post("/e2e/wait_host", {"timeout": 60}, timeout=70)
    if not r.get("ok"):
        print("宿主未登记", r)
        return
    print("== 延迟（index.html）==")
    r, ms = run("navigate", {"url": SITE})
    print(f"  首次导航 {ms:.0f} ms ok={r.get('ok')} {r.get('error') or ''}")
    lat("navigate(同页刷新)", "navigate", {"url": SITE}, n=3)
    snap = lat("snapshot", "snapshot", {}, n=3)
    ref_hello = find_ref((snap or {}).get("text", ""), "点击我")
    lat("click(ref,DOM变化)", "click", {"ref": ref_hello})
    lat("click(selector)", "click", {"selector": "#btn-twice"})
    lat("click(无变化)", "click", {"selector": "#page-title"})
    lat("hover", "hover", {"selector": "#hover-me"})
    lat("type(10字)", "type_text", {"selector": "#input-text", "text": "abcdefghij"})
    lat("press(End)", "press", {"keys": "End"})
    lat("scroll(down)", "scroll", {"direction": "down", "amount": 300})
    lat("scroll(top)", "scroll", {"to": "top"})
    lat("screenshot", "screenshot", {})
    lat("get_text", "get_text", {}, n=3)

    print("== 准确性（lab.html）==")
    r, ms = run("navigate", {"url": LAB})
    print(f"  打开实验页 {ms:.0f} ms ok={r.get('ok')} {r.get('error') or ''}")
    acc = results["accuracy"]

    # a. 动画菜单：打开后立即点「选项三」
    hits = []
    for i in range(3):
        page("document.getElementById('menu').hidden = true; window.__lab.menu = []; 1")
        r, ms = run("batch", {"actions": [{"action": "click", "selector": "#menu-btn"}, {"action": "click", "text": "选项三"}]})
        got = page("JSON.stringify(window.__lab.menu)")
        hits.append(got)
        print(f"  动画菜单第{i + 1}次：选中 {got}（期望 [\"3\"]） {ms:.0f} ms ok={r.get('ok')} {(r.get('error') or '')[:80]}")
    acc["animated_menu"] = hits

    # b. 失败接口 + 一闪而过的错误提示
    page("document.getElementById('menu').hidden = true; 1")
    r, ms = run("click", {"selector": "#save-btn"})
    text = r.get("text") or ""
    acc["toast_error"] = {"mentions_msg": "名称不能为空" in text, "mentions_500": "500" in text, "has_image": r.get("has_image"), "ms": round(ms), "text": text}
    print(f"  失败接口：提到报错={'名称不能为空' in text} 提到500={'500' in text} 附图={r.get('has_image')} {ms:.0f} ms")
    print("    " + text.replace("\n", "\n    "))

    # c. 业务错误（200 + code≠0）
    r, ms = run("click", {"selector": "#order-btn"})
    text = r.get("text") or ""
    acc["biz_error"] = {"mentions_msg": "库存不足" in text, "ms": round(ms), "text": text}
    print(f"  业务错误：提到库存不足={'库存不足' in text} {ms:.0f} ms")
    print("    " + text.replace("\n", "\n    "))

    # d. 慢接口（450ms 后才更新 DOM）
    r, ms = run("click", {"selector": "#slow-btn"})
    text = r.get("text") or ""
    acc["slow_api"] = {"reports_change": "没有可见变化" not in text, "ms": round(ms), "text": text}
    print(f"  慢接口：报告了变化={'没有可见变化' not in text} {ms:.0f} ms")
    print("    " + text.replace("\n", "\n    "))

    # e. 列表就地重渲染后的旧 ref
    snap, _ = run("snapshot", {})
    ref_del0 = find_ref(snap.get("text", ""), "删除", 0)
    run("click", {"selector": "#next-page"})
    page("window.__lab.deleted = []; 1")
    r, ms = run("click", {"ref": ref_del0})
    got = page("JSON.stringify(window.__lab.deleted)")
    acc["stale_ref"] = {"deleted": got, "ok": r.get("ok"), "error": r.get("error")}
    print(f"  旧 ref（原张三行）：删除了 {got} ok={r.get('ok')} {(r.get('error') or '')[:100]}")
    run("click", {"selector": "#next-page"})

    # f. 文字有歧义
    page("window.__lab.deleted = []; 1")
    r, ms = run("click", {"text": "删除"})
    got = page("JSON.stringify(window.__lab.deleted)")
    acc["ambiguous_text"] = {"deleted": got, "ok": r.get("ok"), "error": r.get("error")}
    print(f"  歧义文字「删除」：删除了 {got} ok={r.get('ok')} {(r.get('error') or '')[:120]}")

    # g. 坐标点在小图标按钮外 5px（窄视口下图标在视口右侧之外：先横向滚入，保证坐标在视口内）
    run("scroll", {"to": "top"})
    page("document.getElementById('icon-btn').scrollIntoView({block: 'nearest', inline: 'center'}); 1")
    shot, _ = run("screenshot", {})
    w = (shot.get("data") or {}).get("width") or 0
    geo = page("(() => { const r = document.getElementById('icon-btn').getBoundingClientRect(); return {x: r.right + 5, y: r.top + r.height / 2, vw: innerWidth}; })()")
    if geo and w:
        sc = w / geo["vw"]
        page("window.__lab.icon = 0; 1")
        r, ms = run("click", {"x": round(geo["x"] * sc), "y": round(geo["y"] * sc)})
        got = page("window.__lab.icon")
        acc["near_miss_coordinate"] = {"icon_clicks": got, "text": r.get("text"), "error": r.get("error")}
        print(f"  坐标偏出图标 5px：图标被点击 {got} 次  {(r.get('text') or r.get('error') or '')[:100]}")

    # h. 截图后页面滚动，再按旧截图坐标点
    run("scroll", {"to": "top"})
    page("window.scrollTo(0, 120); 1")
    shot, _ = run("screenshot", {})
    w = (shot.get("data") or {}).get("width") or 0
    geo = page("(() => { const r = document.getElementById('scroll-target-b').getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2, vw: innerWidth}; })()")
    if geo and w:
        sc = w / geo["vw"]
        run("scroll", {"direction": "down", "amount": 70})
        page("window.__lab.targets = []; 1")
        r, ms = run("click", {"x": round(geo["x"] * sc), "y": round(geo["y"] * sc)})
        got = page("JSON.stringify(window.__lab.targets)")
        acc["stale_shot_scroll"] = {"targets": got, "text": r.get("text"), "error": r.get("error")}
        print(f"  截图后滚动 70px 再按旧坐标点「目标乙」：实际点击 {got}  {(r.get('text') or r.get('error') or '')[:120]}")

    # i. 网页缩放 125%
    run("scroll", {"to": "top"})
    z = asyncio.run(set_zoom(1.25))
    print(f"  设置缩放 1.25：{z}")
    time.sleep(0.3)
    page("window.__lab.far = 0; 1")
    r, ms = run("click", {"selector": "#far-btn"})
    got = page("window.__lab.far")
    acc["zoom_125"] = {"far_clicks": got, "text": r.get("text"), "error": r.get("error")}
    print(f"  缩放 125% 点「远处按钮」：命中 {got} 次  {(r.get('text') or r.get('error') or '')[:100]}")
    asyncio.run(set_zoom(1.0))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("已写入", OUT)


if __name__ == "__main__":
    main()

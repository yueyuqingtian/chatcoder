"""探测 E2E 主窗口（宿主渲染进程）的可见性与定时器节流（plan-20261015-browser-perf）。

为什么用裸 CDP 而不是 Playwright connect_over_cdp：Playwright 接管页面时会开启焦点模拟，
页面随之报告 visible / hasFocus=true，正好掩盖了要观察的「窗口被遮挡 → 页面 hidden → 定时器被节流」。

单次 setTimeout 可能恰好落在节流对齐点附近而显得正常，所以连续测 5 次链式等待（host.ts 的轮询就是链式的）。
用法：先按交接文档 §6 启动 server.py 与 main.cjs，再运行
    server/.venv/Scripts/python.exe docs/handoff/browser-perf-e2e/probe_host.py [cdp_port]
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

import websockets

CDP = int(sys.argv[1]) if len(sys.argv) > 1 else 9339

CHAINED = """new Promise((resolve) => {
  const out = []; let t0 = performance.now(); let n = 0;
  const tick = () => {
    const t = performance.now(); out.push(Math.round(t - t0)); t0 = t;
    if (++n < 5) setTimeout(tick, 40); else resolve(out);
  };
  setTimeout(tick, 40);
})"""


async def main() -> None:
    targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json").read())
    for t in targets:
        print(t["type"], t["url"][:90])
    page = next(t for t in targets if t["type"] == "page" and t["url"].startswith("file:"))
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
        seq = 0

        async def ev(expr: str):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": "Runtime.evaluate",
                                      "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == seq:
                    return m["result"]["result"].get("value")

        print("宿主页面：", await ev("JSON.stringify({vis: document.visibilityState, focus: document.hasFocus()})"))
        print("链式 setTimeout(40) ×5 实测间隔(ms)：", await ev(CHAINED), "（接近 1000 说明宿主定时器被后台节流）")


if __name__ == "__main__":
    asyncio.run(main())

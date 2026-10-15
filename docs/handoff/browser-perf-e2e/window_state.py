"""把 E2E 测试窗口最小化 / 恢复（plan-20261015-browser-perf：「后台」场景复测用）。

用裸 CDP 调主窗口页面的 chatcoderAPI（测试主进程 main.cjs 把 window:minimize / maximizeToggle 接到真实窗口）。
用法：server/.venv/Scripts/python.exe docs/handoff/browser-perf-e2e/window_state.py minimize|restore [cdp_port]
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

import websockets

ACTION = sys.argv[1] if len(sys.argv) > 1 else "minimize"
CDP = int(sys.argv[2]) if len(sys.argv) > 2 else 9339


async def main() -> None:
    targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json").read())
    page = next(t for t in targets if t["type"] == "page" and t["url"].startswith("file:"))
    expr = "window.chatcoderAPI.minimizeWindow()" if ACTION == "minimize" else "window.chatcoderAPI.toggleMaximize()"
    async with websockets.connect(page["webSocketDebuggerUrl"]) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expr}}))
        await ws.recv()
    print("ok", ACTION)


if __name__ == "__main__":
    asyncio.run(main())

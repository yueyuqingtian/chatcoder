"""隔离 E2E 服务：真实 driver + /ws/global 宿主协议 + 测试站点（plan-20261015-browser-perf 基准脚手架）。

独立端口、不建库、不碰已安装应用（12973）的数据；用法见交接文档 docs/handoff/browser-perf-handoff.md §6。
启动：server/.venv/Scripts/python.exe docs/handoff/browser-perf-e2e/server.py 22973
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 本文件位于 docs/handoff/browser-perf-e2e/，仓库根目录在上三级
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "server"))

import uvicorn  # noqa: E402
import pydantic  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.orchestration.browser.bridge import bridge  # noqa: E402
from app.orchestration.browser import driver  # noqa: E402

SITE_DIR = Path(__file__).resolve().parent / "site"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origin_regex=".*", allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


@app.get("/e2e-site/ping.txt")
async def ping():
    return JSONResponse(content={"ok": True}, media_type="text/plain")


@app.post("/e2e-site/api/fail")
async def api_fail():
    await asyncio.sleep(0.3)
    return JSONResponse(status_code=500, content={"code": 500, "msg": "名称不能为空"})


@app.get("/e2e-site/api/biz")
async def api_biz():
    await asyncio.sleep(0.25)
    return JSONResponse(content={"code": 40001, "msg": "库存不足，无法下单"})


@app.get("/e2e-site/api/slow")
async def api_slow():
    await asyncio.sleep(0.45)
    return JSONResponse(content={"code": 0, "value": 42})


app.mount("/e2e-site", StaticFiles(directory=str(SITE_DIR), html=True), name="site")


@app.websocket("/ws/global")
async def ws_global(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = data.get("event")
            payload = data.get("payload") or {}
            if event == "browser.response":
                bridge.resolve(payload)
            elif event == "browser.host.hello":
                await bridge.register(ws, payload)
            elif event == "browser.host.bye":
                bridge.unregister(ws)
    except Exception:
        pass
    finally:
        bridge.unregister(ws)


class Step(pydantic.BaseModel):
    action: str
    args: dict = {}
    timeout: float = 30.0


@app.post("/e2e/run")
async def e2e_run(step: Step):
    try:
        out = await driver.execute(1, step.action, dict(step.args))
        data = {k: v for k, v in (out.data or {}).items() if k not in ("base64",)}
        return {"ok": out.ok, "text": out.text, "error": out.error, "data": data,
                "has_image": bool(out.image_b64), "image_len": len(out.image_b64 or "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@app.post("/e2e/eval_page")
async def eval_page(body: dict):
    out = await driver.execute(1, "evaluate", {"script": str(body.get("script") or "")})
    return {"ok": out.ok, "value": (out.data or {}).get("result"), "text": out.text, "error": out.error}


@app.post("/e2e/wait_host")
async def wait_host(body: dict):
    deadline = asyncio.get_event_loop().time() + float(body.get("timeout", 30))
    while asyncio.get_event_loop().time() < deadline:
        if bridge.has_host():
            return {"ok": True}
        await asyncio.sleep(0.3)
    return {"ok": False, "error": "宿主未在超时内登记"}


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 22973
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", http="h11")


if __name__ == "__main__":
    main()

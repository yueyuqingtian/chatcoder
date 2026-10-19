"""电脑操控点击精度实测（端到端）——在你自己的机器、当前屏幕缩放比例下量出「点哪儿、落哪儿」的真实偏差。

为什么需要它：屏幕缩放（100% / 110% / 125% / 150% / 200%）、多显示器、分辨率不同，点击偏移的根因往往不在换算公式，
而在「进程是否真的按物理像素工作」「系统把光标放到了哪」这类运行时事实。本脚本把整条链路真实跑一遍：

  1. 内核自检：DPI 感知级别（应为 PerMonitorV2）、各显示器缩放、光标落点自检（calib）；
  2. 打开一个带已知元素位置的测试页（临时 Chrome 配置，不碰你的浏览器与登录态）；
  3. 按**元素**点击：读无障碍树 → 点每个元素中心 → 读回页面实际收到的点击坐标；
  4. 按**坐标**点击（理想模型：在截图里精确找到目标）：截图缩放 → 工具换算 → 点击 → 读回页面落点；
  5. 打印每个点的偏差（物理像素）与是否命中目标元素；任何一项超过阈值时退出码为 1。

只会在测试页窗口里点击（不会点到别的窗口：每次点击前先把测试窗口激活到前台，内核另有前台校验）；
运行期间请不要动鼠标，约 20 秒。

用法（在仓库根目录、server 的虚拟环境里）：
    server\\.venv\\Scripts\\python.exe tools\\desktop-core\\test\\calibrate-e2e.py
    server\\.venv\\Scripts\\python.exe tools\\desktop-core\\test\\calibrate-e2e.py --scale 1.25   # 模拟页面 125% 缩放

依赖：Windows、已安装的 Google Chrome、内核已编译（tools/desktop-core/build.ps1）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "server"))

PAGE_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>CC-CALIB 标定页</title>
<style>html,body{margin:0;background:#eef3fb;font-family:"Microsoft YaHei",sans-serif}
.t{position:absolute;width:40px;height:24px;background:#ff00cc;border:0;color:#fff;font:12px/24px sans-serif;text-align:center}
#form{position:absolute;left:520px;top:180px;width:300px;padding:20px;background:#fff;border-radius:12px}
.in{display:block;width:260px;height:36px;margin:12px 0;border:1px solid #cfd8ea;border-radius:6px;padding:0 8px}
.login_btn{width:260px;height:40px;background:#1677ff;color:#fff;text-align:center;line-height:40px;border-radius:6px}
</style></head><body>
<div id="form"><h3>用户登录</h3><input class="in" id="u" placeholder="用户名"><input class="in" id="p" type="password" placeholder="密码">
<input class="in" id="c" placeholder="验证码"><div class="login_btn" id="btn">登  录</div></div>
<button class="t" id="t1" style="left:30px;top:120px">T1</button><button class="t" id="t2" style="left:400px;top:120px">T2</button>
<button class="t" id="t3" style="left:30px;top:420px">T3</button><button class="t" id="t4" style="left:400px;top:420px">T4</button>
<button class="t" id="t5" style="left:900px;top:120px">T5</button><button class="t" id="t6" style="left:900px;top:420px">T6</button>
<button class="t" id="t7" style="left:220px;top:270px">T7</button>
<script>window.__clicks=[];function rec(e){const t=e.target;window.__clicks.push({type:e.type,id:t.id||t.tagName,cx:e.clientX,cy:e.clientY,dpr:devicePixelRatio})}
addEventListener('mousedown',rec,true);</script></body></html>
"""

IDS = ["t1", "t2", "t3", "t4", "t5", "t6", "t7"]
TOLERANCE_PHYS_PX = 3.0      # 偏差阈值（物理像素）：小目标（16px 复选框）也能点中


def find_chrome() -> str:
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            return p
    raise SystemExit("未找到 Chrome / Edge，无法运行端到端实测")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", default="", help="强制页面设备缩放因子（如 1 / 1.25 / 2），默认跟随系统")
    ap.add_argument("--port", type=int, default=9341)
    args = ap.parse_args()

    import aiohttp
    import numpy as np
    import websockets
    from PIL import Image

    from app.core import desktop_coords
    from app.core.desktop_env import DesktopCore, resolve_core_path

    exe = resolve_core_path()
    if not exe:
        raise SystemExit("找不到内核：先运行 tools/desktop-core/build.ps1")

    work = Path(tempfile.mkdtemp(prefix="cc_calib_"))
    page = work / "calib.html"
    page.write_text(PAGE_HTML, encoding="utf-8")
    profile = work / "profile"
    chrome_args = [find_chrome(), f"--user-data-dir={profile}", f"--remote-debugging-port={args.port}",
                   "--no-first-run", "--no-default-browser-check", "--new-window",
                   "--window-position=200,100", "--window-size=1300,900"]
    if args.scale:
        chrome_args.append(f"--force-device-scale-factor={args.scale}")
    chrome_args.append(page.as_uri())

    async def cdp(ws_url: str, expr: str):
        async with websockets.connect(ws_url, max_size=1 << 24) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                      "params": {"expression": expr, "returnByValue": True}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == 1:
                    return m["result"]["result"].get("value")

    core = DesktopCore(exe, pipe_name=f"cc_calib_{os.getpid()}")
    chrome = subprocess.Popen(chrome_args)
    failures: list[str] = []
    try:
        # ── 1. 内核自检 ──
        info = await core.call("info")
        aware = info.get("dpiAwareness")
        mons = info.get("monitors") or []
        print(f"[内核] 版本 {info.get('version')}  DPI 感知={aware}  "
              + "  ".join(f"{m['w']}x{m['h']}@{m['scale']}%" for m in mons))
        if aware != "PerMonitorV2":
            failures.append(f"DPI 感知级别是 {aware}，应为 PerMonitorV2（坐标会被系统虚拟化）")
        cal = await core.call("calib")
        print(f"[落点自检] SendInput 原始偏差 {cal['rawMax']} px，校正后 {cal['finalMax']} px，"
              f"{cal['corrected']}/5 个点用了 SetCursorPos 校正")
        if cal["finalMax"] > 1:
            failures.append(f"光标落点自检校正后仍偏差 {cal['finalMax']} px")

        # ── 2. 测试页 ──
        target = None
        for _ in range(80):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"http://127.0.0.1:{args.port}/json/list",
                                     timeout=aiohttp.ClientTimeout(total=1)) as r:
                        lst = await r.json(content_type=None)
                target = next((t for t in lst if t.get("type") == "page" and "CC-CALIB" in (t.get("title") or "")), None)
                if target:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        if not target:
            raise SystemExit("测试页没有打开")
        ws = target["webSocketDebuggerUrl"]
        await asyncio.sleep(1.0)
        m = await cdp(ws, """(()=>{const o={dpr:devicePixelRatio,els:{}};
          for(const id of ['t1','t2','t3','t4','t5','t6','t7','u','p','c','btn']){const r=document.getElementById(id).getBoundingClientRect();o.els[id]=[r.left,r.top,r.width,r.height]} return o})()""")
        dpr = float(m["dpr"])
        print(f"[页面] devicePixelRatio={dpr}")
        wins = (await core.call("windows", {"limit": 120}))["items"]
        win = next(w for w in wins if "CC-CALIB" in w["title"])
        h = win["h"]
        await core.call("focus", {"handle": h})
        await asyncio.sleep(0.6)

        def rect_center(tid: str):
            r = m["els"][tid]
            return r[0] + r[2] / 2, r[1] + r[3] / 2

        async def click_and_read(x: int, y: int):
            await core.call("focus", {"handle": h})
            res = await core.call("click", {"x": x, "y": y, "handle": h, "requireForeground": True})
            await asyncio.sleep(0.2)
            last = (await cdp(ws, "window.__clicks.slice(-1)"))[-1]
            return res, last

        # ── 3. 按元素点击 ──
        snap = await core.call("snapshot", {"handle": h, "interactiveOnly": False, "budget": 300,
                                            "timeBudgetMs": 1500, "warmupMs": 3000})
        print(f"[元素] 读到 {snap['nodes']} 个节点，通道 {snap.get('path')}，预热 {snap.get('warmMs')} ms")
        by_name = {it["n"].replace("\u00a0", " ").replace(" ", ""): it for it in snap["items"] if it.get("d")}
        names = {"T1": "t1", "T2": "t2", "T3": "t3", "T4": "t4", "T5": "t5", "T6": "t6", "T7": "t7",
                 "用户名": "u", "密码": "p", "验证码": "c", "登录": "btn"}
        print("[按元素点击] 目标   偏差(物理px)  命中")
        for label, tid in names.items():
            it = by_name.get(label)
            if it is None:
                failures.append(f"按元素点击：没读到元素「{label}」")
                continue
            _res, last = await click_and_read(it["x"], it["y"])
            r = m["els"][tid]
            ok = r[0] <= last["cx"] <= r[0] + r[2] and r[1] <= last["cy"] <= r[1] + r[3]
            tcx, tcy = rect_center(tid)
            err = max(abs(last["cx"] - tcx), abs(last["cy"] - tcy)) * dpr
            print(f"    {label:<6} {err:6.1f}        {'是' if ok else '否（实际点到 ' + str(last['id']) + '）'}")
            if not ok:
                failures.append(f"按元素点击「{label}」没有命中目标")

        # ── 4. 按坐标点击（理想模型）──
        fr = (await core.call("frame", {"handle": h}))["frame"]
        maxdim = desktop_coords.model_safe_max_dim(fr[2], fr[3], 0)
        shot_path = str(work / "shot.png")
        shot = await core.call("shot", {"x": fr[0], "y": fr[1], "w": fr[2], "h": fr[3], "maxDim": maxdim,
                                        "format": "png", "path": shot_path})
        meta = {k: shot[k] for k in ("srcX", "srcY", "srcW", "srcH", "width", "height", "scale")}
        img = np.array(Image.open(shot_path).convert("RGB")).astype(int)
        mask = (img[:, :, 0] > 220) & (img[:, :, 1] < 60) & (img[:, :, 2] > 160)
        comps = []
        seen = np.zeros_like(mask, dtype=bool)
        ys, xs = np.nonzero(mask)
        for y0, x0 in zip(ys, xs):
            if seen[y0, x0]:
                continue
            stack, pix = [(y0, x0)], []
            seen[y0, x0] = True
            while stack:
                y, x = stack.pop()
                pix.append((x, y))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(pix) >= 20:
                a = np.array(pix)
                comps.append((a[:, 0].mean() + 0.5, a[:, 1].mean() + 0.5))
        order = sorted(IDS, key=lambda t: (round(m["els"][t][1] / 50), m["els"][t][0]))
        comps.sort(key=lambda c: (round(c[1] / (50 * dpr * meta["scale"])), c[0]))
        if len(comps) != len(IDS):
            failures.append(f"截图里找到 {len(comps)} 个标定方块，应为 {len(IDS)} 个（窗口被遮挡或超出屏幕？）")
        print(f"[按坐标点击] 截图 {meta['width']}×{meta['height']}（源区域 {meta['srcW']}×{meta['srcH']}）目标   偏差(物理px)  命中")
        for mode in (desktop_coords.PIXEL, desktop_coords.NORM1000):
            for tid, (xi, yi) in zip(order, comps):
                ax, ay = (xi, yi) if mode == desktop_coords.PIXEL else (xi / meta["width"] * 1000, yi / meta["height"] * 1000)
                sx, sy, _ = desktop_coords.to_screen(meta, ax, ay, mode)
                _res, last = await click_and_read(sx, sy)
                r = m["els"][tid]
                ok = r[0] <= last["cx"] <= r[0] + r[2] and r[1] <= last["cy"] <= r[1] + r[3]
                tcx, tcy = rect_center(tid)
                err = max(abs(last["cx"] - tcx), abs(last["cy"] - tcy)) * dpr
                print(f"    {mode:<8} {tid}  {err:6.1f}        {'是' if ok else '否（实际点到 ' + str(last['id']) + '）'}")
                if not ok or err > TOLERANCE_PHYS_PX:
                    failures.append(f"按坐标点击（{mode}）{tid} 偏差 {err:.1f} px 或未命中")
    finally:
        try:
            await core.shutdown()
        except Exception:
            pass
        try:
            chrome.terminate()
        except Exception:
            pass
        await asyncio.sleep(1.0)
        shutil.rmtree(work, ignore_errors=True)

    print()
    if failures:
        print("未通过：")
        for f in failures:
            print("  -", f)
        return 1
    print("通过：内核按物理像素工作，按元素与按坐标点击都落在目标上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

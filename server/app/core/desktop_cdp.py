"""Chromium 系窗口的 CDP 感知通道（plan-340-1705 L1.1）。

**为什么需要它**（本机实测，同一窗口同一时刻）：

| 指标 | CDP | UIA |
|---|---|---|
| 耗时 | 18–20 ms（首次 41 ms） | 171–177 ms |
| 可读内容 | 225 个 a11y 节点 / 54 个元素带坐标 | 79 个，**只有窗口标题栏按钮** |
| 内容覆盖 | 角色 + 名称 + 值 + 精确坐标 | 无网页内容 |

UIA 在 Chromium 系窗口上是双输：不只慢 9 倍，更关键的是**拿不到内容**——Chromium 的
UIA 提供程序默认惰性构建可访问性树。本机 21 个可见窗口中 7 个属此类（Chrome/Edge/
Electron/WebView2），且恰好是最需要界面操作的那类应用。因此对这类窗口改走 CDP。

**实现取舍**：用 `websockets` 直连 CDP（项目核心依赖），而不是拉起 Playwright——
后者要多一个 node driver 子进程，且 playwright 只是可选 `[browser]` 依赖。
这与 `mcp_servers/debug_client.py` 的 CdpSession 同构，但本模块只做"一次性感知"，
刻意不复用那个面向调试会话的长连接实现，避免为感知引入调试域的初始化开销。

**前提**：目标应用须带 `--remote-debugging-port=N` 启动（WebView2 走环境变量
`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`）。未带参的进程连不上，此时本模块返回 None，
调用方自动回退到 UIA——不改变任何既有行为。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Chromium 系窗口类名前缀：命中即说明该窗口可由 CDP 感知
CHROMIUM_CLASS_PREFIXES: tuple[str, ...] = (
    "Chrome_WidgetWin_",   # Chrome / Edge / Electron / 多数 Chromium 套壳
    "CefWebViewWnd",       # CEF（部分国产客户端）
    "Chrome_RenderWidgetHostHWND",
)

# 命令行里显式声明的调试端口（最可靠：按 pid 与窗口精确对应）
_PORT_IN_CMD_RE = re.compile(r"--remote-debugging-port[=\s]+(\d{2,5})", re.IGNORECASE)

# 调用方显式指定的额外候选端口（默认空）。
#
# 这里刻意**不做**「盲扫常见端口」：实测盲扫要 511 ms，而且可能误配到别的应用恰好
# 开着的调试端口。改为按 pid 枚举该进程实际监听的端口（内核 listenports，约 3 ms），
# 既快又准，还能覆盖 WebView2 这类用环境变量注入调试参数的场景。
DEFAULT_EXTRA_PORTS: tuple[int, ...] = ()

# 单次感知的总时间预算：超过即放弃 CDP、回退 UIA（宁可慢也不卡住任务）
_PROBE_BUDGET_S = 3.0
_HTTP_TIMEOUT_S = 1.2
_WS_TIMEOUT_S = 1.5

# 一次抓取的元素上限（与内核 snapshot 的预算语义一致）
_MAX_ELEMENTS = 400

# 抓取可交互元素的脚本。用批量 evaluate 而非 Accessibility.getFullAXTree：
# 前者能直接带回 DOM 精确矩形与名称，无需再做节点树遍历（实测 7–27 ms）。
_ELEMENTS_JS = """
(() => {
  const SEL = 'a[href],button,input,textarea,select,summary,[role],[onclick],'
            + '[contenteditable="true"],[tabindex]:not([tabindex="-1"])';
  const out = [];
  const seen = new Set();
  const vw = window.innerWidth, vh = window.innerHeight;
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    const role = (el.getAttribute('role') || el.tagName.toLowerCase());
    let name = (el.getAttribute('aria-label') || el.innerText || el.value
                || el.placeholder || el.title || el.alt || '');
    name = String(name).replace(/\\s+/g, ' ').trim().slice(0, 60);
    const key = role + '|' + name + '|' + Math.round(r.left) + ',' + Math.round(r.top);
    if (seen.has(key)) continue;
    seen.add(key);
    const tag = el.tagName.toLowerCase();
    const editable = (tag === 'input' || tag === 'textarea'
                      || el.isContentEditable === true);
    out.push({
      tag: tag,
      role: role,
      name: name,
      id: el.id || '',
      value: (typeof el.value === 'string' ? el.value : '').slice(0, 40),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      editable: editable,
      readOnly: !!el.readOnly,
      inputType: (tag === 'input' ? String(el.type || '') : ''),
      left: r.left, top: r.top, width: r.width, height: r.height,
      cx: r.left + r.width / 2, cy: r.top + r.height / 2
    });
    if (out.length >= %d) break;
  }
  return {
    elements: out,
    win: {
      screenX: window.screenX, screenY: window.screenY,
      outerWidth: window.outerWidth, outerHeight: window.outerHeight,
      innerWidth: window.innerWidth, innerHeight: window.innerHeight,
      dpr: window.devicePixelRatio,
      url: location.href, title: document.title,
      scrollX: window.scrollX, scrollY: window.scrollY
    }
  };
})()
""" % _MAX_ELEMENTS


def is_chromium_window(cls: str | None) -> bool:
    """窗口类名是否属于 Chromium 系（决定是否尝试 CDP 通道）。"""
    if not cls:
        return False
    return any(cls.startswith(p) for p in CHROMIUM_CLASS_PREFIXES)


def extract_debug_port(cmdline: str | None) -> int | None:
    """从进程命令行中提取 `--remote-debugging-port=N`。

    这是端口发现的首选来源：命令行按 pid 与窗口一一对应，不会误配到别的应用。
    Chromium 也接受 `--remote-debugging-port N`（空格分隔）写法，一并覆盖。
    """
    if not cmdline:
        return None
    m = _PORT_IN_CMD_RE.search(cmdline)
    if not m:
        return None
    try:
        port = int(m.group(1))
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


async def _http_json(port: int, path: str, timeout_s: float) -> Any:
    """请求 CDP 的 HTTP 端点（/json/version、/json/list）。"""
    import aiohttp

    url = f"http://127.0.0.1:{port}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as resp:
            return await resp.json(content_type=None)


async def list_targets(port: int, timeout_s: float = _HTTP_TIMEOUT_S) -> list[dict]:
    """列出该端口的可调试页面（CDP 的 /json/list）。失败返回空列表。"""
    try:
        data = await _http_json(port, "/json/list", timeout_s)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[desktop-cdp] 端口 %s 不可用: %s", port, exc)
        return []
    if not isinstance(data, list):
        return []
    return [
        {
            "id": t.get("id"),
            "title": t.get("title") or "",
            "url": t.get("url") or "",
            "type": t.get("type") or "",
            "ws": t.get("webSocketDebuggerUrl") or "",
        }
        for t in data
        if isinstance(t, dict)
    ]


def pick_target(targets: list[dict], window_title: str) -> dict | None:
    """从 targets 中挑出与窗口最匹配的一个页面。

    匹配优先级：标题完全相等 → 标题互相包含 → 唯一的 page 类型 target。
    Electron 应用的主窗口标题通常与页面 title 一致，因此标题匹配是可靠的锚点。
    """
    pages = [t for t in targets if t.get("type") == "page" and t.get("ws")]
    if not pages:
        return None
    want = (window_title or "").strip()
    if want:
        for t in pages:
            if (t.get("title") or "").strip() == want:
                return t
        for t in pages:
            got = (t.get("title") or "").strip()
            if got and (got in want or want in got):
                return t
    return pages[0] if len(pages) == 1 else pages[0]


def _title_matches(a: str, b: str) -> bool:
    """窗口标题与页面标题是否指向同一窗口（用于兜底路径的防误配校验）。"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a


async def find_debug_port(
    pid: int,
    window_title: str = "",
    cmdline: str = "",
    ports_provider: Callable[[int], Awaitable[list[int]]] | None = None,
    extra_ports: tuple[int, ...] = DEFAULT_EXTRA_PORTS,
) -> tuple[int, dict] | None:
    """定位该窗口对应的 CDP 端口与 target。

    顺序：
      ① 命令行里的显式端口（最准，实测 0.3 ms）；
      ② 该进程实际监听的端口（内核 listenports 按 pid 枚举，实测 3 ms）——
         覆盖 WebView2 通过环境变量注入调试参数的场景；
      ③ 调用方显式指定的端口 extra_ports。
    都不可用返回 None，由调用方回退 UIA。
    """
    explicit = extract_debug_port(cmdline)
    if explicit:
        targets = await list_targets(explicit)
        target = pick_target(targets, window_title)
        if target:
            return explicit, target
        logger.debug("[desktop-cdp] pid=%s 声明的端口 %s 上没有匹配页面", pid, explicit)

    candidates: list[int] = [p for p in extra_ports]
    if ports_provider is not None:
        try:
            listening = await ports_provider(pid)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[desktop-cdp] pid=%s 枚举监听端口失败：%s", pid, exc)
            listening = []
        for p in listening or []:
            if p not in candidates:
                candidates.append(p)

    if candidates:
        # 并发探测：连接被拒时是快速失败，总耗时取决于最慢一个
        probe = candidates[:12]
        results = await asyncio.gather(
            *(list_targets(p, timeout_s=0.6) for p in probe), return_exceptions=True
        )
        for port, res in zip(probe, results):
            if isinstance(res, Exception) or not res:
                continue
            target = pick_target(res, window_title)
            # 候选端口虽然来自“该进程在监听”，仍要求标题对得上，
            # 避免把别的应用恰好在听的调试端口误认为它的
            if target and _title_matches(window_title, target.get("title") or ""):
                return port, target
    return None


async def _evaluate(ws_url: str, expression: str, timeout_s: float) -> Any:
    """一次性 CDP 求值：连接 → Runtime.evaluate → 断开。

    刻意不缓存连接：感知是低频调用（一次任务几步），而缓存连接要在应用重启、
    页面导航、端口变化时维护失效检测——复杂度远高于收益。实测短连接总耗时
    仍在 50 ms 量级，已满足验收标准。
    """
    import websockets

    ws = await asyncio.wait_for(
        websockets.connect(ws_url, max_size=16 * 1024 * 1024), timeout=timeout_s
    )
    try:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
            msg = json.loads(raw)
            if msg.get("id") != 1:
                continue  # 事件消息（无 id 或 id 不匹配）直接跳过
            if msg.get("error"):
                raise RuntimeError(str(msg["error"])[:200])
            result = msg.get("result") or {}
            details = result.get("exceptionDetails")
            if details:
                raise RuntimeError(str(details.get("text") or "CDP 求值异常")[:200])
            return (result.get("result") or {}).get("value")
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def to_screen_coords(el: dict, win: dict, rect: list[int]) -> tuple[int, int]:
    """把元素的视口内 CSS 坐标换算成屏幕物理像素。

    推导：`rect` 是内核给的窗口**物理**矩形；`win.outerWidth/Height` 是 CDP 给的窗口
    **逻辑（DIP）**尺寸。两者之比即当前缩放比。元素先加"窗口边框 + 浏览器 UI（顶部
    高度）"偏移得到相对窗口左上角的 DIP 位置，再乘缩放比、加到窗口物理原点上。

    注意这是估算：Windows 的 GetWindowRect 含 DWM 不可见边框（约 7–8 px），可能带来
    个位数像素偏差。因此调用方必须用 desktop_hit 校验换算结果（见工具层），
    校验不通过时不执行点击，避免误点。
    """
    ow = float(win.get("outerWidth") or 0) or 1.0
    oh = float(win.get("outerHeight") or 0) or 1.0
    iw = float(win.get("innerWidth") or 0)
    ih = float(win.get("innerHeight") or 0)
    win_w, win_h = float(rect[2] or 0) or 1.0, float(rect[3] or 0) or 1.0

    scale_x = win_w / ow
    scale_y = win_h / oh

    border_x = max(0.0, (ow - iw) / 2.0)   # 左右边框各占一半
    chrome_h = max(0.0, oh - ih)           # 顶部浏览器 UI（标签栏/地址栏）

    dip_x = border_x + float(el.get("cx") or 0)
    dip_y = chrome_h + float(el.get("cy") or 0)

    return (
        int(round(float(rect[0]) + dip_x * scale_x)),
        int(round(float(rect[1]) + dip_y * scale_y)),
    )


def normalize_elements(raw: dict, rect: list[int]) -> list[dict]:
    """把 CDP 元素转成与内核 snapshot 一致的字段结构（n/id/ct/en/ix/x/y/r）。

    统一结构让工具层的渲染与点击坐标逻辑无需区分感知通道。
    """
    win = raw.get("win") or {}
    items: list[dict] = []
    for el in raw.get("elements") or []:
        x, y = to_screen_coords(el, win, rect)
        left, top = to_screen_coords(
            {"cx": el.get("left", 0), "cy": el.get("top", 0)}, win, rect
        )
        right, bottom = to_screen_coords(
            {"cx": float(el.get("left", 0)) + float(el.get("width", 0)),
             "cy": float(el.get("top", 0)) + float(el.get("height", 0))},
            win, rect,
        )
        items.append({
            "n": el.get("name") or "",
            "id": el.get("id") or "",
            "ct": _role_label(el),
            "cls": el.get("tag") or "",
            "en": not el.get("disabled"),
            "ix": True,
            "x": x,
            "y": y,
            "r": [left, top, max(1, right - left), max(1, bottom - top)],
            "val": el.get("value") or "",
            "editable": bool(el.get("editable")) and not el.get("readOnly"),
        })
    return items


def _role_label(el: dict) -> str:
    """把 CDP 的 role/tag 转成人类可读的控件类型（与 UIA 的 ControlType 对齐）。"""
    role = (el.get("role") or "").lower()
    tag = (el.get("tag") or "").lower()

    # input 的语义完全取决于 type：复选框/单选框/提交按钮都不该显示成编辑框
    if tag == "input":
        kind = (el.get("inputType") or "text").lower()
        if kind == "checkbox":
            return "CheckBox"
        if kind == "radio":
            return "RadioButton"
        if kind in ("submit", "button", "reset", "image"):
            return "Button"
        return "Edit"

    mapping = {
        "button": "Button",
        "link": "Hyperlink",
        "textbox": "Edit",
        "searchbox": "Edit",
        "combobox": "ComboBox",
        "listbox": "List",
        "checkbox": "CheckBox",
        "radio": "RadioButton",
        "tab": "TabItem",
        "menuitem": "MenuItem",
        "option": "ListItem",
        "switch": "CheckBox",
        "a": "Hyperlink",
        "textarea": "Edit",
        "select": "ComboBox",
        "summary": "Button",
    }
    if role in mapping:
        return mapping[role]
    return mapping.get(tag, tag.title() if tag else "Unknown")


async def probe_window(
    pid: int,
    window_title: str,
    rect: list[int],
    cmdline: str = "",
    ports_provider: Callable[[int], Awaitable[list[int]]] | None = None,
    extra_ports: tuple[int, ...] = DEFAULT_EXTRA_PORTS,
) -> dict | None:
    """对 Chromium 系窗口做一次 CDP 感知。

    成功返回 {"port", "target", "items", "url", "page_title"}；任何环节不可用都返回 None，
    由调用方回退 UIA（保证"最差情况不劣于现状"）。
    """
    try:
        return await asyncio.wait_for(
            _probe(pid, window_title, rect, cmdline, ports_provider, extra_ports),
            timeout=_PROBE_BUDGET_S,
        )
    except asyncio.TimeoutError:
        logger.info("[desktop-cdp] pid=%s CDP 感知超时（%.1fs），回退 UIA", pid, _PROBE_BUDGET_S)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.info("[desktop-cdp] pid=%s CDP 感知失败：%s，回退 UIA", pid, exc)
        return None


async def _probe(
    pid: int,
    window_title: str,
    rect: list[int],
    cmdline: str,
    ports_provider: Callable[[int], Awaitable[list[int]]] | None,
    extra_ports: tuple[int, ...],
) -> dict | None:
    found = await find_debug_port(pid, window_title, cmdline, ports_provider, extra_ports)
    if not found:
        logger.info(
            "[desktop-cdp] 未找到 pid=%s 的调试端口（应用可能未以 --remote-debugging-port 启动）", pid
        )
        return None
    port, target = found

    raw = await _evaluate(target["ws"], _ELEMENTS_JS, _WS_TIMEOUT_S)
    if not isinstance(raw, dict):
        return None

    items = normalize_elements(raw, rect)
    return {
        "port": port,
        "target": target.get("title") or "",
        "url": (raw.get("win") or {}).get("url") or "",
        "page_title": (raw.get("win") or {}).get("title") or "",
        "items": items,
        "nodes": len(items),
        "channel": "cdp",
    }

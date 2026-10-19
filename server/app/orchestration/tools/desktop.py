"""电脑操控工具集（plan-334-1661；plan-20261019-desktop-precision 重构）。

为什么是**后端原生工具**而不是 MCP：
`orchestration/tools/mcp_wrapper.py` 每次调用都新起子进程并完整握手（initialize →
initialized → tools/call），实测这条路径下每次调用要付 30–430 ms 的进程启动成本。而
本能力的内核是常驻进程，实测常驻往返仅约 0.06 ms。走 MCP 等于白白丢掉这个数量级的优势。
原生工具直接跑在已经常驻的 FastAPI 进程里，内核作为它的子进程常驻，固定开销趋近于零。
因此本能力**不写入 mcp_servers 表**，也就不会出现在「拓展 → 连接器」页面，
只由「设置 → 电脑操控」页控制。

定位约定（plan-20261019-desktop-precision 起，plan-20261019-desktop-targeting 改为「元素优先、坐标兜底」）：
  - **优先按元素定位**：截图结果自带「页面元素」编号清单（e1…eN，来自无障碍树，含 Chrome / Edge / Electron 的网页内容），
    desktop_click 用编号或元素上的文字（element="e3" / element="登录"）——位置是内核取到的物理像素矩形中心，
    与屏幕缩放比例、分辨率、截图缩放全都无关，不经过任何换算。
  - 坐标兜底（自绘界面等没有元素的地方）：唯一坐标系 = 最近一次截图，放大查看只读不改坐标系；
    口径按模型族选择（像素 / 0–1000），截图结果用「坐标口径」一行写明，换算见 core/desktop_coords.py。
  - 点完有反馈：动作后的新截图上用红色圆环标出落点，结果文字说明「点击处元素是什么」，点偏当场可见。
  - 内核启动时校验 DPI 感知（Per-Monitor-V2）并上报显示器缩放；光标落点读回校正，偏差会在结果里说明。
动作后画面：内核在动作前后采样，等画面稳定再截图，并报告「动作是否引起了可见变化」——
此前动作后立即截图，常截到弹窗 / 下拉还没出现或正在淡出的中间帧，模型据此反复重点（实录见计划文档）。
OCR（系统识图、文字定位、OCR 编号）与点前校验已移除：识别结果几乎不可用，且让截图链路慢数百毫秒。

审批：只读工具 risk_level="low"（免审），写操作 risk_level="high"。
是否真需要询问用户由 approval_policy.decide() 按输入框的权限模式裁决，工具不自行决定。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core import desktop_cdp, desktop_coords, desktop_elements, desktop_overlay, desktop_shots
from app.core.config import settings
from app.core.desktop_env import DesktopCoreError, get_desktop_core
from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


def _check_enabled() -> str | None:
    """总开关门禁：未启用时给出明确指引，而不是静默失败。"""
    if not getattr(settings, "desktop_enabled", False):
        return (
            "电脑操控当前未启用。请在「设置 → 电脑操控」中开启后重试。"
        )
    return None


def _check_plain_ops() -> str | None:
    """普通电脑操作子开关。"""
    err = _check_enabled()
    if err:
        return err
    if not getattr(settings, "desktop_plain_ops_enabled", True):
        return "普通电脑操作已在「设置 → 电脑操控」中关闭。"
    return None


async def _call(op: str, params: dict | None = None) -> dict:
    """调用内核并统一把内核错误转成可读文本。"""
    try:
        core = get_desktop_core()
        return await core.call(op, params)
    except DesktopCoreError as exc:
        raise _CoreFailure(str(exc)) from exc


class _CoreFailure(RuntimeError):
    """内核调用失败（消息面向用户）。"""


def _fail(exc: Exception | str) -> ToolResult:
    return ToolResult(ok=False, output="", error=str(exc))


def _text(lines: list[str]) -> str:
    return "\n".join(lines)


def _fmt_num(v: Any) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def _default_shot_path(ctx: ToolContext, fmt: str) -> str:
    """截图默认落点：工作区 `.chatcoder/desktop-shots/`（绝对路径）。

    为什么落工作区而不是内核的默认临时目录：截图接下来要做的事几乎总是「让模型看到」。
    落工作区后图片随工具结果直接内联给多模态模型，一次调用即可决策；此前落系统临时目录时，
    模型得先复制文件再调 view_image 才看得到，每次截图链路多付两轮工具往返（plan-334-1661）。
    工作区不可写时回退临时目录，保证工具本身仍可用。

    必须是绝对路径（plan-365-1774 修复）：内核是独立进程，相对路径按「内核进程的 cwd」解析，
    与后端工作区不一定一致。
    """
    ext = "png" if fmt.lower() == "png" else "jpg"
    # 文件名带毫秒（plan-365-1773）：秒级精度会让同一秒内的两次截图写进同一个文件，
    # 后一张静默覆盖前一张——模型看到的就是错位画面。
    now = datetime.now()
    name = f"shot-{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond // 1000:03d}.{ext}"
    root = (ctx.workspace_root or "").strip()
    if root:
        try:
            d = Path(root) / ".chatcoder" / "desktop-shots"
            d.mkdir(parents=True, exist_ok=True)
            return str((d / name).resolve())
        except OSError:
            pass
    return str(Path(tempfile.gettempdir()) / name)


def _coord_mode(ctx: ToolContext) -> str:
    """本次调用模型的坐标口径（隐藏配置 desktop_coord_mode 可强制指定）。"""
    return desktop_coords.coord_mode_for(
        getattr(ctx, "model_name", "") or "", getattr(settings, "desktop_coord_mode", "auto"))


def _obs(event: str, **fields: Any) -> None:
    """电脑操控观测埋点（plan-340-1705 起沿用）：只记录、不改行为，是排障与验收的数据来源。"""
    if not fields:
        logger.info("[desktop-obs] %s", event)
        return
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("[desktop-obs] %s %s", event, parts)


# ── 感知通道分流（plan-340-1705 L1.1）───────────────────────────
# CDP 探测失败的 pid 缓存（pid → 失败时刻）。应用未带 --remote-debugging-port 启动时，
# 每次快照都跑一遍端口扫描会白等数百毫秒；缓存后短时间内直接走 UIA。
_CDP_FAILED: dict[int, float] = {}
_CDP_FAIL_TTL_S = 60.0


# 最近一次窗口列表（plan-20261018-operator-boost）：动作前解析目标窗口时顺带记下，动作后与新列表比对，
# 就能发现「点了之后应用弹出了新窗口」——不必为此在动作前多付一次枚举。
_LAST_WINDOWS: dict[str, Any] = {"at": 0.0, "items": []}


async def _window_list() -> list[dict]:
    try:
        data = await _call("windows", {"limit": 120})
    except Exception:  # noqa: BLE001
        return []
    items = list(data.get("items") or [])
    _LAST_WINDOWS.update(at=time.monotonic(), items=items)
    return items


def _same_app(a: dict | None, b: dict | None) -> bool:
    """两个窗口是否属于同一应用（同进程，或进程名相同——播放器常是同名子进程）。"""
    if not a or not b:
        return False
    if a.get("pid") and a.get("pid") == b.get("pid"):
        return True
    pa, pb = str(a.get("proc") or "").casefold(), str(b.get("proc") or "").casefold()
    return bool(pa) and pa == pb


def _describe_window(w: dict) -> str:
    return f"「{w.get('title') or '(无标题)'}」（handle={w.get('h')}，进程 {w.get('proc') or '?'}）"


async def _resolve_window(handle: int, title: str) -> dict | None:
    """按 handle 或 title 取窗口信息（cls / pid / rect），供通道分流与窗口范围判断。"""
    items = await _window_list()
    if not items:
        return None
    if handle:
        for it in items:
            if int(it.get("h") or 0) == handle:
                return it
    if title:
        low = title.lower()
        for it in items:
            if low in (it.get("title") or "").lower():
                return it
    return None


async def _foreground_window() -> dict | None:
    """取当前前台窗口。"""
    for it in await _window_list():
        if it.get("fg"):
            return it
    return None


async def _try_cdp(win: dict) -> dict | None:
    """尝试用 CDP 感知该窗口；不可用返回 None（调用方回退 UIA）。失败会记入缓存，避免反复白扫端口。"""
    pid = int(win.get("pid") or 0)
    if not pid:
        return None
    now = time.monotonic()
    failed_at = _CDP_FAILED.get(pid)
    if failed_at is not None and now - failed_at < _CDP_FAIL_TTL_S:
        return None

    cmdline = ""
    try:
        cmd = await _call("proccmd", {"pid": pid})
        cmdline = str(cmd.get("cmdline") or "")
    except Exception:  # noqa: BLE001
        pass  # 拿不到命令行不影响主流程：探测会改用该进程的监听端口

    async def _ports(target_pid: int) -> list[int]:
        """该进程实际在监听的 TCP 端口（覆盖命令行看不到调试参数的场景）。"""
        data = await _call("listenports", {"pid": target_pid})
        return [int(p) for p in (data.get("ports") or [])]

    res = await desktop_cdp.probe_window(
        pid=pid,
        window_title=str(win.get("title") or ""),
        rect=list(win.get("rect") or [0, 0, 0, 0]),
        cmdline=cmdline,
        ports_provider=_ports,
    )
    if not res or not res.get("items"):
        _CDP_FAILED[pid] = now
        return None
    return res


async def _window_change_note(target: dict | None) -> tuple[str, dict | None]:
    """动作后窗口变化（plan-20261018-operator-boost）：返回 (给模型的说明, 应跟随的新前台窗口)。

    真实故障：腾讯视频首页点开剧集后弹出独立的播放窗口并成为前台，工具不说，模型继续对首页窗口截图 / 按键，
    每次都报「不在前台」，只能反复 focus 首页——把播放窗口又压到后面，越操作越乱。
    """
    before_items = list(_LAST_WINDOWS.get("items") or [])
    fresh = time.monotonic() - float(_LAST_WINDOWS.get("at") or 0) < 30
    after = await _window_list()
    if not after:
        return "", None
    fg = next((w for w in after if w.get("fg")), None)
    before_h = {int(w.get("h") or 0) for w in before_items} if fresh else set()
    new = [w for w in after if before_h and int(w.get("h") or 0) not in before_h and not w.get("min")
           and (target is None or _same_app(w, target))]
    lines: list[str] = []
    follow: dict | None = None
    if new:
        for w in new[:3]:
            lines.append("操作后出现了新窗口" + _describe_window(w) + ("，它现在在前台" if w.get("fg") else ""))
        follow = next((w for w in new if w.get("fg")), None)
    elif fg and target and int(fg.get("h") or 0) != int(target.get("h") or 0):
        if _same_app(fg, target):
            lines.append("操作后前台切换到了同一应用的另一个窗口" + _describe_window(fg))
            follow = fg
        else:
            lines.append("操作后前台变成了其他程序的窗口" + _describe_window(fg))
    if not lines:
        return "", None
    if follow is not None:
        old = f"原窗口 handle={target.get('h')} 仍在；" if target else ""
        lines.append(f"后续操作新窗口请把 handle 换成 {follow.get('h')}（截图、点击、按键都一样），{old}"
                     "不要再 desktop_focus 原窗口——那会把新窗口压到后面。")
    return "\n".join(lines), follow


async def _not_foreground_hint(exc: Exception, handle: int, title: str) -> str:
    """内核报「目标窗口不在前台」时补充真实前台是谁：多数情况是同一应用弹出了新窗口。"""
    msg = str(exc)
    if "不在前台" not in msg:
        return msg
    items = await _window_list()
    fg = next((w for w in items if w.get("fg")), None)
    target = next((w for w in items if handle and int(w.get("h") or 0) == handle), None)
    if target is None and title:
        target = next((w for w in items if title.lower() in str(w.get("title") or "").lower()), None)
    if fg is None:
        return msg
    if target is not None and _same_app(fg, target):
        return (msg + f"\n当前前台是同一应用的另一个窗口{_describe_window(fg)}——界面很可能已切到这个窗口"
                "（例如点开后弹出的播放 / 详情窗口）。要操作它就改用 handle="
                f"{fg.get('h')}；确实要回原窗口才用 focus_first=true。")
    return msg + f"\n当前前台是{_describe_window(fg)}。"


async def _window_frame(win: dict | None) -> list[int] | None:
    """窗口的**可见**边界（DWM 扩展帧）。

    为什么不直接用 windows op 给的 rect：那是 GetWindowRect，含一圈不可见的调整边框
    （Win10/11 左右各约 7–8 px）。截图与范围判断都以可见边界为基准。
    """
    handle = int((win or {}).get("h") or 0)
    if not handle:
        return None
    try:
        data = await _call("frame", {"handle": handle})
    except Exception:  # noqa: BLE001
        return None
    rect = data.get("frame") or []
    if len(rect) < 4 or int(rect[2] or 0) <= 0 or int(rect[3] or 0) <= 0:
        return None
    return [int(v) for v in rect[:4]]


async def _virtual_screen() -> tuple[int, int, int, int] | None:
    """虚拟桌面矩形（多显示器时是并集），用于把窗口裁到可见部分。"""
    try:
        data = await _call("info")
    except Exception:  # noqa: BLE001
        return None
    vs = data.get("virtualScreen") or {}
    try:
        w, h = int(vs.get("w") or 0), int(vs.get("h") or 0)
        if w <= 0 or h <= 0:
            return None
        return (int(vs.get("x") or 0), int(vs.get("y") or 0), w, h)
    except (TypeError, ValueError):
        return None


_ENV_CACHE: dict[str, Any] = {"at": 0.0, "info": None, "logged": False}


async def _kernel_info() -> dict:
    """内核环境信息（DPI 感知级别、显示器缩放、虚拟桌面），缓存 10 分钟；首次取到时写一条观测日志。

    plan-20261019-desktop-targeting：「不同缩放比例下点击偏移」的底层原因就是进程没处在 Per-Monitor-V2——
    坐标被系统虚拟化而没人发现。内核启动即校验并在这里上报，日志里能直接看到「缩放 150%、PerMonitorV2」。
    """
    now = time.monotonic()
    if _ENV_CACHE["info"] is not None and now - float(_ENV_CACHE["at"]) < 600:
        return _ENV_CACHE["info"]
    try:
        info = await _call("info")
    except Exception:  # noqa: BLE001
        return {}
    _ENV_CACHE.update(at=now, info=info)
    if not _ENV_CACHE["logged"]:
        _ENV_CACHE["logged"] = True
        mons = info.get("monitors") or []
        _obs("kernel", dpi=info.get("dpiAwareness"), version=info.get("version"),
             monitors=";".join(f"{m.get('w')}x{m.get('h')}@{m.get('scale')}%" for m in mons) or "-")
    return info


def _env_warning(info: dict) -> str:
    aw = str(info.get("dpiAwareness") or "")
    if aw and aw != "PerMonitorV2":
        return (f"注意：电脑操控内核的 DPI 感知级别是「{aw}」而不是 PerMonitorV2，系统会按缩放比例虚拟化坐标，"
                "按截图坐标点击可能整体偏移——请优先用元素编号 / 元素文字定位，并在「设置 → 电脑操控」点「检测」查看详情。")
    return ""


_SCREEN_CACHE: dict[str, Any] = {"at": 0.0, "size": None}


async def _screen_size() -> tuple[int, int]:
    """整屏截图的源尺寸（虚拟桌面），缓存 30 秒——分辨率很少变化，不值得每次截图多一次内核往返。"""
    now = time.monotonic()
    if _SCREEN_CACHE["size"] and now - float(_SCREEN_CACHE["at"]) < 30:
        return _SCREEN_CACHE["size"]
    vs = await _virtual_screen()
    size = (vs[2], vs[3]) if vs else (0, 0)
    _SCREEN_CACHE.update(at=now, size=size)
    return size


async def _desktop_hint(ctx: ToolContext, win: dict | None, *names: str) -> str:
    """按窗口（进程名 / 标题）与额外名称匹配已沉淀路线，返回附在结果末尾的文本（plan-20261018-operator-boost）。"""
    from app.orchestration.route_hints import route_hint

    pool = [str((win or {}).get("proc") or ""), str((win or {}).get("title") or ""), *names]
    pool = [p for p in pool if p.strip()]
    if not pool:
        return ""
    return await route_hint(ctx, scope="desktop", names=pool, target_label=pool[0])


def _first_step_of(recipe: object) -> str:
    """取路线记录的首个可执行步骤（L4.1）。

    路线存的是「通用原则」而不是坐标回放，但很多操作确实有天然的第一步。
    把它直接摆出来，模型就不必先观察界面再决定动作——省掉的正是最贵的那一轮推理。
    """
    steps = getattr(recipe, "steps", None)
    if not isinstance(steps, list):
        return ""
    for s in steps:
        if isinstance(s, dict):
            action = str(s.get("action") or s.get("do") or s.get("step") or "").strip()
            if action:
                return action
        elif isinstance(s, str) and s.strip():
            return s.strip()
    return ""


# ════════════════════════════════════════════════════════════════
# 元素（UIA / CDP）：编号、文字描述、按名称重新定位
# ════════════════════════════════════════════════════════════════

# 元素读写的小工具统一放在 core/desktop_elements（纯函数、可单测），这里只留别名，沿用既有调用点
_element_name = desktop_elements.element_name
_element_rect = desktop_elements.element_rect
_element_center = desktop_elements.element_center

# 读取元素时 Chromium 窗口的预热上限（毫秒）：网页无障碍树首次访问后才开始构建，实测约 0.2–0.5 s 就绪，
# 之后同一窗口一直保持；冷启动留足余量，超时就按「读到多少算多少」处理
_UIA_WARMUP_MS = 1800


def _dedupe_items(items: list[dict]) -> list[dict]:
    """按「名称 + 矩形」去重：Chromium 的嵌套结构常把同一控件报多遍。"""
    out: list[dict] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    for it in items:
        rect = _element_rect(it) or (0, 0, 0, 0)
        key = (_element_name(it), rect[0], rect[1], rect[2], rect[3])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _uia_sparse(items: list[dict], frame: list[int] | None) -> bool:
    """控件树是否「稀疏」：元素很少，且只分布在窗口里很窄的一条（典型是只有顶栏）。"""
    if len(items) >= 30:
        return False
    if not frame or frame[3] <= 0:
        return len(items) < 15
    ys = [int(it.get("y") or 0) for it in items]
    span = (max(ys) - min(ys)) if ys else 0
    return span < frame[3] * 0.3


async def _collect_elements(
    win: dict | None, *, budget: int = 300, interactive_only: bool = False,
    time_budget_ms: int = 700, depth: int = 0,
) -> tuple[list[dict], str, dict]:
    """取一份元素集合 → (元素, 通道, 附加信息)。附加信息含 chromium / doc / warmMs / truncated 等内核回报。

    通道顺序（plan-20261019-desktop-targeting）：
    1. **UIA**（所有窗口）：Chromium 系窗口由内核先预热网页无障碍树、再一次性取屏幕内可见节点——
       不需要调试端口，网页里的输入框 / 按钮 / 链接与原生控件一样带精确的物理像素矩形；
    2. **CDP**（仅 Chromium 系、且 UIA 没读到网页内容时）：应用恰好带调试端口时的补充。
    此前顺序相反且 UIA 一次性快照常只拿到标题栏按钮（树尚未构建），于是整页网页被判为「读不到控件」。
    """
    handle = int((win or {}).get("h") or 0)
    if not handle:
        return [], "none", {}
    chromium = desktop_cdp.is_chromium_window(str((win or {}).get("cls") or ""))
    items: list[dict] = []
    info: dict = {}
    try:
        data = await _call("snapshot", {
            "handle": handle, "interactiveOnly": bool(interactive_only), "budget": budget,
            "depth": depth or 12, "timeBudgetMs": time_budget_ms,
            "warmupMs": _UIA_WARMUP_MS if chromium else 0,
        })
        items = list(data.get("items") or [])
        info = {k: data.get(k) for k in
                ("path", "chromium", "warmed", "warmMs", "doc", "truncated", "total", "visited", "fallback")
                if data.get(k) is not None}
    except Exception:  # noqa: BLE001
        data = {}
    if chromium and not any(it.get("d") for it in items):
        # UIA 没读到网页内容（老内核 / 无障碍被关 / 页面仍在加载）：试 CDP 兜底
        cdp = await _try_cdp(win)
        cdp_items = list((cdp or {}).get("items") or [])
        if cdp_items:
            for it in cdp_items:
                it["d"] = 1
            return _dedupe_items(cdp_items), "cdp", {"chromium": True}
    return _dedupe_items(items), "uia" if items or data else "none", info


def _norm_label(text: str) -> str:
    return desktop_elements.norm_text(text)


async def _relocate_element(item: dict, table: desktop_shots.ElementTable) -> dict | None:
    """界面变化后按「名称 + 类型」在当前界面重新找该元素：唯一命中才返回，否则 None（不猜）。"""
    name = _norm_label(_element_name(item) or str(item.get("_label") or ""))
    if not name:
        return None
    win = await _resolve_window(table.handle, table.title)
    if win is None:
        return None
    items, _channel, _info = await _collect_elements(win)
    ct = str(item.get("ct") or "")
    hits = [it for it in items
            if _norm_label(_element_name(it)) == name and (not ct or str(it.get("ct") or "") == ct)]
    return hits[0] if len(hits) == 1 else None


# ════════════════════════════════════════════════════════════════
# 坐标解析：元素编号 / 截图坐标 → 屏幕物理像素
# ════════════════════════════════════════════════════════════════


@dataclass
class _Point:
    """解析后的目标点。error 非空表示解析失败，此时绝不能执行动作。"""

    x: int = 0
    y: int = 0
    label: str = ""
    source: str = ""
    note: str = ""
    error: str = ""
    ref: str = ""                    # 按元素点击时的编号（如 e3）；按坐标点击为空
    item: dict | None = None         # 对应的元素：点击后据此核对「点到的是不是它」
    handle: int = 0                  # 该元素所属窗口的句柄：调用方没点名窗口时用它做前台 / 范围校验


async def _element_target_window(ctx: ToolContext, args: dict[str, Any]) -> dict | None:
    """元素定位该读哪个窗口：显式 handle / title → 最近一次元素清单所属窗口 → 最近一次截图的窗口 → 前台窗口。"""
    handle = int(args.get("handle") or 0)
    title = str(args.get("title") or "")
    if handle or title:
        return await _resolve_window(handle, title)
    table = desktop_shots.get_table(ctx)
    if table is not None and (table.handle or table.title):
        win = await _resolve_window(table.handle, table.title)
        if win is not None:
            return win
    frame = desktop_shots.get_frame(ctx)
    if frame is not None and frame.handle:
        win = await _resolve_window(frame.handle, frame.window)
        if win is not None:
            return win
    return await _foreground_window()


async def _element_listing(
    ctx: ToolContext, win: dict, *, region: list[int] | tuple[int, ...] | None = None,
    limit: int = desktop_elements.LIST_LIMIT, interactive_only: bool = False,
    budget: int = 300, time_budget_ms: int = 700,
) -> tuple[dict[str, dict], str, dict]:
    """读窗口元素 → 筛选排序 → 登记成编号表（覆盖上一份），返回 (编号 → 元素, 通道, 内核附加信息)。"""
    items, channel, info = await _collect_elements(
        win, budget=budget, interactive_only=interactive_only, time_budget_ms=time_budget_ms)
    ordered = desktop_elements.prepare(items, region, limit=limit)
    if interactive_only:
        ordered = [it for it in ordered if it.get("ix")]
    if not ordered:
        return {}, channel, info
    prev = desktop_shots.get_table(ctx)
    mapping = desktop_shots.store_elements(
        ctx, ordered, handle=int(win.get("h") or 0), title=str(win.get("title") or ""), channel=channel)
    cur = desktop_shots.get_table(ctx)
    # 与上一份清单内容相同（同窗口、同样的元素序列）：编号沿用，调用方可以不再重复打印整份清单
    info["same_as_prev"] = bool(
        prev is not None and cur is not None and cur.sig and prev.sig == cur.sig and prev.handle == cur.handle)
    return mapping, channel, info


async def _resolve_ref(ctx: ToolContext, ref: str) -> _Point:
    """元素编号 → 屏幕点。界面有过写动作（编号过期）时按名称在当前界面重新定位。"""
    hit = desktop_shots.lookup_element(ctx, ref)
    shown = ref if ref.lower().startswith("e") else "e" + ref
    if hit is None:
        return _Point(error=(f"元素编号 {ref} 不在最近一次元素清单里（或已过期）。"
                             "请重新 desktop_screenshot / desktop_snapshot 获取清单，直接写元素上的文字"
                             "（如 element=\"登录\"），或按截图坐标操作。"))
    item, table = hit
    name = desktop_elements.label_of(item)
    source = f"元素 {shown}「{name}」"
    if table.stale:
        relocated = await _relocate_element(item, table)
        if relocated is None:
            return _Point(error=(f"界面已变化，元素 {shown}「{name}」在当前界面找不到唯一匹配。"
                                 "请重新 desktop_screenshot 获取最新清单，或按截图坐标操作。"))
        relocated.setdefault("_label", item.get("_label", ""))
        item = relocated
        source += "（界面变化后已按名称重新定位）"
    center = _element_center(item)
    if center is None:
        return _Point(error=f"元素 {shown} 没有可用的位置信息，请改用截图坐标。")
    return _Point(x=center[0], y=center[1], label=name, source=source, ref=shown, item=item,
                  handle=int(table.handle or 0))


async def _resolve_text(ctx: ToolContext, args: dict[str, Any], text: str, *,
                        allow_refresh: bool = True) -> _Point:
    """按元素上的文字定位：先在最近一次（未过期的）清单里找，没有就按当前界面现读一份再找。

    点「登录」「用户名」这类目标不需要先记编号、更不需要估坐标；位置是内核从无障碍树取的精确矩形。
    allow_refresh=False（批量动作规划时）：只在已有清单里找，不中途刷新——刷新会让整批的编号换一遍。
    """
    table = desktop_shots.get_table(ctx)
    cands: list[tuple[str, dict]] = []
    full: dict[str, dict] = {}
    src_handle = 0
    if table is not None and not table.stale and table.age() < 180:
        full = table.items
        src_handle = int(table.handle or 0)
        cands = desktop_elements.find_by_text(full, text)
    refreshed = False
    if not cands and allow_refresh:
        win = await _element_target_window(ctx, args)
        if win is not None and not win.get("min"):
            full, _channel, _info = await _element_listing(
                ctx, win, region=await _window_frame(win), limit=desktop_elements.LIST_LIMIT)
            refreshed = True
            src_handle = int(win.get("h") or 0)
            cands = desktop_elements.find_by_text(full, text)
    if not cands:
        return _Point(error=(f"当前界面没有找到文字为「{text}」的元素。请对照最近一次清单换个说法，"
                             "或 desktop_screenshot 刷新清单；自绘界面里读不到元素时按截图坐标操作。"))
    pick: tuple[str, dict] | None = None
    if len(cands) == 1:
        pick = cands[0]
    else:
        inter = [c for c in cands if desktop_elements.is_interactive(c[1])]
        if len(inter) == 1:
            pick = inter[0]
    if pick is None:
        hints = desktop_elements.duplicate_context(full)
        names = "、".join(
            f"{r} {desktop_elements.type_label(it)}「{desktop_elements.label_of(it)}」"
            + (f"〔{hints[r]}〕" if r in hints else "")
            for r, it in cands[:6])
        return _Point(error=(f"「{text}」匹配到 {len(cands)} 个元素：{names}。"
                             "请用编号指定其中一个（element=\"e…\"）" + ("。" if refreshed else "（编号取自最近的清单）。")))
    ref, item = pick
    center = _element_center(item)
    if center is None:
        return _Point(error=f"元素 {ref} 没有可用的位置信息，请改用截图坐标。")
    name = desktop_elements.label_of(item)
    return _Point(x=center[0], y=center[1], label=name, ref=ref, item=item, handle=src_handle,
                  source=f"元素 {ref}「{name}」（按文字「{text}」匹配）")


async def _resolve_point(ctx: ToolContext, args: dict[str, Any], *,
                         x_key: str = "x", y_key: str = "y", element_key: str = "element",
                         allow_refresh: bool = True) -> _Point:
    """把 element（编号或元素文字）或 x/y（最近一次截图上的位置）解析为屏幕点。"""
    ref = str(args.get(element_key) or "").strip()
    if ref:
        if desktop_elements.looks_like_ref(ref):
            return await _resolve_ref(ctx, ref)
        return await _resolve_text(ctx, args, ref, allow_refresh=allow_refresh)

    if args.get(x_key) is None or args.get(y_key) is None:
        return _Point(error=("请给出目标：element（清单里的编号或元素上的文字）或 "
                             f"{x_key}/{y_key}（最近一次截图上的位置）。"))
    frame = desktop_shots.get_frame(ctx)
    if frame is None:
        return _Point(error=("按坐标操作需要先 desktop_screenshot：坐标取自最近一次截图"
                             "（本轮还没有截图，或已过期）。"))
    try:
        sx, sy, note = desktop_coords.to_screen(frame.meta, args[x_key], args[y_key], frame.mode)
    except desktop_coords.CoordError as exc:
        return _Point(error=str(exc))
    source = (f"截图坐标 ({_fmt_num(args[x_key])},{_fmt_num(args[y_key])}) → 屏幕 ({sx},{sy})"
              f"，截图 {frame.shot_id}")
    if frame.mode == desktop_coords.NORM1000:
        # 比例口径回显它落在图上的哪个像素：模型能当场对出「我给的比例对应的位置对不对」
        ipx, ipy = desktop_coords.to_image_px(frame.meta, sx, sy)
        source += f"（即图上像素 ({ipx},{ipy})）"
    return _Point(x=sx, y=sy, source=source, note=note)


async def _outside_window_error(win: dict | None, pt: _Point, args: dict[str, Any], label: str) -> str:
    """给了目标窗口时，点位必须落在该窗口可见范围内（plan-20261018-operator-boost）。

    坐标给错时点击会落到窗口外、点中别的程序或桌面——宁可拒绝并说明，也不静默点错。
    最小化窗口的边界在屏幕外（-32000），focus_first 会先还原它，此时不校验。
    """
    if not win or win.get("min") or bool(args.get("allow_outside", False)):
        return ""
    frame = await _window_frame(win)
    if not frame:
        return ""
    fx, fy, fw, fh = frame
    if fx <= pt.x < fx + fw and fy <= pt.y < fy + fh:
        return ""
    _obs("outside_window", at=f"{pt.x},{pt.y}", frame=f"{fx},{fy},{fw},{fh}", source=pt.source)
    return (f"{label}点 ({pt.x},{pt.y}) 不在目标窗口「{win.get('title') or ''}」的范围内，已拒绝执行"
            f"（{pt.source}）。请对照最近一次截图与「坐标口径」重新给坐标，或改用元素编号；"
            "确需操作窗口外（如任务栏）时去掉 title/handle 或设 allow_outside=true。")


# ════════════════════════════════════════════════════════════════
# 截图（坐标系）与动作后画面
# ════════════════════════════════════════════════════════════════


def _shot_meta(data: dict[str, Any]) -> dict[str, Any]:
    """把内核 shot 的返回整理成坐标换算元数据。"""
    return {
        "path": str(data.get("path") or ""),
        "srcX": int(data.get("srcX") or 0),
        "srcY": int(data.get("srcY") or 0),
        "srcW": int(data.get("srcW") or 0),
        "srcH": int(data.get("srcH") or 0),
        "width": int(data.get("width") or 0),
        "height": int(data.get("height") or 0),
        "scale": float(data.get("scale") or 1.0) or 1.0,
    }


async def _capture_region(win: dict | None, *, prefer_window: bool) -> tuple[list[int] | None, str]:
    """决定截哪儿：给了目标窗口时截该窗口的可见部分，否则整屏。

    plan-20261018-operator-boost 起，指定窗口就只截窗口：4K 整屏缩到模型可见尺寸后小字发糊、
    小窗口在图里只剩一小块，裁到窗口能把有效分辨率提高数倍。图上坐标一律由工具换算，原点不再是负担。
    窗口部分越出屏幕时裁到屏幕内的可见部分（此前会退回整屏）。
    """
    if not (prefer_window and win and not win.get("min")):
        return None, "full"
    frame = await _window_frame(win)
    if not frame:
        return None, "full"
    vs = await _virtual_screen()
    if vs is None:
        return frame, "window"
    left, top = max(frame[0], vs[0]), max(frame[1], vs[1])
    right = min(frame[0] + frame[2], vs[0] + vs[2])
    bottom = min(frame[1] + frame[3], vs[1] + vs[3])
    if right - left < 16 or bottom - top < 16:
        return None, "full"
    return [left, top, right - left, bottom - top], "window"


def _dark_ratio(path: str) -> float:
    """截图中接近纯黑的像素占比（64x40 灰度缩略图，约 3 ms）；取不到返回 0。

    plan-20261018-operator-boost：视频 / 游戏等硬件加速或受版权保护的画面截屏时是黑的，
    模型据此判断「黑屏 / 还在加载」并反复重试。识别出来就明确告诉它。
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            small = img.convert("L").resize((64, 40), Image.BILINEAR)
            # Pillow 12 起 getdata 已弃用（14 移除），优先用新接口
            getter = getattr(small, "get_flattened_data", None) or small.getdata
            px = list(getter())
        # 只算接近纯黑（灰度 < 8）：受保护画面截出来是纯黑；深色主题网页（如 #0d1117 灰度 17）
        # 此前被 <20 的阈值误判成「黑屏」，在实录里让模型怀疑页面没加载
        return sum(1 for v in px if v < 8) / max(1, len(px))
    except Exception:  # noqa: BLE001 — 只用于提示，取不到就不提示
        return 0.0


def _shot_notes(path: str, win: dict | None, explicit_target: bool) -> list[str]:
    """截图附加说明：画面大面积纯黑、目标窗口被其它窗口盖住。"""
    notes: list[str] = []
    ratio = _dark_ratio(path)
    if ratio >= 0.55:
        notes.append(
            f"注意：截图中约 {round(ratio * 100)}% 的区域接近纯黑。视频、游戏或受版权保护的画面通常无法被截屏"
            "（截出来是黑的），这不代表黑屏或仍在加载——请结合窗口标题、可见文字与控件（剧集名、播放进度、"
            "暂停按钮等）判断状态，不要因此反复截图或重试。"
        )
    if explicit_target and win and not win.get("fg"):
        fg = next((w for w in (_LAST_WINDOWS.get("items") or []) if w.get("fg")), None)
        if fg and int(fg.get("h") or 0) != int(win.get("h") or 0):
            line = ("注意：目标窗口当前不在前台，截图截的是它所在的屏幕区域，画面里可能叠着前台窗口"
                    + _describe_window(fg) + "。")
            if _same_app(fg, win):
                line += f"它属于同一应用，界面很可能已切到这个窗口，请改用 handle={fg.get('h')} 截图和操作。"
            notes.append(line)
    return notes


def _setting_max_dim() -> int:
    """设置里的截图缩放长边（0 = 自动适配模型可见尺寸）。"""
    try:
        return max(0, int(getattr(settings, "desktop_screenshot_max_dim", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _quality() -> int:
    try:
        return max(10, min(100, int(getattr(settings, "desktop_screenshot_quality", 100) or 100)))
    except (TypeError, ValueError):
        return 100


def _is_self_window(win: dict | None, explicit: bool) -> bool:
    """前台窗口是本应用自己（chatcoder 的 Electron 界面）且调用方没点名它：不读它的元素。

    读 Chromium 系窗口的无障碍树会让它开启无障碍模式，对承载当前会话的界面既没有意义也有开销。
    """
    return (not explicit) and str((win or {}).get("proc") or "").lower() == "chatcoder"


def _listing_lines(mapping: dict[str, dict], info: dict) -> list[str]:
    """截图结果里的「页面元素」段：编号清单 + 用法一句话 + 不完整时的说明。"""
    lines = [
        f"页面元素（{len(mapping)} 项）——定位优先用编号或元素上的文字，免坐标、不受缩放影响："
        "desktop_click element=\"e3\" 或 element=\"登录\"；清单里没有的才按截图坐标。",
    ]
    lines.extend(desktop_elements.format_list(mapping))
    total = int(info.get("total") or 0)
    if info.get("truncated") or total > len(mapping):
        lines.append("（元素较多，只列出一部分；没看到目标时先滚动页面，或 desktop_snapshot 重新读取。）")
    return lines


async def _take_shot(
    ctx: ToolContext, *, handle: int = 0, title: str = "", win: dict | None = None,
    elements: bool | None = None, list_limit: int = desktop_elements.LIST_LIMIT,
    clicks: list[tuple[int, int, str]] | None = None, dedupe_list: bool = False,
) -> tuple[dict[str, Any], str, desktop_shots.Frame]:
    """截一张图并登记为新的坐标系，返回 (图片结果数据, 给模型看的文本, 坐标系)。

    desktop_screenshot、动作后截图、批量动作结尾截图共用这一条路径，保证三处的坐标系与说明一致。
    plan-20261019-desktop-targeting：截图时顺带读窗口元素（UIA / CDP）并登记编号表——模型优先说「点 e3」
    或「点登录」，位置由内核取到的物理像素矩形给出，与缩放比例、分辨率无关；只有清单里没有的目标才需要报坐标。
    clicks 非空时在图上标出这些点击位置（红色圆环），点偏当场可见。
    """
    explicit = bool(handle or title)
    if win is None:
        win = await _resolve_window(handle, title) if explicit else await _foreground_window()
    rect, region_src = await _capture_region(win, prefer_window=explicit)
    src_w, src_h = (rect[2], rect[3]) if rect else await _screen_size()
    params: dict[str, Any] = {
        "maxDim": desktop_coords.model_safe_max_dim(int(src_w), int(src_h), _setting_max_dim()),
        "format": "jpg",
        "quality": _quality(),
        "path": _default_shot_path(ctx, "jpg"),
    }
    if rect:
        params.update({"x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3]})
    data = await _call("shot", params)

    meta = _shot_meta(data)
    mode = _coord_mode(ctx)
    in_window = region_src == "window"
    win_title = str((win or {}).get("title") or title or "") if in_window else ""
    frame = desktop_shots.store_frame(
        ctx, path=meta["path"], meta=meta, mode=mode, window=win_title,
        handle=int((win or {}).get("h") or 0) if in_window else 0, region_source=region_src,
    )
    origin = f"目标窗口「{win_title}」" if in_window else "整个屏幕"
    lines = [
        f"截图 {meta['width']}×{meta['height']}（{origin}），shot_id={frame.shot_id}",
        desktop_coords.convention_line(mode, meta["width"], meta["height"]),
    ]

    # 元素清单：读不到不报错（自绘界面本来就没有），只是这一段不出现
    want_elements = elements if elements is not None else bool(getattr(settings, "desktop_auto_elements", True))
    mapping: dict[str, dict] = {}
    elem_info: dict = {}
    channel = ""
    if want_elements and win is not None and not win.get("min") and not _is_self_window(win, explicit):
        try:
            mapping, channel, elem_info = await _element_listing(
                ctx, win, region=frame.region(), limit=list_limit)
        except Exception as exc:  # noqa: BLE001 — 元素清单是增强，不能让截图失败
            logger.debug("[desktop] 截图附带元素清单失败：%s", exc)
    if mapping:
        if dedupe_list and elem_info.get("same_as_prev"):
            # 动作后的清单与上一份完全一致（表单录入、翻页前后很常见）：不再重复整份，编号沿用
            lines.append(f"页面元素与上一份清单一致（共 {len(mapping)} 项），编号沿用。")
        else:
            lines.extend(_listing_lines(mapping, elem_info))
    elif want_elements and elem_info.get("chromium"):
        lines.append("（网页元素暂时没读到：页面可能还在加载或无障碍树尚未就绪，稍等后再 desktop_screenshot 一次；"
                     "急需操作时按截图坐标。）")

    # 叠加层：参考网格 / 元素编号框（模型按需开启，开一次持续生效）、上一次点击的落点标记
    overlay = desktop_shots.get_overlay(ctx)
    q = _quality()
    try:
        if overlay["grid"]:
            desktop_overlay.draw_grid(meta["path"], meta, mode, quality=q)
        if overlay["marks"] and mapping:
            # 有网页内容时只标网页里的元素（浏览器标签栏 / 工具栏的编号挤在图的最上沿，互相压住也没有标注价值）；
            # 文字类元素也标——网页里按钮样式的 div 在无障碍树里就是一段文字
            has_doc = any(it.get("d") for it in mapping.values())
            marks = [(ref, r) for ref, it in mapping.items()
                     if (it.get("d") or not has_doc) and (r := _element_rect(it)) is not None]
            if desktop_overlay.draw_marks(meta["path"], meta, marks, quality=q):
                lines.append("图上已用红框标出元素并写上编号，与上面清单一致。")
        if clicks and desktop_overlay.draw_clicks(meta["path"], meta, clicks, quality=q):
            lines.append("图上的红色圆环是刚才点击的实际落点——核对它是否落在目标上；偏了就按图上位置修正，不要原样重试。")
        if overlay["grid"]:
            lines.append("图上淡红色网格线与边缘数字是参考刻度（与上面坐标口径一致），可对着数字读位置。")
    except Exception as exc:  # noqa: BLE001 — 叠加层是增强
        logger.debug("[desktop] 叠加层绘制失败：%s", exc)

    lines.extend(_shot_notes(meta["path"], win, explicit))
    lines.append(f"已保存：{meta['path']}")
    _obs("screenshot", mode=mode, region=region_src, size=f"{meta['width']}x{meta['height']}",
         elements=len(mapping), channel=channel or "-", warm_ms=elem_info.get("warmMs"))
    out = {**data, "shot_id": frame.shot_id, "region_source": region_src, "coord_mode": mode,
           "element_count": len(mapping),
           "list_same": bool(mapping and dedupe_list and elem_info.get("same_as_prev"))}
    return out, _text(lines), frame


def _inline(data: dict[str, Any], path: str) -> bool:
    """把图片放进 data.base64：agent loop 据此把图片送入多模态模型。"""
    try:
        with open(path, "rb") as f:
            data["base64"] = base64.b64encode(f.read()).decode("ascii")
        return True
    except OSError:
        return False


# 动作后等待画面稳定的参数（毫秒）：先等 120 再每 50 采样一次，相邻两次都静止即稳定，最长 1500；
# 已静止但还没出现变化时最多等到 900（请求→渲染型界面常在 300–800 ms 后才开始变）
_SETTLE = {"settleMinMs": 120, "settleMs": 1500, "settleIntervalMs": 50, "noChangeMs": 900}


async def _settle_region(ctx: ToolContext, win: dict | None) -> list[int] | None:
    """动作后观察哪块区域：目标窗口 > 当前坐标系的截图区域 > 整屏（None）。"""
    if win and not win.get("min"):
        frame = await _window_frame(win)
        if frame:
            return frame
    cur = desktop_shots.get_frame(ctx)
    if cur is not None:
        region = list(cur.region())
        if region[2] > 0 and region[3] > 0:
            return region
    return None


async def _settle_args(ctx: ToolContext, win: dict | None) -> dict[str, Any]:
    """动作请求附带的「动作前采样 + 动作后等稳定」参数（内核 plan-20261019-desktop-precision 起支持）。"""
    out: dict[str, Any] = {"settle": True, **_SETTLE}
    region = await _settle_region(ctx, win)
    if region:
        out.update({"sx": region[0], "sy": region[1], "sw": region[2], "sh": region[3]})
    return out


async def _standalone_settle(region: list[int] | None, *, timeout_ms: int = 1500, min_ms: int = 0) -> dict:
    """独立等待画面稳定（截图前 wait_stable、批量动作收尾）。旧内核没有 settle 时退回 waitstable / 固定等待。"""
    params: dict[str, Any] = {"timeout": timeout_ms, "minMs": min_ms, "interval": 50}
    if region:
        params.update({"x": region[0], "y": region[1], "w": region[2], "h": region[3]})
    try:
        return await _call("settle", params)
    except Exception as exc:  # noqa: BLE001
        if "unknown op" not in str(exc):
            return {}
    try:
        legacy = {k: v for k, v in params.items() if k in ("x", "y", "w", "h")}
        return await _call("waitstable", {**legacy, "timeout": timeout_ms, "settle": 60})
    except Exception:  # noqa: BLE001
        await asyncio.sleep(0.4)
        return {}


def _settle_note(settle: dict | None, *, judge_change: bool) -> str:
    """把内核的稳定检测结果转成给模型的一句话；无需提示时返回空串。"""
    if not settle:
        return ""
    elapsed = settle.get("elapsedMs")
    if judge_change and settle.get("changed") is False:
        return (f"动作后画面没有可见变化（已等待 {elapsed} ms）：目标可能不可点击、位置有偏差，或界面仍在加载。"
                "请对照新截图核对位置（或改用元素编号）再操作，不要原样重试；"
                "若只是在等加载，用 desktop_screenshot wait_stable=true 再看一次。")
    if settle.get("stable") is False:
        return f"画面在 {elapsed} ms 内仍在变化（动画、视频或加载中），新截图可能不是最终状态。"
    return ""


def _window_before(handle: int, title: str) -> dict | None:
    """动作前的目标窗口（取自最近一次窗口列表），动作后据此判断有没有弹出新窗口。"""
    items = list(_LAST_WINDOWS.get("items") or [])
    for it in items:
        if (handle and int(it.get("h") or 0) == handle) or (
                not handle and title and title.lower() in str(it.get("title") or "").lower()):
            return it
    if not (handle or title):
        return next((it for it in items if it.get("fg")), None)
    return None


async def _after_action(
    ctx: ToolContext, args: dict[str, Any], *, handle: int, title: str, output: str,
    data: dict[str, Any], settle: dict | None, judge_change: bool,
    clicks: list[tuple[int, int, str]] | None = None,
) -> str:
    """动作后：窗口变化说明 → 画面变化说明 → 默认附带新截图（成为新的坐标系）。

    点击 / 输入 / 按键 / 滚动 / 批量动作共用，保证「动作后看结果」的行为一致；
    附带观察失败只追加一行说明，绝不让动作本身报错。
    clicks：本次动作的点击落点（屏幕坐标），画在新截图上让模型看到「实际点在哪」。
    """
    target = _window_before(handle, title)
    try:
        note, follow = await _window_change_note(target)
    except Exception:  # noqa: BLE001 — 窗口比对只是附加信息
        note, follow = "", None
    if note:
        output += "\n" + note
        data["window_change"] = {"follow_handle": (follow or {}).get("h")}
    if settle is not None:
        # 新窗口弹出时，原区域「没变化」并不代表动作无效：不给无变化提示
        line = _settle_note(settle, judge_change=judge_change and not note)
        if line:
            output += "\n" + line
    if not bool(args.get("then_shot", True)):
        return output
    if settle is None:
        # 内核未做动作后采样（旧版本）：固定等一下，避免截到动作前的画面
        await asyncio.sleep(0.4)
    shot_handle, shot_title = handle, title
    if follow is not None:
        shot_handle, shot_title = int(follow.get("h") or 0), ""
    elif not (handle or title):
        cur = desktop_shots.get_frame(ctx)
        if cur is not None and cur.region_source == "window" and cur.handle:
            shot_handle = cur.handle
    # 动作确实改变了画面（或无法判断）才附带新的元素清单：没变化时清单与上一份相同，重复附带只会撑大上下文
    changed = (settle or {}).get("changed")
    want_list = bool(getattr(settings, "desktop_auto_elements", True)) and (changed is not False or bool(note))
    try:
        shot_data, shot_text, _frame = await _take_shot(
            ctx, handle=shot_handle, title=shot_title, clicks=clicks, dedupe_list=True,
            elements=want_list, list_limit=desktop_elements.AFTER_ACTION_LIMIT)
        output += "\n\n[动作后的界面]\n" + shot_text
        if _inline(data, str(shot_data.get("path") or "")):
            output += "\n（新画面已随本次结果送入模型，之后的坐标按这张图取值"
            if shot_data.get("list_same"):
                output += "；元素编号沿用。）"
            elif shot_data.get("element_count"):
                output += "；上面的新清单已替换旧编号。）"
            else:
                output += "。）"
    except Exception as exc:  # noqa: BLE001 - 附带截图失败不应让动作本身报错
        output += f"\n（动作后截图失败：{exc}）"
    return output


# ════════════════════════════════════════════════════════════════
# 感知类（只读，免审）
# ════════════════════════════════════════════════════════════════


class DesktopWindowsTool(Tool):
    name = "desktop_windows"
    risk_level = "low"
    description = (
        "列出当前可见的桌面窗口（标题、进程、是否前台）。\n"
        "操作电脑前的第一步：先看有哪些窗口，拿到 handle 或标题再给其它工具用。\n"
        "需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filter": {"type": "string", "description": "按标题或进程名过滤（可选）"},
                        "limit": {"type": "integer", "description": "最多返回多少个窗口，默认 30"},
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        try:
            data = await _call("windows", {
                "filter": str(args.get("filter") or ""),
                "limit": int(args.get("limit") or 30),
            })
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        items = data.get("items") or []
        lines = [f"共 {data.get('count', 0)} 个可见窗口，当前前台：{data.get('foregroundTitle') or '(无)'}"]
        for w in items:
            flags = []
            if w.get("fg"):
                flags.append("前台")
            if w.get("min"):
                flags.append("最小化")
            tag = f" [{'/'.join(flags)}]" if flags else ""
            lines.append(f"- {w.get('title')}  (进程 {w.get('proc')}, handle={w.get('h')}){tag}")
        # 内核环境自检：DPI 感知不对时坐标会被系统虚拟化，要让模型知道别再信坐标（元素定位不受影响）
        warn = _env_warning(await _kernel_info())
        if warn:
            lines.append(warn)
        return ToolResult(ok=True, output=_text(lines), data=data)


class DesktopSnapshotTool(Tool):
    name = "desktop_snapshot"
    risk_level = "low"
    description = (
        "读取指定窗口里屏幕内可见的元素，给每个元素一个编号（e1、e2…）；之后 desktop_click element=\"e5\" "
        "（或直接写元素上的文字，如 element=\"登录\"）点元素中心——位置来自系统无障碍树的精确矩形，"
        "不经过任何坐标估算与换算，与屏幕缩放比例、分辨率无关，是最准的定位方式。\n"
        "原生应用（设置、资源管理器、Office、企业客户端）与浏览器 / Electron 应用里的网页内容都可读"
        "（无需调试端口；浏览器窗口首次读取会先等网页无障碍树就绪，约 0.5 秒）。\n"
        "截图结果本身已自带这份清单，通常不必单独调用；需要更多元素、或想只看可交互项时才用它。"
        "界面变化后旧编号会按名称自动重新定位，定位不到再重新读取。\n"
        "需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "窗口标题（包含匹配，可选）"},
                        "handle": {"type": "integer", "description": "窗口句柄（来自 desktop_windows）"},
                        "interactive_only": {
                            "type": "boolean",
                            "description": "只返回可交互元素，默认 false（同时列出按钮样式的短文字，如网页里的「登 录」）",
                        },
                        "budget": {"type": "integer", "description": "最多返回多少个元素，默认 120"},
                        "depth": {
                            "type": "integer",
                            "description": "仅在快速读取失败回退到逐层遍历时使用的最大深度，默认 12。",
                        },
                        "time_budget_ms": {
                            "type": "integer",
                            "description": "读取时间预算（毫秒），默认 800。到点即返回已收集元素并标记截断。",
                        },
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        title = str(args.get("title") or "")
        handle = int(args.get("handle") or 0)
        if not title and not handle:
            return ToolResult(ok=False, output="", error="必须提供 title 或 handle 之一。")

        win = await _resolve_window(handle, title)
        if win is None:
            return ToolResult(ok=False, output="", error="未找到目标窗口，请先 desktop_windows 确认 handle 或标题。")
        route = await _desktop_hint(ctx, win, title)
        limit = max(1, min(300, int(args.get("budget") or 120)))
        interactive_only = bool(args.get("interactive_only", False))
        try:
            mapping, channel, info = await _element_listing(
                ctx, win, region=await _window_frame(win), limit=limit, interactive_only=interactive_only,
                budget=max(limit * 2, 300), time_budget_ms=int(args.get("time_budget_ms") or 800))
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        if not mapping:
            hint = ("该窗口读不到屏幕内的元素（元素数 0）：多为自绘界面、页面仍在加载，或窗口被最小化 / 遮挡。\n"
                    "请用 desktop_screenshot 看图，再按截图坐标操作（坐标口径见截图结果）；"
                    "浏览器页面刚打开时稍等 1 秒再试一次。")
            return ToolResult(ok=True, output=hint + route,
                              data={"count": 0, "channel": channel, "route_hint": bool(route)})

        source = "CDP 调试通道" if channel == "cdp" else "UI Automation 无障碍树"
        lines = [f"共 {len(mapping)} 个元素（{source}，屏幕内可见）。点击直接用编号：desktop_click element=\"e1\""
                 "（点元素中心，免坐标换算），也可直接写元素上的文字："]
        lines.extend(desktop_elements.format_list(mapping))
        total = int(info.get("total") or 0)
        truncated = bool(info.get("truncated")) or total > len(mapping)
        # 读取受数量 / 时间两重上限约束，命中任一上限都必须明说——否则模型会以为这就是全部元素
        if truncated:
            lines.append("注意：元素较多，只列出一部分（可交互项优先）；找不到目标时先滚动页面再读，或按截图坐标操作。")
        if info.get("fallback"):
            lines.append("注意：快速读取失败，已回退到逐层遍历，结果可能不完整。")
        # plan-20261018-operator-boost：控件树只覆盖窗口一小条（自绘 / 内嵌网页）时直说，避免误判内容区为空
        if channel == "uia" and not info.get("chromium") and _uia_sparse(
                list(mapping.values()), await _window_frame(win)):
            lines.append("注意：控件树只覆盖窗口很窄的一部分，内容区是自绘、读不到控件——这不代表内容区为空。"
                         "请用 desktop_screenshot 看图后按截图坐标操作。")
        _obs("snapshot", channel=channel, count=len(mapping), warm_ms=info.get("warmMs"),
             doc="yes" if info.get("doc") else "no")
        return ToolResult(ok=True, output=_text(lines) + route,
                          data={"count": len(mapping), "channel": channel, "truncated": truncated,
                                "route_hint": bool(route)})


class DesktopScreenshotTool(Tool):
    name = "desktop_screenshot"
    risk_level = "low"
    description = (
        "截取屏幕（给了 title/handle 时只截该窗口，更清晰），图片直接送入模型，并自带「页面元素」编号清单"
        "（网页、原生应用里的按钮 / 输入框 / 链接，来自系统无障碍树）。\n"
        "定位优先级：① 清单里有的目标用编号或元素上的文字——desktop_click element=\"e3\" 或 element=\"登录\"，"
        "免坐标，不受屏幕缩放与截图缩放影响；② 清单里没有的（自绘界面、图片、画布）才按图上位置给 x/y，"
        "口径见结果里的「坐标口径」一行，换算成屏幕坐标由工具完成。\n"
        "点击 / 输入 / 按键默认会在动作后自动等画面稳定并带回新截图（上面用红色圆环标出点击落点），"
        "不必为了看结果单独再截。\n"
        "小目标看不清时用 zoom=\"x0,y0,x1,y1\"（取自最近一次截图的区域）放大查看——只看不改坐标系；"
        "加载或动画中的界面用 wait_stable=true 等画面稳定后再截。\n"
        "点不准时可开叠加层：marks=true 在图上用红框标出元素并写上编号，grid=true 画带刻度数字的参考网格"
        "（开一次持续生效，设为 false 关闭）。\n"
        "需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle": {"type": "integer", "description": "目标窗口句柄（来自 desktop_windows）"},
                        "title": {"type": "string", "description": "目标窗口标题（包含匹配）；给了就只截该窗口"},
                        "zoom": {
                            "type": "string",
                            "description": '放大查看的区域 "x0,y0,x1,y1"（左上角与右下角，取自最近一次截图、口径同点击）；只看不改坐标系',
                        },
                        "wait_stable": {
                            "type": "boolean",
                            "description": "截图前先等画面稳定（默认 false），加载或动画中的界面建议开启",
                        },
                        "elements": {
                            "type": "boolean",
                            "description": "是否附带页面元素编号清单，默认 true；纯看图、不需要元素时可设 false 省上下文",
                        },
                        "marks": {
                            "type": "boolean",
                            "description": "在图上用红框标出可交互元素并写上编号（开启后持续生效，false 关闭）",
                        },
                        "grid": {
                            "type": "boolean",
                            "description": "在图上画带刻度数字的参考网格，可对着数字读位置（开启后持续生效，false 关闭）",
                        },
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        zoom = args.get("zoom")
        if zoom not in (None, "", []):
            return await self._zoom(zoom, ctx)

        if "marks" in args or "grid" in args:
            desktop_shots.set_overlay(
                ctx,
                marks=bool(args["marks"]) if "marks" in args else None,
                grid=bool(args["grid"]) if "grid" in args else None,
            )

        handle = int(args.get("handle") or 0)
        title = str(args.get("title") or "").strip()
        win = await _resolve_window(handle, title) if (handle or title) else await _foreground_window()
        if bool(args.get("wait_stable", False)):
            region, _src = await _capture_region(win, prefer_window=bool(handle or title))
            await _standalone_settle(region, timeout_ms=3000)

        try:
            data, text, _frame = await _take_shot(
                ctx, handle=handle, title=title, win=win,
                elements=bool(args["elements"]) if "elements" in args else None)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        if _inline(data, str(data.get("path") or "")):
            text += "\n图片已随本次结果送入模型。"
        else:
            text += "\n（图片内联失败，可用 view_image 查看上面的文件。）"
        # plan-20261018-operator-boost：截图对象（前台窗口或指定窗口）已沉淀的路线随结果附上
        route = await _desktop_hint(ctx, win, title)
        if route:
            text += route
            data["route_hint"] = True
        return ToolResult(ok=True, output=text, data=data)

    async def _zoom(self, raw: Any, ctx: ToolContext) -> ToolResult:
        """放大查看（只读）：区域取自当前坐标系，放大图不成为新的坐标参照——与 Claude computer use 的 zoom 一致。"""
        frame = desktop_shots.get_frame(ctx)
        if frame is None:
            return ToolResult(ok=False, output="", error="放大查看需要先 desktop_screenshot：区域取自最近一次截图。")
        try:
            x, y, w, h = desktop_coords.parse_region(frame.meta, raw, frame.mode)
        except desktop_coords.CoordError as exc:
            return ToolResult(ok=False, output="", error=str(exc))
        # 太小的区域外扩到至少 96 物理像素，保留一点上下文便于辨认
        if w < 96:
            x, w = x - (96 - w) // 2, 96
        if h < 96:
            y, h = y - (96 - h) // 2, 96
        params = {
            "x": x, "y": y, "w": w, "h": h,
            "maxDim": desktop_coords.model_safe_max_dim(w, h, 1024),
            "fit": True,
            "format": "jpg",
            "quality": _quality(),
            "path": _default_shot_path(ctx, "jpg"),
        }
        try:
            data = await _call("shot", params)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)
        meta = _shot_meta(data)
        lines = [
            f"放大查看：屏幕区域 ({x},{y}) {w}×{h}，放大图 {meta['width']}×{meta['height']}。",
            f"只用于看清细节：之后的坐标仍按截图 shot_id={frame.shot_id} 取值，口径不变。",
            f"已保存：{meta['path']}",
        ]
        _obs("zoom", region=f"{x},{y},{w},{h}")
        if _inline(data, meta["path"]):
            lines.append("图片已随本次结果送入模型。")
        return ToolResult(ok=True, output=_text(lines), data={**data, "zoom": True})


# ════════════════════════════════════════════════════════════════
# 执行类（写操作，需审批）
# ════════════════════════════════════════════════════════════════

_TARGET_PROPS: dict[str, dict] = {
    "title": {"type": "string", "description": "目标窗口标题（包含匹配），用于安全校验：点位必须落在该窗口内"},
    "handle": {"type": "integer", "description": "目标窗口句柄"},
    "focus_first": {"type": "boolean", "description": "执行前先激活目标窗口（需给出 title 或 handle），默认 false"},
}
_THEN_SHOT_PROP = {
    "type": "boolean",
    "description": "动作后等画面稳定并附带新截图（默认 true，新截图即新的坐标参照）；确定不需要看结果时设 false",
}
_XY_DESC = "仅在没有可用元素时使用；取自最近一次截图，按截图结果「坐标口径」一行给值（默认图上像素）"


def _click_feedback(pt: _Point, data: dict[str, Any], near_items: dict[str, dict]) -> list[str]:
    """点击后的反馈：点到的元素是什么、是否与目标一致、光标落点偏差、附近的可交互元素。

    这是「点偏当场可见」的文字部分（图上的红色圆环是图形部分）。实录里模型点了 20 多次标题文字，
    因为结果只有「已点击」，它从未被告知点到的是一段不可点击的文字。
    """
    lines: list[str] = []
    hit = data.get("hit") if isinstance(data.get("hit"), dict) else None
    kind, text = desktop_elements.hit_summary(hit)
    if pt.item is not None:
        # 按编号 / 文字点：点击点理应落在目标元素上，不一致说明有东西盖在上面
        if kind != "none" and desktop_elements.hit_conflicts(hit, pt.item):
            lines.append(f"注意：点击点下实际是{text}，不是 {pt.ref}「{pt.label}」——目标可能被弹层 / 浮窗遮住或已移动，"
                         "请看新截图确认。")
    elif kind == "interactive":
        lines.append(f"点击处元素：{text}。")
    elif kind in ("text", "blank"):
        lines.append(f"点击处元素：{text}。")
        near = desktop_elements.nearest(near_items, pt.x, pt.y) if near_items else []
        if near:
            names = "、".join(
                f"{desktop_elements.type_label(it)}「{desktop_elements.label_of(it)}」（{d}）" for _r, it, d in near)
            lines.append(f"点击点附近的可交互元素：{names}。要点它们请直接用元素文字，如 "
                         f"element=\"{desktop_elements.label_of(near[0][1])}\"，不必再估坐标。")
    err = int(data.get("landErr") or 0)
    if err > 1:
        lines.append(f"注意：光标没有精确落在目标点（偏差约 {err} 像素，已尽量校正）；小目标请核对图上的落点标记。")
    return lines


class DesktopClickTool(Tool):
    name = "desktop_click"
    risk_level = "high"
    description = (
        "点击鼠标。目标二选一，**优先 element**：\n"
        "- element：截图结果「页面元素」清单里的编号（如 e5）或元素上的可见文字（如 登录），直接点元素中心——"
        "位置来自系统无障碍树的精确矩形，免坐标、不受屏幕缩放与截图缩放影响；\n"
        "- x/y：清单里没有的目标（自绘界面、图片、画布）才用，取自最近一次截图，按结果里「坐标口径」一行给值，"
        "换算成屏幕坐标由工具完成，不要自己换算。\n"
        "可一步完成「点击 → 清空 → 输入 → 回车」：如 desktop_click element=\"用户名\" clear_first=true text=\"…\" keys=ENTER。\n"
        "结果会告诉你「点击处元素是什么」，动作后的新截图上用红色圆环标出实际落点；"
        "点到的不是目标、或结果说「画面没有可见变化」，说明没点中或点了无效——对照落点修正，不要原样重复。"
        "流程已知的多步操作用 desktop_batch 一次完成。\n"
        "安全：默认要求目标窗口在前台；不在前台时给出 title/handle 并设 focus_first=true。"
        "给了 title/handle 时，落在该窗口外的点击会被拒绝（多为坐标给错）。\n"
        "需审批，且需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element": {
                            "type": "string",
                            "description": "元素编号（如 e5，取自最近一次截图 / desktop_snapshot 的清单）或元素上的可见文字（如 登录）；"
                                           "优先使用，免坐标",
                        },
                        "x": {"type": "number", "description": "横坐标：" + _XY_DESC},
                        "y": {"type": "number", "description": "纵坐标：" + _XY_DESC},
                        "button": {"type": "string", "description": "left / right / middle，默认 left"},
                        "count": {"type": "integer", "description": "点击次数，2 表示双击，默认 1"},
                        **_TARGET_PROPS,
                        "allow_outside": {
                            "type": "boolean",
                            "description": "给了 title/handle 但确需点击窗口外（如任务栏）时设 true，默认 false",
                        },
                        "text": {"type": "string", "description": "点击后紧接着输入的文本（可选）"},
                        "keys": {"type": "string", "description": "输入后紧接着按下的键，如 ENTER（可选）"},
                        "clear_first": {"type": "boolean", "description": "输入前先全选清空，默认 false"},
                        "then_shot": _THEN_SHOT_PROP,
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        handle = int(args.get("handle") or 0)
        title = str(args.get("title") or "")
        pt = await _resolve_point(ctx, args)
        if pt.error:
            return ToolResult(ok=False, output="", error=pt.error)
        # 按元素点击但没点名窗口：元素的位置属于它所在的窗口，用它做前台 / 范围校验——
        # 否则用户中途切到别的窗口后，同一个屏幕位置会点在别的应用上
        if pt.handle and not (handle or title):
            handle = pt.handle

        win = await _resolve_window(handle, title) if (handle or title) else None
        outside = await _outside_window_error(win, pt, args, "点击")
        if outside:
            return ToolResult(ok=False, output="", error=outside,
                              data={"action": "refuse", "at": [pt.x, pt.y], "coord_source": pt.source})

        params: dict[str, Any] = {
            "x": pt.x,
            "y": pt.y,
            "title": title,
            "handle": handle,
            "button": str(args.get("button") or "left"),
            "count": int(args.get("count") or 1),
            "focusFirst": bool(args.get("focus_first", False)),
            "requireForeground": bool(getattr(settings, "desktop_require_foreground", True)),
            "text": str(args.get("text") or ""),
            "keys": str(args.get("keys") or ""),
            "clearFirst": bool(args.get("clear_first", False)),
            **(await _settle_args(ctx, win)),
        }
        # 点击点「附近有什么」要用点击时刻的界面：取点击前的元素表（写动作之后它会被标为过期）
        table_before = desktop_shots.get_table(ctx)
        near_items = dict(table_before.items) if (table_before is not None and not table_before.stale) else {}
        try:
            data = await _call("click", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=await _not_foreground_hint(exc, handle, title))

        # 动作改变了界面：元素编号需要重新定位；记下点击位置供随后的输入观察局部变化
        desktop_shots.mark_stale(ctx)
        desktop_shots.note_point(ctx, pt.x, pt.y)

        verb = {1: "点击", 2: "双击"}.get(int(args.get("count") or 1), "连击")
        if str(args.get("button") or "left") == "right":
            verb = "右键点击"
        parts = [f"已{verb}（{pt.source}）"]
        if data.get("target"):
            parts.append(f"窗口「{data['target']}」")
        if data.get("typed"):
            parts.append(f"输入了 {data['typed']} 个字符")
        if args.get("keys") and not data.get("keysError"):
            parts.append(f"按下了 {args.get('keys')}")
        if data.get("keysError"):
            parts.append(f"按键失败：{data['keysError']}")
        output = "，".join(parts) + "。"
        if pt.note:
            output += f"（{pt.note}）"
        feedback = _click_feedback(pt, data, near_items)
        if feedback:
            output += "\n" + "\n".join(feedback)

        settle = data.get("settle") if isinstance(data.get("settle"), dict) else None
        output = await _after_action(ctx, args, handle=handle, title=title, output=output, data=data,
                                     settle=settle, judge_change=True, clicks=[(pt.x, pt.y, "")])
        _obs("click", source=("element" if args.get("element") else "xy"),
             changed=(settle or {}).get("changed"), settle_ms=(settle or {}).get("elapsedMs"),
             hit=desktop_elements.hit_summary(data.get("hit"))[0], land_err=data.get("landErr"))
        data.update({"coord_source": pt.source, "at": [pt.x, pt.y]})
        return ToolResult(ok=True, output=output, data=data)


class DesktopTypeTool(Tool):
    name = "desktop_type"
    risk_level = "high"
    description = (
        "向目标窗口当前焦点输入文本（支持中文，走 Unicode 输入而非剪贴板）。\n"
        "提交类输入设 keys=\"ENTER\"，一步完成「输入 + 回车」；需要先点输入框时直接用 desktop_click 带 text。\n"
        "默认在输入后等画面稳定并带回新截图。\n"
        "安全：默认要求目标窗口处于前台；不在前台时给出 title/handle 并设 focus_first=true。\n"
        "需审批，且需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要输入的文本"},
                        **_TARGET_PROPS,
                        "clear_first": {"type": "boolean", "description": "输入前先全选清空，默认 false"},
                        "keys": {"type": "string", "description": "输入后紧接着按下的键，如 ENTER"},
                        "then_shot": _THEN_SHOT_PROP,
                    },
                    "required": ["text"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        text = str(args.get("text") or "")
        if not text:
            return ToolResult(ok=False, output="", error="text 不能为空。")
        handle = int(args.get("handle") or 0)
        title = str(args.get("title") or "")
        win = await _resolve_window(handle, title) if (handle or title) else None
        params: dict[str, Any] = {
            "text": text,
            "title": title,
            "handle": handle,
            "focusFirst": bool(args.get("focus_first", False)),
            "clearFirst": bool(args.get("clear_first", False)),
            "keys": str(args.get("keys") or ""),
            "requireForeground": bool(getattr(settings, "desktop_require_foreground", True)),
            **(await _settle_args(ctx, win)),
        }
        last = desktop_shots.last_point(ctx)
        if last is not None:
            params.update({"lx": last[0], "ly": last[1]})
        try:
            data = await _call("type", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=await _not_foreground_hint(exc, handle, title))

        desktop_shots.mark_stale(ctx)
        focused = "（已先激活目标窗口）" if data.get("focusedNow") else ""
        output = f"已向「{data.get('target') or '当前窗口'}」输入 {data.get('typed', 0)} 个字符{focused}。"
        keys = str(args.get("keys") or "")
        if keys and not data.get("keysError"):
            output += f"随后按下了 {keys}。"
        if data.get("keysError"):
            output += f"按键失败：{data['keysError']}。"
        settle = data.get("settle") if isinstance(data.get("settle"), dict) else None
        output = await _after_action(ctx, args, handle=handle, title=title, output=output, data=data,
                                     settle=settle, judge_change=False)
        return ToolResult(ok=True, output=output, data=data)


class DesktopKeysTool(Tool):
    name = "desktop_keys"
    risk_level = "high"
    description = (
        "发送按键或组合键，如 ENTER / TAB / ESC / ctrl+c / alt+F4 / ctrl+shift+s（修饰键全程按住）。\n"
        "空格分隔可连发多个按键（\"TAB TAB TAB\"、\"DOWN DOWN ENTER\"），repeat 把整串重复若干次。\n"
        "默认在按键后等画面稳定并带回新截图。\n"
        "安全：默认要求目标窗口处于前台；不在前台时给出 title/handle 并设 focus_first=true。\n"
        "需审批，且需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keys": {"type": "string", "description": "按键或组合键，如 ENTER 或 ctrl+s；空格分隔可连发，如 TAB TAB"},
                        "repeat": {"type": "integer", "description": "把按键串重复的次数，默认 1，最多 50"},
                        **_TARGET_PROPS,
                        "then_shot": _THEN_SHOT_PROP,
                    },
                    "required": ["keys"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        keys = str(args.get("keys") or "").strip()
        if not keys:
            return ToolResult(ok=False, output="", error="keys 不能为空。")
        handle = int(args.get("handle") or 0)
        title = str(args.get("title") or "")
        win = await _resolve_window(handle, title) if (handle or title) else None
        try:
            repeat = max(1, min(50, int(args.get("repeat") or 1)))
        except (TypeError, ValueError):
            repeat = 1
        params: dict[str, Any] = {
            "keys": keys,
            "repeat": repeat,
            "title": title,
            "handle": handle,
            "focusFirst": bool(args.get("focus_first", False)),
            "requireForeground": bool(getattr(settings, "desktop_require_foreground", True)),
            **(await _settle_args(ctx, win)),
        }
        last = desktop_shots.last_point(ctx)
        if last is not None:
            params.update({"lx": last[0], "ly": last[1]})
        try:
            data = await _call("keys", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=await _not_foreground_hint(exc, handle, title))

        desktop_shots.mark_stale(ctx)
        times = f"（共按下 {data.get('pressed')} 次）" if int(data.get("pressed") or 1) > 1 else ""
        output = f"已发送按键 {keys}{times} 到「{data.get('target') or '当前窗口'}」。"
        settle = data.get("settle") if isinstance(data.get("settle"), dict) else None
        output = await _after_action(ctx, args, handle=handle, title=title, output=output, data=data,
                                     settle=settle, judge_change=False)
        return ToolResult(ok=True, output=output, data=data)


class DesktopScrollTool(Tool):
    name = "desktop_scroll"
    risk_level = "high"
    description = (
        "在指定位置滚动鼠标滚轮。x/y 取自最近一次截图（口径同点击），不填则在当前画面中央滚动。\n"
        "clicks 为滚动格数：正数向上、负数向下，默认 -3（向下 3 格）。默认在滚动后等画面稳定并带回新截图。\n"
        "需审批，且需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number", "description": "横坐标（可选）：" + _XY_DESC},
                        "y": {"type": "number", "description": "纵坐标（可选）：" + _XY_DESC},
                        "clicks": {"type": "integer", "description": "滚动格数，正数向上、负数向下，默认 -3"},
                        "horizontal": {"type": "boolean", "description": "是否横向滚动，默认 false"},
                        "title": _TARGET_PROPS["title"],
                        "handle": _TARGET_PROPS["handle"],
                        "then_shot": _THEN_SHOT_PROP,
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        handle = int(args.get("handle") or 0)
        title = str(args.get("title") or "")
        win = await _resolve_window(handle, title) if (handle or title) else None
        params: dict[str, Any] = {
            "clicks": int(args.get("clicks") if args.get("clicks") is not None else -3),
            "horizontal": bool(args.get("horizontal", False)),
            **(await _settle_args(ctx, win)),
        }
        where = "当前画面中央"
        if args.get("x") is not None and args.get("y") is not None:
            pt = await _resolve_point(ctx, args)
            if pt.error:
                return ToolResult(ok=False, output="", error=pt.error)
            outside = await _outside_window_error(win, pt, args, "滚动")
            if outside:
                return ToolResult(ok=False, output="", error=outside)
            params.update({"x": pt.x, "y": pt.y})
            where = pt.source
        else:
            region = await _settle_region(ctx, win)
            if region:
                params.update({"x": region[0] + region[2] // 2, "y": region[1] + region[3] // 2})
        try:
            data = await _call("scroll", params)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        desktop_shots.mark_stale(ctx)
        output = f"已滚动 {params['clicks']} 格（{where}）。"
        settle = data.get("settle") if isinstance(data.get("settle"), dict) else None
        output = await _after_action(ctx, args, handle=handle, title=title, output=output, data=data,
                                     settle=settle, judge_change=True)
        return ToolResult(ok=True, output=output, data=data)


# 批量动作支持的动作名与常见别名（模型常沿用 computer use 的叫法）
_BATCH_ALIASES = {
    "click": "click", "left_click": "click", "tap": "click",
    "double_click": "double_click", "dblclick": "double_click",
    "right_click": "right_click", "context_click": "right_click",
    "move": "move", "hover": "move", "mouse_move": "move",
    "drag": "drag", "left_click_drag": "drag",
    "scroll": "scroll",
    "type": "type", "input": "type", "text": "type",
    "key": "key", "keys": "key", "press": "key", "hotkey": "key",
    "wait": "wait", "sleep": "wait",
}
_BATCH_ACTIONS = ["click", "double_click", "right_click", "move", "drag", "scroll", "type", "key", "wait"]
_BATCH_MAX_STEPS = 30


class DesktopBatchTool(Tool):
    name = "desktop_batch"
    risk_level = "high"
    description = (
        "一次按顺序执行多步电脑操作，结尾等画面稳定后截图（成为新的坐标参照，点击落点用红色圆环标出）。"
        "流程已知时优先用它：真正的延迟在模型往返，合并成一次调用能省掉中间每一轮。\n"
        "每步 {\"action\": 动作, …参数}：click / double_click / right_click / move（悬停）**优先用 element**"
        "（清单编号或元素文字），没有元素时才用 x,y；drag 用 x,y → to_x,to_y；"
        "scroll 用 clicks（正数向上、负数向下，x,y 可省略）；"
        "type 用 text（可带 clear_first）；key 用 keys（如 TAB、ENTER、ctrl+a，可带 repeat）；wait 用 ms（最多 5000）。\n"
        "所有元素与坐标都取自调用前最近一次截图（口径同 desktop_click）；任一步失败立即停止，结果说明执行到第几步。\n"
        "例（登录）：[{\"action\":\"click\",\"element\":\"用户名\"},{\"action\":\"type\",\"text\":\"admin\"},"
        "{\"action\":\"click\",\"element\":\"密码\"},{\"action\":\"type\",\"text\":\"…\"},{\"action\":\"key\",\"keys\":\"ENTER\"}]。\n"
        "看不到的目标（如点开菜单后才出现的选项）不要猜：先执行到能看见为止，再根据新截图继续。\n"
        "需审批，且需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "description": "按顺序执行的动作列表（最多 30 步）",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string", "enum": _BATCH_ACTIONS},
                                    "x": {"type": "number"},
                                    "y": {"type": "number"},
                                    "element": {"type": "string", "description": "元素编号（如 e5）或元素上的可见文字（如 登录）"},
                                    "to_x": {"type": "number"},
                                    "to_y": {"type": "number"},
                                    "clicks": {"type": "integer"},
                                    "horizontal": {"type": "boolean"},
                                    "text": {"type": "string"},
                                    "clear_first": {"type": "boolean"},
                                    "keys": {"type": "string"},
                                    "repeat": {"type": "integer", "description": "key 步：把按键串重复的次数"},
                                    "ms": {"type": "integer"},
                                },
                                "required": ["action"],
                            },
                        },
                        **_TARGET_PROPS,
                        "allow_outside": {
                            "type": "boolean",
                            "description": "给了 title/handle 但确需操作窗口外时设 true，默认 false",
                        },
                        "screenshot": {
                            "type": "boolean",
                            "description": "全部完成后等画面稳定并附带截图（默认 true）",
                        },
                    },
                    "required": ["actions"],
                },
            },
        }

    async def _plan(self, ctx: ToolContext, actions: list, win: dict | None,
                    args: dict[str, Any]) -> tuple[list[tuple[str, dict, str]], str]:
        """执行前把每一步解析成内核请求：任何一步解析失败就整批不执行（不留半截操作）。"""
        plan: list[tuple[str, dict, str]] = []
        # 批量里有可用的最新清单时，所有 element 都按这份清单解析、不中途刷新：
        # 刷新会让编号整体换一遍，同一批里靠前的 e5 与靠后的 e5 就不是同一个元素了
        tbl = desktop_shots.get_table(ctx)
        allow_refresh = tbl is None or tbl.stale
        for i, raw in enumerate(actions, start=1):
            if not isinstance(raw, dict):
                return [], f"第 {i} 步格式不对：每步应是 {{\"action\": …}} 对象"
            kind = _BATCH_ALIASES.get(str(raw.get("action") or "").strip().lower())
            if kind is None:
                return [], f"第 {i} 步动作「{raw.get('action')}」不支持，可用：{' / '.join(_BATCH_ACTIONS)}"
            if kind in ("click", "double_click", "right_click", "move", "drag"):
                pt = await _resolve_point(ctx, raw, allow_refresh=allow_refresh)
                if pt.error:
                    return [], f"第 {i} 步：{pt.error}"
                outside = await _outside_window_error(win, pt, args, "操作")
                if outside:
                    return [], f"第 {i} 步：{outside}"
                if kind == "move":
                    plan.append(("move", {"x": pt.x, "y": pt.y}, f"悬停（{pt.source}）"))
                elif kind == "drag":
                    end = await _resolve_point(ctx, raw, x_key="to_x", y_key="to_y", element_key="to_element",
                                               allow_refresh=allow_refresh)
                    if end.error:
                        return [], f"第 {i} 步（拖动终点）：{end.error}"
                    plan.append(("drag", {"x1": pt.x, "y1": pt.y, "x2": end.x, "y2": end.y, "duration": 300},
                                 f"拖动 ({pt.x},{pt.y})→({end.x},{end.y})"))
                else:
                    count = 2 if kind == "double_click" else 1
                    button = "right" if kind == "right_click" else "left"
                    verb = {"click": "点击", "double_click": "双击", "right_click": "右键点击"}[kind]
                    # _coord：这一步是按坐标点的（按元素点的位置本来就精确，不需要落点反馈）；
                    # 下划线开头的键只在本工具内使用，发给内核前剔除
                    plan.append(("click", {"x": pt.x, "y": pt.y, "button": button, "count": count,
                                           "_coord": pt.item is None},
                                 f"{verb}（{pt.source}）"))
            elif kind == "scroll":
                payload: dict[str, Any] = {
                    "clicks": int(raw.get("clicks") if raw.get("clicks") is not None else -3),
                    "horizontal": bool(raw.get("horizontal", False)),
                }
                if raw.get("x") is not None and raw.get("y") is not None:
                    pt = await _resolve_point(ctx, raw, allow_refresh=allow_refresh)
                    if pt.error:
                        return [], f"第 {i} 步：{pt.error}"
                    payload.update({"x": pt.x, "y": pt.y})
                plan.append(("scroll", payload, f"滚动 {payload['clicks']} 格"))
            elif kind == "type":
                text = str(raw.get("text") or "")
                if not text:
                    return [], f"第 {i} 步：type 需要 text"
                plan.append(("type", {"text": text, "clearFirst": bool(raw.get("clear_first", False))},
                             f"输入 {len(text)} 个字符"))
            elif kind == "key":
                keys = str(raw.get("keys") or raw.get("key") or raw.get("text") or "").strip()
                if not keys:
                    return [], f"第 {i} 步：key 需要 keys"
                try:
                    rep = max(1, min(50, int(raw.get("repeat") or 1)))
                except (TypeError, ValueError):
                    rep = 1
                plan.append(("keys", {"keys": keys, "repeat": rep}, f"按键 {keys}" + (f" ×{rep}" if rep > 1 else "")))
            else:
                try:
                    ms = max(0, min(5000, int(raw.get("ms") or raw.get("duration_ms") or 500)))
                except (TypeError, ValueError):
                    return [], f"第 {i} 步：wait 的 ms 必须是数字"
                plan.append(("wait", {"ms": ms}, f"等待 {ms} ms"))
        return plan, ""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        actions = args.get("actions")
        if not isinstance(actions, list) or not actions:
            return ToolResult(ok=False, output="", error="actions 需要是非空的动作列表。")
        if len(actions) > _BATCH_MAX_STEPS:
            return ToolResult(ok=False, output="", error=f"一次最多 {_BATCH_MAX_STEPS} 步，请拆成多次。")

        handle = int(args.get("handle") or 0)
        title = str(args.get("title") or "")
        if not (handle or title) and any(isinstance(a, dict) and a.get("element") for a in actions):
            # 批量里按元素点击但没点名窗口：用元素清单所属窗口做前台 / 范围校验（理由同 desktop_click）
            tbl = desktop_shots.get_table(ctx)
            if tbl is not None and tbl.handle:
                handle = int(tbl.handle)
        win = await _resolve_window(handle, title) if (handle or title) else None
        plan, plan_err = await self._plan(ctx, actions, win, args)
        if plan_err:
            return ToolResult(ok=False, output="", error=plan_err + "（未执行任何步骤）")

        require_fg = bool(getattr(settings, "desktop_require_foreground", True))
        if bool(args.get("focus_first", False)) and (handle or title):
            # 先激活一次：悬停 / 拖动 / 滚动这几类内核动作不带前台校验，放在每步里激活会漏掉它们
            try:
                await _call("focus", {"title": title, "handle": handle, "proc": ""})
            except Exception as exc:  # noqa: BLE001
                return ToolResult(ok=False, output="", error=f"激活目标窗口失败：{exc}（未执行任何步骤）")
        done: list[str] = []
        failure = ""
        last_click: tuple[int, int] | None = None
        click_marks: list[tuple[int, int, str]] = []
        hit_notes: list[str] = []
        for i, (op, payload, desc) in enumerate(plan, start=1):
            guarded = {"title": title, "handle": handle, "requireForeground": require_fg}
            wire = {k: v for k, v in payload.items() if not k.startswith("_")}
            res: dict[str, Any] = {}
            try:
                if op == "wait":
                    await asyncio.sleep(payload["ms"] / 1000.0)
                elif op in ("click", "type", "keys"):
                    res = await _call(op, {**wire, **guarded})
                else:
                    res = await _call(op, wire)
            except Exception as exc:  # noqa: BLE001
                failure = f"第 {i} 步（{desc}）失败：{await _not_foreground_hint(exc, handle, title)}"
                break
            if op == "click":
                last_click = (int(payload["x"]), int(payload["y"]))
                click_marks.append((last_click[0], last_click[1], ""))
                # 按坐标点的步骤：点到的若是不可点击的文字 / 空白，汇总到结果里（按元素点的位置本来就精确）
                if payload.get("_coord"):
                    kind, text = desktop_elements.hit_summary(res.get("hit"))
                    if kind in ("text", "blank"):
                        hit_notes.append(f"第 {i} 步点击处是{text}")
            done.append(desc)
            if i < len(plan) and op != "wait":
                await asyncio.sleep(0.08)

        if done:
            desktop_shots.mark_stale(ctx)
        if last_click is not None:
            desktop_shots.note_point(ctx, *last_click)
        if not done:
            return ToolResult(ok=False, output="", error=failure + "（未执行任何步骤）")

        output = f"已执行 {len(done)}/{len(plan)} 步：" + "；".join(done) + "。"
        if hit_notes:
            output += "\n注意：" + "；".join(hit_notes) + "——按坐标点到了不可点击的位置，请对照新截图里的落点标记修正。"
        if failure:
            output += "\n" + failure + "\n后续步骤未执行；请看新截图确认当前状态后再继续。"
        settle = await _standalone_settle(await _settle_region(ctx, win), timeout_ms=1500, min_ms=120)
        data: dict[str, Any] = {"steps": len(plan), "done": len(done), "failed": bool(failure)}
        shot_args = {"then_shot": bool(args.get("screenshot", True))}
        output = await _after_action(ctx, shot_args, handle=handle, title=title, output=output, data=data,
                                     settle=settle or None, judge_change=False, clicks=click_marks or None)
        _obs("batch", steps=len(plan), done=len(done), failed=bool(failure))
        # 部分完成也算成功返回：模型需要看到新截图才能决定下一步（失败原因已写在结果里）
        return ToolResult(ok=True, output=output, data=data)


class DesktopFocusTool(Tool):
    name = "desktop_focus"
    risk_level = "low"
    description = (
        "把指定窗口激活到前台。\n"
        "当 desktop_click/desktop_type 提示「目标窗口不在前台」时，先用本工具激活它（或在动作里设 focus_first=true）。\n"
        "需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "窗口标题（包含匹配）"},
                        "handle": {"type": "integer", "description": "窗口句柄"},
                        "proc": {"type": "string", "description": "进程名（如 notepad）"},
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        if not any([args.get("title"), args.get("handle"), args.get("proc")]):
            return ToolResult(ok=False, output="", error="必须提供 title / handle / proc 之一。")

        try:
            data = await _call("focus", {
                "title": str(args.get("title") or ""),
                "handle": int(args.get("handle") or 0),
                "proc": str(args.get("proc") or ""),
            })
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        if data.get("foreground"):
            hint = await _desktop_hint(ctx, None, str(args.get("proc") or ""), str(args.get("title") or ""),
                                       str(data.get("title") or ""))
            return ToolResult(ok=True, output="窗口已激活到前台。" + hint, data={**data, "route_hint": bool(hint)})
        return ToolResult(
            ok=False, output="",
            error="无法把该窗口激活到前台（可能被系统限制）。",
            data=data,
        )


class DesktopRecipeTool(Tool):
    name = "desktop_recipe"
    risk_level = "low"
    description = (
        "记录或查询「操作路线」——某应用某操作意图的通用原则，用于减少重复推演。\n"
        "action=recall：在操控某个应用之前先查一次。若命中已有路线，按其 principle 执行即可，\n"
        "不必重新摸索界面（这是本工具的主要价值：降低模型思考与推理开销）。\n"
        "desktop_focus / desktop_apps start / desktop_snapshot / desktop_screenshot 确定目标应用时也会自动附上已有路线。\n"
        "网页（内置浏览器）的路线用 browser_recipe。\n"
        "action=save：一次操控成功后，把「怎么做才有效、踩了什么坑」沉淀下来。\n"
        "**存的是通用原则，不是坐标回放**——界面会变，原则不会。\n"
        "同一应用同一意图只保留一条：重复保存会累加使用次数并更新原则，不会产生重复条目。\n"
        "需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "recall（查询）或 save（保存）"},
                        "app_name": {"type": "string", "description": "应用名，建议用进程名（如 QQMusic）"},
                        "intent": {"type": "string", "description": "操作意图，一句话（如「搜索并播放指定歌曲」）"},
                        "principle": {
                            "type": "string",
                            "description": "action=save 时必填：通用原则（跨界面版本仍成立的操作知识）",
                        },
                        "pitfalls": {"type": "string", "description": "action=save 时可选：踩过的坑"},
                        "steps": {
                            "type": "array",
                            "description": "action=save 时可选：本次实际操作序列，供人工核对",
                            "items": {"type": "object"},
                        },
                    },
                    "required": ["action"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        if not getattr(settings, "desktop_recipe_enabled", True):
            return ToolResult(ok=False, output="", error="操作路线功能已在「设置 → 电脑操控」中关闭。")

        if ctx.db is None:
            return ToolResult(ok=False, output="", error="当前上下文没有数据库会话，无法读写操作路线。")

        from app.services import desktop_recipe_service as svc

        action = str(args.get("action") or "").lower()
        app_name = str(args.get("app_name") or "")
        intent = str(args.get("intent") or "")

        if action == "recall":
            if not app_name and not intent:
                return ToolResult(ok=False, output="", error="至少提供 app_name 或 intent 之一。")
            hits = await svc.recall_recipe(ctx.db, app_name=app_name, intent=intent, limit=3,
                                           scope=svc.SCOPE_DESKTOP)
            if not hits:
                return ToolResult(
                    ok=True,
                    output=(
                        f"没有找到「{app_name or intent}」的操作路线。\n"
                        "请按常规方式操作；成功后可用 action=save 把经验沉淀下来，"
                        "下次同类操作就能直接复用。"
                    ),
                    data={"count": 0},
                )
            # 命中会累加 usage_count（「常用路线优先」依赖它），同样需要提交落库
            await ctx.db.commit()
            from app.orchestration.route_hints import note_recalled

            note_recalled(ctx.session_id, [h.id for h in hits])
            lines = [f"找到 {len(hits)} 条参考路线，按原则执行即可，不必重新摸索界面："]
            for h in hits:
                used = f"（已成功复用 {h.usage_count} 次）" if (h.usage_count or 0) > 0 else ""
                lines.append(f"\n【{h.app_name} / {h.intent}】{used}")
                lines.append(f"  原则：{h.principle}")
                if h.pitfalls:
                    lines.append(f"  注意：{h.pitfalls}")
                # L4.1：能给出第一步就直接给出，省掉「先观察再决定」的一轮
                first = _first_step_of(h)
                if first:
                    lines.append(f"  第一步：{first}")
            lines.append(
                "\n命中路线时直接照此动手；只有当界面与描述不符、或找不到元素时，再截图或读取界面观察。"
            )
            return ToolResult(ok=True, output="\n".join(lines), data={"count": len(hits)})

        if action == "save":
            try:
                row, is_new = await svc.save_recipe(
                    ctx.db,
                    app_name=app_name,
                    intent=intent,
                    principle=str(args.get("principle") or ""),
                    pitfalls=str(args.get("pitfalls") or ""),
                    steps=args.get("steps") if isinstance(args.get("steps"), list) else None,
                )
            except ValueError as exc:
                return ToolResult(ok=False, output="", error=str(exc))
            # 必须 commit 而非 flush：ctx.db 是工具专用的独立会话，工具执行完即关闭，
            # 只 flush 未提交的事务会被回滚——表现就是「提示已记录，设置页却看不到」。
            await ctx.db.commit()
            verb = "已记录" if is_new else "已更新并累加使用次数"
            return ToolResult(
                ok=True,
                output=f"{verb}操作路线「{row.app_name} / {row.intent}」（第 {row.usage_count} 次）。",
                data={"id": row.id, "created": is_new, "usage_count": row.usage_count},
            )

        return ToolResult(ok=False, output="", error="action 必须是 recall 或 save。")


class DesktopAppsTool(Tool):
    name = "desktop_apps"
    risk_level = "medium"
    description = (
        "查找已安装程序，或启动一个程序。\n"
        "action=find 按关键词搜索（返回可执行文件路径）；\n"
        "action=start 启动指定程序并等待其窗口出现。\n"
        "需审批，且需在「设置 → 电脑操控」中启用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "find 或 start"},
                        "keyword": {"type": "string", "description": "程序名关键词（action=find/start 时）"},
                        "exe": {"type": "string", "description": "可执行文件完整路径（action=start 时优先使用）"},
                    },
                    "required": ["action"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        action = str(args.get("action") or "").lower()
        if action == "find":
            return await self._find(str(args.get("keyword") or ""))
        if action == "start":
            return await self._start(str(args.get("exe") or ""), str(args.get("keyword") or ""), ctx)
        return ToolResult(ok=False, output="", error="action 必须是 find 或 start。")

    async def _find(self, keyword: str) -> ToolResult:
        if not keyword:
            return ToolResult(ok=False, output="", error="keyword 不能为空。")
        try:
            data = await _call("findapp", {"keyword": keyword})
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        hits = data.get("items") or []
        if not hits:
            return ToolResult(ok=True, output=f"没有找到与「{keyword}」匹配的已安装程序。", data=data)
        lines = [f"找到 {len(hits)} 个匹配："]
        for h in hits[:10]:
            lines.append(f"- {h.get('name')}  {h.get('exe')}")
        lines.append("可用 action=start 并传入 exe 启动。")
        return ToolResult(ok=True, output=_text(lines), data=data)

    async def _start(self, exe: str, keyword: str, ctx: ToolContext) -> ToolResult:
        if not exe and not keyword:
            return ToolResult(ok=False, output="", error="需要提供 exe 或 keyword。")
        try:
            data = await _call("startapp", {"exe": exe, "keyword": keyword})
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        # plan-20261018-operator-boost：启动应用即附上它已沉淀的操作路线
        exe_name = Path(str(data.get("exe") or exe or "")).stem
        route = await _desktop_hint(ctx, None, exe_name, keyword, str(data.get("title") or ""))
        if route:
            data = {**data, "route_hint": True}
        if data.get("title"):
            return ToolResult(
                ok=True,
                output=f"已启动「{data.get('title')}」（handle={data.get('handle')}）。" + route,
                data=data,
            )
        return ToolResult(
            ok=True,
            output=f"已启动 {data.get('exe')}，但未在超时内检测到窗口。" + route,
            data=data,
        )


ALL_DESKTOP_TOOLS = (
    DesktopWindowsTool, DesktopSnapshotTool, DesktopScreenshotTool,
    DesktopClickTool, DesktopTypeTool, DesktopKeysTool, DesktopScrollTool, DesktopBatchTool,
    DesktopFocusTool, DesktopAppsTool, DesktopRecipeTool,
)

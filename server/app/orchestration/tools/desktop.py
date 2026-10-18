"""电脑操控工具集（plan-334-1661）。

为什么是**后端原生工具**而不是 MCP：
`orchestration/tools/mcp_wrapper.py` 每次调用都新起子进程并完整握手（initialize →
initialized → tools/call），实测这条路径下每次调用要付 30–430 ms 的进程启动成本。而
本能力的内核是常驻进程，实测常驻往返仅约 0.06 ms。走 MCP 等于白白丢掉这个数量级的优势。
原生工具直接跑在已经常驻的 FastAPI 进程里，内核作为它的子进程常驻，固定开销趋近于零。
因此本能力**不写入 mcp_servers 表**，也就不会出现在「拓展 → 连接器」页面，
只由「设置 → 电脑操控」页控制。

感知分级（按实测耗时，从便宜到贵）：
  1. desktop_hit         ElementFromPoint，约 2.5 ms ——「这个位置是什么」
  2. desktop_snapshot    UIA CacheRequest 批量，约 53 ms —— 一次拿到全部可交互元素
  3. desktop_screenshot  截图兜底，约 110 ms —— 仅用于自绘 UI（UIA 树为空时）
真正的落地经验是：**先用 hit 定位，再决定动作**，比「先抓全树再在客户端搜索」快两个数量级。

审批：只读工具 risk_level="low"（免审），写操作 risk_level="high"。
是否真需要询问用户由 approval_policy.decide() 按输入框的权限模式裁决，工具不自行决定。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core import desktop_cdp, desktop_marks, desktop_shots
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


def _fail(exc: Exception) -> ToolResult:
    return ToolResult(ok=False, output="", error=str(exc))


def _text(lines: list[str]) -> str:
    return "\n".join(lines)


def _default_shot_path(ctx: ToolContext, fmt: str) -> str:
    """截图默认落点：工作区 `.chatcoder/desktop-shots/`（绝对路径）。

    为什么落工作区而不是内核的默认临时目录：截图接下来要做的事几乎总是「让模型看到」。
    落工作区后图片随工具结果直接内联给多模态模型，一次调用即可决策；此前落系统临时目录时，
    模型得先复制文件再调 view_image 才看得到，每次截图链路多付两轮工具往返（plan-334-1661）。
    工作区不可写时回退临时目录，保证工具本身仍可用。

    必须是绝对路径（plan-365-1774 修复）：内核是独立进程，相对路径按「内核进程的 cwd」
    解析，与后端工作区不一定一致——曾造成 ocrboxes/findtext 静默读不到图、Chromium
    回退到 730 ms 的 UIA 枚举，是「操作变慢」的直接来源。
    """
    ext = "png" if fmt.lower() == "png" else "jpg"
    # 文件名带毫秒（plan-365-1773）：秒级精度会让同一秒内的两次截图写进同一个文件，
    # 后一张静默覆盖前一张——已登记的历史截图内容被换掉，模型看到/点到的就是错位画面。
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


def _abs_image(path: str) -> str:
    """传给内核的图片路径统一转成绝对路径（正斜杠）。

    内核按自己的 cwd 解析相对路径，相对路径会让 ocrboxes/findtext 静默读不到图
    （plan-365-1774 的实际故障）；这里兜底，即使调用方给了相对路径也不会踩坑。
    """
    try:
        return str(Path(path).resolve()).replace("\\", "/")
    except OSError:
        return str(path).replace("\\", "/")


# ── 感知通道分流（plan-340-1705 L1.1）───────────────────────────
# CDP 探测失败的 pid 缓存（pid → 失败时刻）。应用未带 --remote-debugging-port 启动时，
# 每次快照都跑一遍端口扫描会白等数百毫秒；缓存后短时间内直接走 UIA。
_CDP_FAILED: dict[int, float] = {}
_CDP_FAIL_TTL_S = 60.0


# 最近一次窗口列表（plan-20261018-operator-boost）：动作前解析目标窗口时顺带记下，动作后与新列表比对，
# 就能发现「点了之后应用弹出了新窗口」——不必为此在动作前多付一次 ~200 ms 的枚举。
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
    return (f"「{w.get('title') or '(无标题)'}」（handle={w.get('h')}，进程 {w.get('proc') or '?'}，"
            f"中心点 {w.get('x')},{w.get('y')}）")


async def _resolve_window(handle: int, title: str) -> dict | None:
    """按 handle 或 title 取窗口信息（cls / pid / rect），供通道分流判断。"""
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


async def _try_cdp(win: dict) -> dict | None:
    """尝试用 CDP 感知该窗口；不可用返回 None（调用方回退 UIA）。

    失败会记入缓存，避免在未带调试参数的应用上反复白扫端口。
    """
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


def _action_hint(it: dict) -> str:
    """直述该元素能做什么（L1.2）：模型不必再从控件类型自行推断可操作性。"""
    if not it.get("en", True):
        return "已禁用"
    if it.get("editable"):
        return "可输入"
    ct = it.get("ct") or ""
    if ct in ("Button", "Hyperlink", "TabItem", "MenuItem", "ListItem",
              "CheckBox", "RadioButton", "ComboBox"):
        return "可点击"
    return ""


def _render_items(items: list[dict], limit: int = 120) -> list[str]:
    """两个感知通道共用同一渲染，保证模型看到的格式一致。"""
    lines: list[str] = []
    for it in items[:limit]:
        label = it.get("n") or "(无名称)"
        extra = []
        if it.get("id"):
            extra.append(f"id={it['id']}")
        if it.get("ct"):
            extra.append(it["ct"])
        if not it.get("en", True):
            extra.append("已禁用")
        hint = _action_hint(it)
        if hint and hint != "已禁用":
            extra.append(hint)
        suffix = f"  [{', '.join(extra)}]" if extra else ""
        lines.append(f"- {label}  ({it.get('x')},{it.get('y')}){suffix}")
    return lines


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


def _next_step_hint(items: list[dict]) -> str:
    """给一条可直接复制的调用示例（L1.2）：省掉模型自己拼参数格式的功夫。"""
    for it in items:
        if _action_hint(it) == "可点击":
            return (f"示例：desktop_click x={it.get('x')} y={it.get('y')}"
                    f"　—— 点击「{it.get('n') or '该元素'}」")
    for it in items:
        if it.get("editable"):
            return (f"示例：desktop_click x={it.get('x')} y={it.get('y')} text=\"...\""
                    f"　—— 在「{it.get('n') or '该输入框'}」中输入")
    return ""


async def _snapshot_after_action(ctx: ToolContext, handle: int = 0, title: str = "") -> str:
    """动作后附带一次快照（L1.3）：把「操作 + 看结果」合并为一次调用。

    每省一轮工具往返，就省掉一次完整的模型推理——那才是电脑操控里最贵的一环。
    快照失败或目标不明时返回空串，绝不因此让动作本身报错。
    """
    snap_args: dict[str, Any] = {}
    if handle:
        snap_args["handle"] = handle
    elif title:
        snap_args["title"] = title
    else:
        # 未给出目标时看前台窗口：动作可能刚打开了新窗口，前台就是结果所在
        try:
            data = await _call("windows", {"limit": 120})
            fg = str(data.get("foregroundTitle") or "").strip()
            if fg:
                snap_args["title"] = fg
        except Exception:  # noqa: BLE001
            return ""
    if not snap_args:
        return ""
    try:
        res = await DesktopSnapshotTool().run(snap_args, ctx)
    except Exception:  # noqa: BLE001
        return ""
    return res.output if res.ok else ""


def _cdp_result(cdp: dict, budget: int) -> ToolResult:
    """把 CDP 感知结果渲染成与 UIA 通道一致的元素清单。"""
    items = (cdp.get("items") or [])[:budget]
    nodes = len(items)
    lines = [f"共 {nodes} 个元素（来自 CDP 调试通道，已取到网页完整内容）："]
    lines.extend(_render_items(items))
    if nodes > 120:
        lines.append(f"... 其余 {nodes - 120} 个元素已省略（可调小 budget 或加 filter）")
    lines.append("坐标为屏幕物理像素，可直接用于 desktop_click。")
    hint = _next_step_hint(items)
    if hint:
        lines.append(hint)
    return ToolResult(ok=True, output=_text(lines), data=cdp)


# ════════════════════════════════════════════════════════════════
# 「点准」支撑（plan-365-1757）：元素集合、区域决策、观测埋点
#
# 这一段是 A1–A5 的地基：把「模型看图 → 心算坐标 → 点像素」改成
# 「模型给语义锚点（编号 / 文字 / 图片归一化坐标）→ 代码换算并校验 → 执行」。
# ════════════════════════════════════════════════════════════════


def _shot_meta(data: dict[str, Any]) -> dict[str, Any]:
    """把内核 shot / zoom 的返回整理成坐标换算元数据（与 desktop_marks 同一口径）。"""
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


def _marked_path(src_path: str) -> str:
    """编号图落点：与源图同目录、加 `-mark` 后缀（保留原图，模型随时可回看未标注版本）。"""
    p = Path(src_path)
    suffix = p.suffix or ".jpg"
    return str(p.with_name(f"{p.stem}-mark{suffix}"))


# ── 感知复用（plan-365-1774）────────────────────────────────────
# 变慢的最大来源是「重复感知」：点击前重新枚举元素（Chromium 实测 0.73 s）、
# 找文字时自截图再 OCR（实测 0.53 s）。这些改为复用最近一次截图的感知结果——
# 复用条件收紧到「同一窗口 + 时效内」，且写动作会让复用窗口骤减：
#   - 未失效截图（截图后没写过）：时效与编号一致（300 s）；
#   - 已失效截图（写过一次，画面可能已变）：只给 60 s 短窗，超窗必须重新感知；
#   - 找文字比点前校验更依赖「当前位置」：失效截图的窗口收紧到 20 s。
REUSE_ITEMS_TTL_S = 300.0
REUSE_ITEMS_STALE_TTL_S = 60.0
REUSE_TEXT_STALE_TTL_S = 20.0


def _file_digest(path: str) -> str:
    """画面文件内容摘要（plan-365-1774「同画面沿用编号」用）；读不到返回空串。"""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _screen_signature(path: str) -> bytes:
    """截图的粗糙指纹：64x40 灰度缩略图字节（plan-365-1774 提速主路径）。

    为什么不用文件哈希：桌面截图总在微变（任务栏时钟、鼠标光标、JPEG 压缩伪影），
    逐字节比较几乎永不命中；缩略图比较只对「界面真的变了」敏感，忽略这些噪声。
    成本实测约 3-5 ms（远低于省下的 OCR 300+ ms）。任何异常都返回空（退回精确哈希判定）。
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            small = img.convert("L").resize((64, 40), Image.BILINEAR)
            return small.tobytes()
    except Exception:  # noqa: BLE001 - 指纹只用于提速，取不到就退回精确判定
        return b""


def _reusable_shot(
    ctx: ToolContext, win: dict | None, *,
    allow_stale: bool = True, stale_ttl: float = REUSE_ITEMS_STALE_TTL_S,
) -> Any | None:
    """取可用于复用感知结果的最近截图；不满足条件返回 None。

    - 没有截图、或截图没登记元素 → 没有可复用的东西；
    - 给了目标窗口且与截图窗口不一致 → 不复用（窗口换了，坐标系与内容都不同）；
    - 失效截图由调用方决定是否接受（allow_stale）与给多长的窗口（stale_ttl）。
    """
    rec = desktop_shots.get_shot(ctx)
    if rec is None or not rec.items:
        return None
    if rec.stale:
        if not allow_stale or rec.age() > stale_ttl:
            return None
    elif rec.age() > REUSE_ITEMS_TTL_S:
        return None
    win_title = str((win or {}).get("title") or "")
    if win_title and rec.window and win_title.strip() != rec.window.strip():
        return None
    return rec


def _shot_text_hits(rec: Any, text: str) -> list[dict]:
    """在截图登记的元素里按文字找（plan-365-1774 的内存快路径）。

    与内核 findtext 同一套「归一化 + 包含」口径；按阅读顺序（上→下、左→右）
    排序，首个命中即定位结果、其余作为候选，便于与内核输出格式对齐。
    """
    if rec is None or not text:
        return []
    items = getattr(rec, "items", None) or []
    if not items:
        return []
    hits = desktop_marks.find_by_label(items, text)

    def _order(it: dict) -> tuple[int, int]:
        center = desktop_marks.element_center(it) or (0, 0)
        return (center[1], center[0])

    return sorted(hits, key=_order)


async def _foreground_window() -> dict | None:
    """取当前前台窗口（「不指定窗口就按前台来」是最常见的用法）。"""
    for it in await _window_list():
        if it.get("fg"):
            return it
    return None


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
    （Win10/11 左右各约 7–8 px）。截图与 CDP 坐标换算都以可见边界为基准，
    这样 A6「按帧边界修正 CDP 坐标」与 B2「按窗口裁剪」用的是同一个矩形。
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
    """虚拟桌面矩形（多显示器时是并集），用于判断窗口是否越界。"""
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


async def _capture_region(
    win: dict | None, explicit: str, *, prefer_window: bool = False,
) -> tuple[list[int] | None, str]:
    """决定这次截哪儿：显式 region > （仅 prefer_window 时）目标窗口可见边界 > 全屏。

    为什么默认整屏、不再自动按窗口裁剪（plan-365-1773 的取舍）：
    窗口裁剪会让图片原点非零，模型按坐标换算时必须记得加上原点。
    实录里模型算的是「屏幕坐标 = 图片坐标 × 1.6」，漏掉的正是原点，
    于是点击整体偏移、落到按钮附近的其他控件上——这就是「点击完全偏移」的直接来源。
    整屏截图原点恒为 (0,0)，换算只剩一步乘法；画面聚焦的收益远小于多一次加法带来的错误率。
    prefer_window 只留给「等待画面稳定」这类必须限定区域、且不需要模型参与换算的场景。
    """
    if explicit:
        parts = [p.strip() for p in explicit.split(",")]
        if len(parts) == 4:
            try:
                rect = [int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])]
                if rect[2] > 0 and rect[3] > 0:
                    return (rect, "explicit")
            except ValueError:
                pass
        return (None, "full")

    if prefer_window and win:
        frame = await _window_frame(win)
        if frame:
            vs = await _virtual_screen()
            if vs is None:
                return (frame, "window")
            x, y, w, h = frame
            if (x >= vs[0] and y >= vs[1]
                    and x + w <= vs[0] + vs[2] and y + h <= vs[1] + vs[3]):
                return (frame, "window")
    return (None, "full")


def _boxes_to_items(boxes: list[dict]) -> list[dict]:
    """文字行框 → 与 UIA/CDP 一致的元素结构（供编号与校验共用）。"""
    out: list[dict] = []
    for b in boxes:
        try:
            bx, by = int(b.get("x") or 0), int(b.get("y") or 0)
            bw, bh = int(b.get("w") or 0), int(b.get("h") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "n": str(b.get("t") or ""),
            "ct": "Text",
            "cls": "",
            "en": True,
            # 文字本身不是控件，但自绘界面里它是唯一能对齐的锚点——所以给 ix=True，
            # 让它能进入编号与「附近元素」判定。
            "ix": True,
            "x": bx + bw // 2,
            "y": by + bh // 2,
            "r": [bx, by, bw, bh],
        })
    return out


async def _ocr_boxes(
    meta: dict[str, Any] | None, max_boxes: int = 200, clip: list[int] | None = None,
) -> list[dict]:
    """文字行框（自绘界面唯一可用的元素来源）。给了 meta 就基于那张截图识别，不再截屏。

    clip：屏幕矩形 [x,y,w,h]，只保留与之相交的框。整屏截图时用它把编号限定在
    目标窗口内——否则任务栏、其他窗口的文字也会进编号列表（plan-365-1773）。
    """
    params: dict[str, Any] = {"maxDim": 1600, "maxBoxes": max_boxes}
    if meta and meta.get("path"):
        params.update({
            "image": _abs_image(str(meta["path"])),
            "scale": float(meta.get("scale") or 1.0),
            "srcX": int(meta.get("srcX") or 0),
            "srcY": int(meta.get("srcY") or 0),
        })
    try:
        data = await _call("ocrboxes", params)
    except Exception as exc:  # noqa: BLE001 - OCR 只是兜底来源，失败不影响主流程
        # 不再静默（plan-365-1774）：OCR 失败会让 Chromium 回退到 730 ms 的 UIA 枚举，
        # 静默处理会让「操作变慢」无法排障；这里落一条观测日志。
        _obs("ocr_failed", op="ocrboxes", error=f"{type(exc).__name__}: {exc}"[:160])
        return []
    boxes = list(data.get("boxes") or [])
    if not (clip and len(clip) == 4):
        return boxes
    cx0, cy0, cw, ch = int(clip[0]), int(clip[1]), int(clip[2]), int(clip[3])
    if cw <= 0 or ch <= 0:
        return boxes
    kept: list[dict] = []
    for b in boxes:
        try:
            bx, by = int(b.get("x") or 0), int(b.get("y") or 0)
            bw, bh = int(b.get("w") or 0), int(b.get("h") or 0)
        except (TypeError, ValueError):
            continue
        if bx + bw <= cx0 or bx >= cx0 + cw:
            continue
        if by + bh <= cy0 or by >= cy0 + ch:
            continue
        kept.append(b)
    return kept


async def _collect_elements(
    win: dict | None, meta: dict[str, Any] | None = None,
) -> tuple[list[dict], str]:
    """取一份用于编号与点前校验的元素集合（plan-365-1757 A2 的三级来源）。

    1) Chromium 系窗口 → CDP（网页内完整元素，实测约 10 ms）
    2) Chromium 无 CDP（浏览器未开调试端口）→ OCR 先于 UIA（见下）
    3) 其余窗口 → UIA 快照（原生控件）
    4) 都取不到（自绘界面）→ OCR 文字块

    顺序与感知通道分流一致，保证「模型看到的编号」与「校验用的元素」是同一份事实。
    """
    handle = int((win or {}).get("h") or 0)
    cls = str((win or {}).get("cls") or "")

    if handle and desktop_cdp.is_chromium_window(cls):
        cdp = await _try_cdp(win)
        items = list((cdp or {}).get("items") or [])
        if items:
            return _dedupe_items(items), "cdp"
        # CDP 不可用时 Chromium 的 UIA 树又慢又不完整（实测 0.73 s、到点截断）；
        # OCR 读到的恰是可点的页面文字、耗时减半，且同一张图的识别结果会被随后的
        # 「找文字 / 按文字点击」复用（内容哈希缓存）。因此浏览器窗口把 OCR 提到
        # UIA 之前（plan-365-1774）；OCR 完全读不到内容才回退 UIA。
        clip = await _window_frame(win)
        boxes = await _ocr_boxes(meta, clip=clip)
        if boxes:
            return _dedupe_items(_boxes_to_items(boxes)), "ocr"

    if handle:
        try:
            data = await _call("snapshot", {
                "handle": handle,
                "interactiveOnly": True,
                "budget": 300,
                "depth": 12,
                "timeBudgetMs": 700,
            })
            items = list(data.get("items") or [])
            if items:
                # plan-20261018-operator-boost：自绘 / 内嵌网页的应用（如腾讯视频）UIA 只暴露顶栏几个按钮，
                # 内容区全是「空白」——点击卡片被当成点空白拒绝，按文字也找不到。控件树稀疏时补上 OCR 文字。
                frame = await _window_frame(win)
                if _uia_sparse(items, frame):
                    boxes = await _ocr_boxes(meta, clip=frame)
                    if boxes:
                        return _dedupe_items(items + _boxes_to_items(boxes)), "uia+ocr"
                return _dedupe_items(items), "uia"
        except Exception:  # noqa: BLE001 - 取不到就往下走 OCR 兜底
            pass

    clip = await _window_frame(win) if win else None
    boxes = await _ocr_boxes(meta, clip=clip)
    if boxes:
        return _dedupe_items(_boxes_to_items(boxes)), "ocr"
    return [], "none"


def _uia_sparse(items: list[dict], frame: list[int] | None) -> bool:
    """控件树是否「稀疏」：元素很少，且只分布在窗口里很窄的一条（典型是只有顶栏）。"""
    if len(items) >= 30:
        return False
    if not frame or frame[3] <= 0:
        return len(items) < 15
    ys = [int(it.get("y") or 0) for it in items]
    span = (max(ys) - min(ys)) if ys else 0
    return span < frame[3] * 0.3


def _dedupe_items(items: list[dict]) -> list[dict]:
    """按「名称 + 矩形」去重：Chromium 的嵌套结构常把同一控件报多遍。"""
    out: list[dict] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    for it in items:
        rect = desktop_marks.element_rect(it) or (0, 0, 0, 0)
        key = (str(it.get("n") or ""), rect[0], rect[1], rect[2], rect[3])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


async def _wait_stable(win: dict | None, timeout_ms: int = 3000) -> dict[str, Any]:
    """等待目标窗口画面稳定（plan-365-1757 A7）：消除「图上是 A、点下时已是 B」的错位。

    检测区域限定在目标窗口（prefer_window=True）：任务栏时钟、其他窗口的动画
    也会让全屏判定永远不“稳定”，而这里只关心目标窗口是否还在变。
    """
    region, _src = await _capture_region(win, "", prefer_window=True)
    params: dict[str, Any] = {"timeout": timeout_ms}
    if region:
        params.update({"x": region[0], "y": region[1], "w": region[2], "h": region[3]})
    try:
        return await _call("waitstable", params)
    except Exception:  # noqa: BLE001 - 等待失败不应让动作失败
        return {}


def _dark_ratio(path: str) -> float:
    """截图中接近纯黑的像素占比（64x40 灰度缩略图，约 3 ms）；取不到返回 0。

    plan-20261018-operator-boost：视频 / 游戏等硬件加速或受版权保护的画面截屏时是黑的（实测腾讯视频
    播放中截图 17–20 KB、几乎全黑），模型据此判断「黑屏 / 还在加载」并反复重试。识别出来就明确告诉它。
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            px = list(img.convert("L").resize((64, 40), Image.BILINEAR).getdata())
        return sum(1 for v in px if v < 20) / max(1, len(px))
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


# plan-20261018-operator-boost：模型「实际看到」的图片尺寸上限。
# 多数视觉模型服务端会把大图再缩小（OpenAI 高清模式：短边 768；Anthropic：长边约 1568 / 约 1.15 MP；
# 部分供应商适配层也会缩，如 kiro 的 _render_image）。截图若大于这个尺寸，模型是在缩小后的图上读坐标，
# 工具却按原图像素换算——点击整体往左上偏，小窗口时直接落到窗口外（用户实测）。
# 因此截图一律缩到模型可见尺寸以内，换算比例由工具记录，模型读到的像素与工具换算口径一致。
_MODEL_LONG_MAX = 1568
_MODEL_SHORT_MAX = 768


def _model_safe_max_dim(src_w: int, src_h: int, requested: int) -> int:
    """按源区域尺寸给出传给内核的 maxDim（0 = 不缩放）：不超过模型可见尺寸，显式设置更小时用设置值。"""
    long_edge, short_edge = max(src_w, src_h), min(src_w, src_h)
    if long_edge <= 0 or short_edge <= 0:
        return requested if requested > 0 else _MODEL_LONG_MAX
    limit = min(_MODEL_LONG_MAX, int(long_edge * _MODEL_SHORT_MAX / short_edge))
    if requested > 0:
        return min(requested, limit)
    return limit if long_edge > limit else 0


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


def _obs(event: str, **fields: Any) -> None:
    """电脑操控观测埋点（plan-340-1705 起沿用，plan-365-1757 扩展「点准」维度）。

    只记录、不改行为——这些数据是验收「误点率下降 / 纠偏次数 / 各通道命中」的唯一来源。
    """
    if not fields:
        logger.info("[desktop-obs] %s", event)
        return
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("[desktop-obs] %s %s", event, parts)


async def _take_shot(
    ctx: ToolContext,
    *,
    handle: int = 0,
    title: str = "",
    region: str = "",
    fmt: str = "jpg",
    want_mark: bool | None = None,
    with_list: bool = True,
    max_dim: int | None = None,
    out_path: str = "",
) -> tuple[dict[str, Any], str]:
    """截一张图并登记到任务缓存，返回 (图片结果数据, 给模型看的文本)。

    抽出来是为了让 desktop_screenshot 与「动作后附带截图」（then_shot）用同一条路径——
    否则两处的编号/缓存/内联行为迟早会不一致。
    want_mark / max_dim 为 None 时跟随设置（plan-20261018-operator-boost：此前两处都写死
    「编号开 + 长边 1600」，设置页这两个选项实际从未生效）。
    """
    if want_mark is None:
        want_mark = bool(getattr(settings, "desktop_mark_shot", False))
    if max_dim is None:
        max_dim = _setting_max_dim()
    win = await _resolve_window(handle, title) if (handle or title) else await _foreground_window()
    win_title = str((win or {}).get("title") or title or "")
    # plan-20261018-operator-boost：指定了目标窗口就只截该窗口。plan-365-1773 默认整屏是为了让模型心算时原点为 0；
    # 现在图上坐标一律由工具换算（image_px / 元素坐标），原点不再是负担，而 4K 整屏缩到模型可见尺寸后小字发糊、
    # 小窗口在图里只剩一小块——裁到窗口能把有效分辨率提高数倍，直接减少认错和点错。
    rect, region_src = await _capture_region(win, region, prefer_window=bool(handle or title))
    src_w, src_h = (rect[2], rect[3]) if rect else await _screen_size()
    max_dim = _model_safe_max_dim(int(src_w), int(src_h), int(max_dim))

    params: dict[str, Any] = {
        "maxDim": int(max_dim),
        "format": fmt,
        "quality": int(getattr(settings, "desktop_screenshot_quality", 75)),
        "path": str(out_path or "").strip() or _default_shot_path(ctx, fmt),
    }
    if rect:
        params.update({"x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3]})
    data = await _call("shot", params)

    meta = _shot_meta(data)
    meta["content_hash"] = _file_digest(meta["path"])
    signature = _screen_signature(meta["path"]) if want_mark else b""
    if want_mark:
        reused = _same_screen_reuse(
            ctx, data, meta, win_title, region_src, with_list, signature)
        if reused is not None:
            return reused

    items, channel = await _collect_elements(win, meta)
    marks = desktop_marks.build_marks(items, meta) if (want_mark and items) else []

    mark_path = ""
    if marks:
        candidate = _marked_path(meta["path"])
        if desktop_marks.draw_marks(meta["path"], candidate, marks, meta):
            mark_path = candidate
        else:
            marks = []   # 标注失败就退化成「原图 + 无编号」，不让模型拿着编号去点看不见的框

    shot_id = desktop_shots.new_shot_id()
    desktop_shots.store_shot(
        ctx,
        path=meta["path"],
        meta=meta,
        marks=desktop_marks.mark_index_map(marks) if marks else {},
        mark_path=mark_path,
        window=win_title,
        shot_id=shot_id,
        items=items,
        signature=signature,
    )

    kb = round((data.get("bytes") or 0) / 1024, 1)
    origin = {"explicit": "指定区域", "window": "目标窗口", "full": "整个屏幕"}.get(region_src, "整个屏幕")
    lines = [
        f"截图已保存：{mark_path or meta['path']}",
        f"实际尺寸 {meta['width']}x{meta['height']}（{kb} KB），源区域 "
        f"{meta['srcW']}x{meta['srcH']}（来源：{origin}），屏幕原点 ({meta['srcX']}, {meta['srcY']})，"
        f"缩放比 {meta['scale']:.4f}",
        f"shot_id={shot_id}",
    ]
    # 换算提示给出「一步乘法」形式（plan-365-1773）：乘法比除法直观，且能一眼核对；
    # 整屏截图时原点为 0，公式退化为纯乘法——此前实录中模型漏加原点导致点击整体偏移。
    inv = 1.0 / meta["scale"] if meta["scale"] > 0 else 1.0
    if abs(meta["scale"] - 1.0) < 1e-6 and meta["srcX"] == 0 and meta["srcY"] == 0:
        lines.append("坐标换算参考：本次图片坐标与屏幕物理坐标一致（未缩放、原点为 0）。")
    elif meta["srcX"] == 0 and meta["srcY"] == 0:
        lines.append(
            "坐标换算参考（仅在不用 ref/image_px/image_x 时兜底）："
            f"屏幕坐标 = 图片坐标 × {inv:.4f}"
        )
    else:
        lines.append(
            "坐标换算参考（仅在不用 ref/image_px/image_x 时兜底）："
            f"屏幕坐标 = ({meta['srcX']}, {meta['srcY']}) + 图片坐标 × {inv:.4f}"
        )
    if marks and with_list:
        lines.append(f"\n图中可点击元素已编号（{len(marks)} 个）：")
        lines.extend(desktop_marks.format_marks(marks))
        first = marks[0]
        lines.append(
            f"\n点击请用编号：desktop_click ref={first.index}"
            + (f" title=\"{win_title}\"" if win_title else "")
            + "　—— 换算与校验由工具完成。"
        )
    elif marks:
        lines.append(f"\n（元素编号已叠加在图上，共 {len(marks)} 个；可直接 desktop_click ref=<编号>。）")
    elif not want_mark and items and with_list:
        # plan-20261018-operator-boost：编号默认关闭，改为列出识别到的元素与屏幕坐标——
        # 模型按文字锚点或这里的坐标点击，不必在图上估算像素，也省掉重绘编号图的耗时。
        lines.append(f"\n识别到的元素（屏幕坐标，共 {len(items)} 个，列前 40 个）：")
        lines.extend(_render_items(items, limit=40))
        lines.append(
            "点击优先用 desktop_click text_target=\"可见文字\"（工具重新定位并核对）；"
            "没有文字的目标用上面的坐标 x/y，或你在图上量到的 image_px/image_py。"
        )
    elif not want_mark:
        lines.append(
            "\n（元素编号已关闭，也没有识别到元素。按你在图上量到的像素用 image_px/image_py 点击。）"
        )
    else:
        lines.append("\n（本次未生成元素编号：该界面既没有可读控件树，也没有识别到文字行框。）")
    lines.append(
        "需要按坐标点击时用 image_px/image_py（你在图上量到的像素位置）或 image_x/image_y（0~1 比例），"
        "不要自己换算屏幕坐标。"
    )
    if channel:
        lines.append(f"元素来源通道：{channel}")
    lines.extend(_shot_notes(meta["path"], win, bool(handle or title)))
    _obs("screenshot", channel=channel or "none", marks=len(marks), region=region_src,
         size=f"{meta['width']}x{meta['height']}")

    out = {**data, "shot_id": shot_id, "channel": channel, "mark_path": mark_path,
           "mark_count": len(marks), "region_source": region_src}
    return out, _text(lines)


def _same_screen_reuse(
    ctx: ToolContext,
    data: dict[str, Any],
    meta: dict[str, Any],
    win_title: str,
    region_src: str,
    with_list: bool,
    signature: bytes = b"",
) -> tuple[dict[str, Any], str] | None:
    """画面与上一张截图基本相同时，沿用它的元素与编号图（plan-365-1774）。

    两级判定：先试逐字节相同（最快、最确定），再试缩略图近似匹配——
    桌面截图总在微变（任务栏时钟、光标、JPEG 伪影），只靠逐字节比较几乎永不命中。
    命中后直接省掉元素枚举/OCR/画编号，把「重复查看同一画面」压到接近纯截图耗时。
    """
    digest = str(meta.get("content_hash") or "")
    prev = desktop_shots.latest_with_hash(ctx, digest) if digest else None
    exact = prev is not None
    if prev is None and signature:
        prev = desktop_shots.latest_similar(ctx, signature, window=win_title)
    if (prev is None or not prev.items or not prev.marks
            or not prev.mark_path or not Path(prev.mark_path).exists()):
        return None
    shot_id = desktop_shots.new_shot_id()
    desktop_shots.store_shot(
        ctx, path=meta["path"], meta=meta, marks=dict(prev.marks),
        mark_path=prev.mark_path, window=win_title, shot_id=shot_id,
        items=list(prev.items), signature=signature,
    )
    kb = round((data.get("bytes") or 0) / 1024, 1)
    reason = "画面与上一张截图完全一致" if exact else "画面与上一张截图基本一致（无可见变化）"
    lines = [
        f"截图已保存：{prev.mark_path}",
        f"实际尺寸 {meta['width']}x{meta['height']}（{kb} KB）"
        f"——{reason}，编号沿用上一张；shot_id={shot_id}",
    ]
    if with_list:
        lines.append(f"\n图中可点击元素已编号（{len(prev.marks)} 个）：")
        for idx in sorted(prev.marks):
            m = prev.marks[idx]
            ct = f"  [{m.get('ct')}]" if m.get("ct") else ""
            lines.append(f"#{idx}  {m.get('name') or '(无名称)'}{ct}")
        first = sorted(prev.marks)[0]
        lines.append(
            f"\n点击请用编号：desktop_click ref={first}"
            + (f" title=\"{win_title}\"" if win_title else "")
            + "　—— 换算与校验由工具完成。"
        )
    lines.append(
        "需要按坐标点击时用 image_px/image_py（你在图上量到的像素位置），"
        "或 image_x/image_y（0~1 比例）；不要自己换算屏幕坐标。"
    )
    _obs("screenshot", channel="same-screen", marks=len(prev.marks), region=region_src,
         size=f"{meta['width']}x{meta['height']}")
    out = {**data, "shot_id": shot_id, "channel": "same-screen",
           "mark_path": prev.mark_path, "mark_count": len(prev.marks),
           "region_source": region_src, "reused": True}
    return out, _text(lines)


async def _attach_after_action(
    ctx: ToolContext,
    args: dict[str, Any],
    handle: int,
    title: str,
    output: str,
    data: dict[str, Any],
) -> str:
    """动作后附带观察：可选「等稳定 → 带编号截图 → 快照」（plan-365-1757 B4）。

    点击 / 输入 / 按键三处共用，保证「动作后看结果」的行为完全一致；
    附带观察失败只追加一行说明，绝不让动作本身报错。
    plan-20261018-operator-boost：动作后检查窗口变化——同一应用弹出新窗口 / 前台换了窗口时如实说明，
    并让动作后的截图跟随新的前台窗口（而不是继续截已被遮住的原窗口）。
    """
    before = {"items": list(_LAST_WINDOWS.get("items") or []), "at": float(_LAST_WINDOWS.get("at") or 0)}
    target = None
    for it in before["items"]:
        if (handle and int(it.get("h") or 0) == handle) or (
                not handle and title and title.lower() in str(it.get("title") or "").lower()):
            target = it
            break
    if target is None and not (handle or title):
        target = next((it for it in before["items"] if it.get("fg")), None)
    if bool(args.get("then_wait_stable", False)):
        win = await _resolve_window(handle, title) if (handle or title) else None
        st = await _wait_stable(win)
        if st:
            output += ("\n（已等待画面稳定："
                       + ("稳定" if st.get("stable") else "超时未稳定")
                       + f"，{st.get('elapsedMs')} ms）")
    try:
        _LAST_WINDOWS.update(before)
        note, follow = await _window_change_note(target)
    except Exception:  # noqa: BLE001 — 窗口比对只是附加信息
        note, follow = "", None
    if note:
        output += "\n" + note
        data["window_change"] = {"follow_handle": (follow or {}).get("h")}
    if follow is not None:
        handle, title = int(follow.get("h") or 0), ""
    if bool(args.get("then_shot", False)):
        try:
            shot_data, shot_text = await _take_shot(ctx, handle=handle, title=title)
            output += "\n\n[动作后的界面]\n" + shot_text
            try:
                with open(str(shot_data.get("mark_path") or shot_data.get("path")), "rb") as f:
                    data["base64"] = base64.b64encode(f.read()).decode("ascii")
                output += "\n（新画面已随本次结果送入模型。）"
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001 - 附带截图失败不应让动作本身报错
            output += f"\n（动作后截图失败：{exc}）"
    if bool(args.get("then_snapshot", False)):
        snap = await _snapshot_after_action(ctx, handle=handle, title=title)
        if snap:
            output += "\n\n[动作后的界面]\n" + snap
    return output


# ════════════════════════════════════════════════════════════════
# 感知类（只读，免审）
# ════════════════════════════════════════════════════════════════


class DesktopWindowsTool(Tool):
    name = "desktop_windows"
    risk_level = "low"
    description = (
        "列出当前可见的桌面窗口（标题、进程、位置、是否前台）。\n"
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
            lines.append(
                f"- {w.get('title')}  (进程 {w.get('proc')}, handle={w.get('h')}, "
                f"中心点 {w.get('x')},{w.get('y')}){tag}"
            )
        return ToolResult(ok=True, output=_text(lines), data=data)


class DesktopSnapshotTool(Tool):
    name = "desktop_snapshot"
    risk_level = "low"
    description = (
        "读取指定窗口的可交互元素及其中心坐标，是感知界面的首选方式。\n"
        "会自动选通道：Chromium 系窗口（Chrome/Edge/Electron/WebView2）走 CDP，能取到网页内完整内容；\n"
        "其余窗口走 UI Automation。元素带 x,y 中心点，可直接用于 desktop_click。\n"
        "用法：一次调用即可拿到当前界面的全部元素，不要为了确认同一界面反复调用；\n"
        "只想确认某个坐标是什么时用 desktop_hit（更快），不必重新快照整个窗口。\n"
        "返回元素数为 0：说明是自绘界面（如部分音乐/游戏客户端），请改用 desktop_screenshot\n"
        "截图后再用 desktop_find_text 按可见文字定位；若来自 Chromium 窗口，属调试端口未开的\n"
        "正常回退（已自动改用 UI Automation），无需额外处理。\n"
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
                            "description": "只返回可交互元素，默认 true；设为 false 可看到全部结构",
                        },
                        "budget": {"type": "integer", "description": "最多返回多少个元素，默认 200"},
                        "depth": {
                            "type": "integer",
                            "description": "最大遍历深度，默认 12。深层元素在网页/Electron 中几乎不是可交互项，调大只会显著变慢。",
                        },
                        "time_budget_ms": {
                            "type": "integer",
                            "description": "遍历时间预算（毫秒），默认 800。到点即返回已收集元素并标记截断。",
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

        # 通道分流（plan-340-1705 L1.1）：Chromium 系窗口优先走 CDP。
        # 依据是本机同窗口实测——UIA 171 ms 且只返回窗口边框按钮，CDP 20 ms 且返回
        # 网页内全部可交互元素；信息完整度直接决定模型要不要靠推理去补全。
        win = await _resolve_window(handle, title)
        # plan-20261018-operator-boost：观察某个应用时顺带附上它已沉淀的操作路线
        route = await _desktop_hint(ctx, win, title)
        if win and desktop_cdp.is_chromium_window(str(win.get("cls") or "")):
            cdp = await _try_cdp(win)
            if cdp:
                res = _cdp_result(cdp, int(args.get("budget") or 200))
                if route:
                    res.output += route
                    res.data = {**(res.data or {}), "route_hint": True}
                return res

        try:
            data = await _call("snapshot", {
                "title": title,
                "handle": handle,
                "interactiveOnly": bool(args.get("interactive_only", True)),
                "budget": int(args.get("budget") or 200),
                "depth": int(args.get("depth") or 12),
                "timeBudgetMs": int(args.get("time_budget_ms") or 800),
            })
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        nodes = data.get("nodes", 0)
        if nodes == 0:
            return ToolResult(
                ok=True,
                output=(
                    "该窗口没有可通过 UI Automation 读取的控件（元素数 0）。\n"
                    "这通常意味着它是自绘界面。请改用 desktop_screenshot 截图，\n"
                    "再用 desktop_find_text 按可见文字定位坐标。"
                ),
                data=data,
            )

        items = data.get("items") or []
        lines = [f"共 {nodes} 个元素："]
        lines.extend(_render_items(items))
        if nodes > 120:
            lines.append(f"... 其余 {nodes - 120} 个元素已省略（可调小 budget 或加 filter）")
        # 遍历受深度/节点数/时间三重上限约束，命中任一上限都必须明说——
        # 否则模型会以为这就是窗口的全部元素，漏掉更深的可交互项。
        if data.get("truncated"):
            lines.append(
                f"注意：已达遍历上限（深度 12 / 访问 {data.get('visited', 0)} 个节点 / 时间 "
                f"800ms），下方元素可能不完整。若找不到目标元素，可改用 desktop_find_text "
                "按可见文字定位，或先用 desktop_hit 确认坐标处的元素。"
            )
        hint = _next_step_hint(items)
        if hint:
            lines.append(hint)
        # plan-20261018-operator-boost：控件树只覆盖窗口一小条（常见于自绘 / 内嵌网页的应用）时直说，
        # 否则模型会以为内容区「什么都没有」而判断成黑屏或加载中
        if win and _uia_sparse(items, await _window_frame(win)):
            lines.append(
                "注意：控件树只覆盖了窗口顶部一小块，内容区是自绘的、读不到控件——这不代表内容区为空。"
                "请用 desktop_screenshot（会附带识别到的文字与坐标）或 desktop_find_text 按可见文字定位，"
                "点击时用 text_target。"
            )
        if route:
            data = {**data, "route_hint": True}
        return ToolResult(ok=True, output=_text(lines) + route, data=data)


class DesktopHitTool(Tool):
    name = "desktop_hit"
    risk_level = "low"
    description = (
        "查询屏幕上某个坐标位置是什么元素（最便宜的感知方式，约 2.5 毫秒）。\n"
        "当你已经知道大致位置、或想确认某个坐标上方是什么控件时使用；\n"
        "返回元素及其上级层级链，便于判断「这个按钮属于哪个工具栏/窗口」。\n"
        "注意：命中测试基于 Windows UI Automation，在 Chromium 系窗口（Chrome/Electron 等）上\n"
        "只能识别到窗口边框控件、看不到网页内容——要看网页里的元素请改用 desktop_snapshot。\n"
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
                        "x": {"type": "integer", "description": "屏幕横坐标（物理像素）"},
                        "y": {"type": "integer", "description": "屏幕纵坐标（物理像素）"},
                        "chain": {"type": "boolean", "description": "是否返回上级层级链，默认 true"},
                    },
                    "required": ["x", "y"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        try:
            data = await _call("hit", {
                "x": int(args.get("x") or 0),
                "y": int(args.get("y") or 0),
                "chain": bool(args.get("chain", True)),
            })
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        if not data.get("found"):
            return ToolResult(ok=True, output="该位置没有可识别的 UI 元素（可能是空白或自绘区域）。", data=data)

        lines = [f"该位置是：{data.get('n') or '(无名称)'}  [{data.get('ct')}]  ({data.get('x')},{data.get('y')})"]
        chain = data.get("chain") or []
        if chain:
            lines.append("层级链（由内到外）：")
            for c in chain:
                lines.append(f"  - {c.get('n') or '(无名称)'}  [{c.get('ct')}]")
        return ToolResult(ok=True, output=_text(lines), data=data)


class DesktopScreenshotTool(Tool):
    name = "desktop_screenshot"
    risk_level = "low"
    description = (
        "截取屏幕（或指定区域），保存为图片并返回路径与尺寸。\n"
        "只在前两种感知方式都无效时使用（自绘界面，如部分音乐/游戏客户端）：\n"
        "先试 desktop_snapshot（能直接给出可点击坐标），再试 desktop_hit。\n"
        "默认截取整个屏幕；给了 title/handle 时只截该窗口（更清晰，小窗口优先这样截）。\n"
        "图片会缩放到模型可见尺寸，按图上像素点击时由工具换算回屏幕坐标，\n"
        "并列出识别到的元素与屏幕坐标；「设置 → 电脑操控 → 截图元素编号」开启时改为在图上叠加编号。\n"
        "看到图后请优先用 desktop_click text_target=<可见文字>（编号开启时也可 ref=<编号>）；\n"
        "确实要按坐标点击时用 image_px/image_py（你在图上量到的像素位置），"
        "不要自己把图片坐标换算成屏幕坐标——换算由工具完成，你不必也不应该心算。\n"
        "小目标看不清可用 zoom_at=\"x,y\" 放大复核；动效/加载界面可用 wait_stable=true 等画面稳定。\n"
        "每次调用都截取当前画面；画面与上一张完全一致（逐字节相同）时编号直接沿用上一张。\n"
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
                        "title": {"type": "string", "description": "目标窗口标题（包含匹配）；给了就只截该窗口（更清晰），并限定元素识别范围"},
                        "region": {
                            "type": "string",
                            "description": '截图区域 "x,y,w,h"（屏幕物理像素）；不填则截取整个屏幕',
                        },
                        "max_dim": {"type": "integer", "description": "缩放后长边像素；默认跟随设置（0 = 自动适配模型可见尺寸），任何取值都不会超过模型可见尺寸"},
                        "format": {"type": "string", "description": "jpg 或 png，默认 jpg"},
                        "mark": {
                            "type": "boolean",
                            "description": "是否给可点击元素叠加编号，默认跟随「设置 → 电脑操控」中的开关",
                        },
                        "with_elements": {
                            "type": "boolean",
                            "description": "是否附上元素编号清单，默认 true",
                        },
                        "zoom_at": {
                            "type": "string",
                            "description": '围绕该屏幕坐标放大复核，格式 "x,y"；小目标看不清时用',
                        },
                        "zoom_radius": {
                            "type": "integer",
                            "description": "配合 zoom_at：放大区域半径（像素），默认 160",
                        },
                        "wait_stable": {
                            "type": "boolean",
                            "description": "截图前先等画面稳定（默认 false），动效/加载中的界面建议开启",
                        },
                        "inline": {
                            "type": "boolean",
                            "description": "是否把图片直接送入模型（默认 true，一步到位）；"
                                           "仅当只需留档、不看内容时设为 false",
                        },
                        "out_path": {
                            "type": "string",
                            "description": "自定义保存路径；默认落在工作区 .chatcoder/desktop-shots/",
                        },
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        zoom_at = str(args.get("zoom_at") or "").strip()
        if zoom_at:
            return await self._zoom(zoom_at, args, ctx)

        inline = bool(args.get("inline", True))
        want_mark = bool(args.get("mark", getattr(settings, "desktop_mark_shot", False)))
        want_list = bool(args.get("with_elements", True))
        fmt = str(args.get("format") or "jpg")
        region_raw = str(args.get("region") or "").strip()
        handle = int(args.get("handle") or 0)
        title = str(args.get("title") or "").strip()

        win = await _resolve_window(handle, title) if (handle or title) else await _foreground_window()

        # 已移除「1.2 秒内复用上一条结果」（plan-365-1773）：复用窗口会让模型看到旧画面，
        # 据此误判「没生效」或按旧图坐标点击。每次调用都必须截取当前画面。
        if bool(args.get("wait_stable", False)):
            await _wait_stable(win)

        try:
            data, text = await _take_shot(
                ctx,
                handle=handle,
                title=title,
                region=region_raw,
                fmt=fmt,
                want_mark=want_mark,
                with_list=want_list,
                max_dim=(int(args["max_dim"]) if args.get("max_dim") is not None else None),
                out_path=str(args.get("out_path") or "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        if inline:
            shown = str(data.get("mark_path") or data.get("path") or "")
            try:
                with open(shown, "rb") as f:
                    data["base64"] = base64.b64encode(f.read()).decode("ascii")
                text += "\n图片已随本次结果送入模型，无需再调用 view_image。"
            except OSError as exc:
                text += f"\n（图片内联失败，可自行用 view_image 查看：{exc}）"
        else:
            text += "\n可用 view_image 查看该图片。"
        # plan-20261018-operator-boost：截图对象（前台窗口或指定窗口）已沉淀的路线随结果附上
        route = await _desktop_hint(ctx, win, title)
        if route:
            text += route
            data["route_hint"] = True
        return ToolResult(ok=True, output=text, data=data)

    async def _zoom(self, zoom_at: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """围绕坐标放大复核（plan-365-1757 A5）。

        小目标（复选框、列表缩略图）在全屏缩放图上看不清时，业界做法是先裁剪放大再看一眼——
        比让模型「凭模糊图猜」准得多。放大图同样登记为最近截图，因此可以直接用
        image_x/image_y 在这张图上点击。
        """
        parts = [p.strip() for p in zoom_at.split(",")]
        if len(parts) != 2:
            return ToolResult(ok=False, output="", error='zoom_at 格式应为 "x,y"（屏幕物理像素）')
        try:
            zx, zy = int(parts[0]), int(parts[1])
        except ValueError:
            return ToolResult(ok=False, output="", error='zoom_at 格式应为 "x,y"（屏幕物理像素）')

        fmt = str(args.get("format") or "jpg")
        params: dict[str, Any] = {
            "x": zx,
            "y": zy,
            "radius": int(args.get("zoom_radius") or 160),
            # 放大图同样不能超过模型可见尺寸（方形区域即短边 768），否则模型读到的像素与换算口径不一致
            "maxDim": _model_safe_max_dim(
                2 * int(args.get("zoom_radius") or 160), 2 * int(args.get("zoom_radius") or 160),
                int(args.get("max_dim") if args.get("max_dim") is not None else 1200)),
            "format": fmt,
            "quality": int(getattr(settings, "desktop_screenshot_quality", 75)),
            "path": str(args.get("out_path") or "").strip() or _default_shot_path(ctx, fmt),
        }
        try:
            data = await _call("zoom", params)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        meta = _shot_meta(data)
        desktop_shots.store_shot(
            ctx, path=meta["path"], meta=meta, shot_id=desktop_shots.new_shot_id())
        lines = [
            f"已放大复核 ({zx},{zy}) 周边区域：{meta['path']}",
            f"裁剪区起点 x={meta['srcX']} y={meta['srcY']}，尺寸 {meta['srcW']}x{meta['srcH']}，"
            f"缩放比 {meta['scale']:.4f}",
            "看清目标后可直接点击：desktop_click image_px/image_py"
            "（填你在**本放大图**上量到的像素位置），或 image_x/image_y（0~1 比例）。",
        ]
        _obs("zoom", at=f"{zx},{zy}", scale=f"{meta['scale']:.3f}")
        if bool(args.get("inline", True)):
            try:
                with open(meta["path"], "rb") as f:
                    data["base64"] = base64.b64encode(f.read()).decode("ascii")
                lines.append("图片已随本次结果送入模型。")
            except OSError as exc:
                lines.append(f"（图片内联失败，可自行用 view_image 查看：{exc}）")
        return ToolResult(ok=True, output=_text(lines), data=data)


# ════════════════════════════════════════════════════════════════
# 执行类（写操作，需审批）
# ════════════════════════════════════════════════════════════════


@dataclass
class _ClickTarget:
    """解析后的点击目标。error 非空表示解析失败，此时绝不能点击。"""

    x: int = 0
    y: int = 0
    label: str = ""
    coord_source: str = ""
    error: str = ""
    # plan-365-1769 F2：编号来自较旧一代截图、或最新截图已被写动作标记失效。
    # 此时不直接照点，先用元素名在当前画面重新定位（界面变了但元素还在）。
    stale_ref: bool = False
    ref_index: int = 0


class DesktopClickTool(Tool):
    name = "desktop_click"
    risk_level = "high"
    description = (
        "点击鼠标。优先用「语义锚点」而不是坐标——这样工具能在点之前核对目标：\n"
        "- ref=<编号>：点桌面上一次截图里编号为 #N 的元素（最稳，推荐）；\n"
        "- text_target=\"可见文字\"：按屏幕上的文字定位后再点（自绘界面主力）；\n"
        "- image_px/image_py：你在截图上量到的像素位置（如 x=420, y=118），工具负责换算；\n"
        "  推荐优先使用，不要自己把图片坐标换算成屏幕坐标；\n"
        "- image_x/image_y：相对最近一次截图的 0~1 比例（图片中心即 0.5/0.5），备选；\n"
        "- x/y：直接给屏幕物理像素坐标（兼容用法，能不用就不用）。\n"
        "给了 expect（该位置应是什么元素）时，工具会在点击前核对：位置有小偏差会纠偏到元素中心；\n"
        "找不到目标则拒绝执行，并回报该位置实际是什么，避免误点。\n"
        "破坏性操作（发送/删除/支付等）请设 destructive=true：改为「重新定位确证后再点」，确证不了就拒绝。\n"
        "**一步完成整段操作**：点击 → 清空 → 输入 → 回车可合并成一次调用，例如地址栏输入\n"
        "desktop_click ref=18 clear_first=true text=\"https://x.com\" keys=ENTER；\n"
        "真正的延迟来自模型往返（每次调用都是一轮），能合并的动作不要拆成多次。\n"
        "若这次点击会改变界面，设 then_shot=true：新画面在同一次调用里一并返回（省一轮往返）；\n"
        "配合 then_wait_stable=true 可先等画面稳定再截图。\n"
        "安全：默认要求目标窗口处于前台，否则拒绝执行（防止输入打到错误窗口）；\n"
        "若目标窗口不在前台，可给出 title/handle 并设 focus_first=true，本次调用会先激活它再点击。\n"
        "失败时先看错误信息给的修正建议，不要原样重试同一次调用。\n"
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
                        "ref": {
                            "type": "integer",
                            "description": "桌面上一次截图里的元素编号（如 7 表示 #7）；最推荐",
                        },
                        "text_target": {
                            "type": "string",
                            "description": "要点击的可见文字；按文字定位后再点，适合自绘界面",
                        },
                        "image_px": {
                            "type": "number",
                            "description": "截图内横向像素坐标（你在图上量到的 x）；工具负责换算，推荐",
                        },
                        "image_py": {
                            "type": "number",
                            "description": "截图内纵向像素坐标（你在图上量到的 y）",
                        },
                        "image_x": {
                            "type": "number",
                            "description": "相对最近一次截图的横向比例 0~1（图片中心 0.5）",
                        },
                        "image_y": {
                            "type": "number",
                            "description": "相对最近一次截图的纵向比例 0~1（图片中心 0.5）",
                        },
                        "shot_id": {
                            "type": "string",
                            "description": "image_x/image_y 所参照的截图 id；默认用最近一次",
                        },
                        "x": {"type": "integer", "description": "屏幕横向物理像素坐标（兼容用法）"},
                        "y": {"type": "integer", "description": "屏幕纵向物理像素坐标（兼容用法）"},
                        "expect": {
                            "type": "string",
                            "description": "该位置应当是什么元素（名称包含匹配）；给了就会在点前核对",
                        },
                        "destructive": {
                            "type": "boolean",
                            "description": "是否破坏性操作（发送/删除/支付/清空等），默认 false；"
                                           "为 true 时会先重新定位确证目标再执行",
                        },
                        "allow_blank": {
                            "type": "boolean",
                            "description": "是否允许点击空白/无名称区域（默认 false）。仅在确需点击"
                                           "无名称图标或画布时开启；坐标算错落到空白处会被拦截提示。",
                        },
                        "title": {"type": "string", "description": "目标窗口标题（包含匹配），用于安全校验；给了就会拒绝落在该窗口外的点击"},
                        "handle": {"type": "integer", "description": "目标窗口句柄"},
                        "allow_outside": {
                            "type": "boolean",
                            "description": "给了 title/handle 但确需点击窗口外（如任务栏）时设 true，默认 false",
                        },
                        "button": {"type": "string", "description": "left / right / middle，默认 left"},
                        "count": {"type": "integer", "description": "点击次数，2 表示双击，默认 1"},
                        "focus_first": {"type": "boolean", "description": "点击前先激活目标窗口，默认 false"},
                        "then_snapshot": {
                            "type": "boolean",
                            "description": "动作完成后自动附带一张新快照（默认 false），可省一轮模型往返。",
                        },
                        "then_shot": {
                            "type": "boolean",
                            "description": "动作完成后自动附带一张带编号的截图（默认 false），"
                                           "「操作 + 看结果」一次调用完成。",
                        },
                        "then_wait_stable": {
                            "type": "boolean",
                            "description": "动作后先等画面稳定再截图（默认 false），配合 then_shot 使用。",
                        },
                        "text": {"type": "string", "description": "点击后紧接着输入的文本（可选）"},
                        "keys": {"type": "string", "description": "输入后紧接着按下的键，如 ENTER（可选）"},
                        "clear_first": {"type": "boolean", "description": "输入前先全选清空，默认 false"},
                    },
                },
            },
        }

    async def _elements(
        self, ctx: ToolContext, win: dict | None, *, fresh: bool = False,
    ) -> list[dict]:
        """点前校验用的元素集合：优先复用最近截图的登记元素（plan-365-1774）。

        复用依据：重新采集一次在 Chromium 上实测 0.73 s（UIA）、OCR 自拍 0.53 s；
        而「截图 → 点击」通常只隔几秒、画面未变，复用的就是模型看到的那份事实。
        写动作后的截图（stale）只给 60 秒短窗：界面可能已变，超窗必须重新感知。
        破坏性操作（fresh=True）不走复用：必须按当前画面确证，宁可多花一次采集。
        """
        if not fresh:
            rec = _reusable_shot(ctx, win)
            if rec is not None:
                _obs("elements_reuse", age=f"{rec.age():.1f}s", stale=rec.stale)
                return list(rec.items)
        items, _channel = await _collect_elements(win)
        return items

    async def _by_text(
        self, text: str, handle: int, title: str, ctx: ToolContext,
    ) -> _ClickTarget:
        """按可见文字定位（A4）。唯一命中才点；多命中交回模型，避免「猜一个点下去」。

        提速（plan-365-1774）：先在最近截图的登记元素里内存匹配（0 成本）；未命中
        再把同一张图交给内核识别（OCR 内容哈希缓存命中时毫秒级）；最后才现截；
        复用图未命中会按当前画面再找一次，保证「找得到 = 现在真的在这里」。
        """
        win = await _resolve_window(handle, title) if (handle or title) else None
        rec = _reusable_shot(ctx, win, stale_ttl=REUSE_TEXT_STALE_TTL_S)
        hits = _shot_text_hits(rec, text)
        if len(hits) == 1:
            center = desktop_marks.element_center(hits[0])
            if center is not None:
                return _ClickTarget(
                    x=center[0], y=center[1],
                    label=desktop_marks.element_name(hits[0]) or text,
                    coord_source="文字定位（复用最近截图）",
                )
        if len(hits) > 1:
            listing = "；".join(
                f"「{desktop_marks.element_name(h)}」" for h in hits[:5]
            )
            return _ClickTarget(error=(
                f"「{text}」匹配到 {len(hits)} 处，无法确定点哪一个：{listing}。"
                "请改用更精确的文字，或先用 desktop_screenshot 拿编号后以 ref 指定。"
            ))

        params: dict[str, Any] = {"text": text, "maxDim": 1600, "maxCandidates": 8}
        if rec is not None and rec.meta.get("path"):
            # 复用同一张截图：与模型所见同源，且内核按内容哈希缓存 OCR 结果
            params.update({
                "image": _abs_image(str(rec.meta["path"])),
                "scale": float(rec.meta.get("scale") or 1.0),
                "srcX": int(rec.meta.get("srcX") or 0),
                "srcY": int(rec.meta.get("srcY") or 0),
            })
        elif win:
            frame = await _window_frame(win)
            if frame:
                params["region"] = f"{frame[0]},{frame[1]},{frame[2]},{frame[3]}"
        try:
            data = await _call("findtext", params)
        except Exception as exc:  # noqa: BLE001
            return _ClickTarget(error=f"文字定位失败：{exc}")

        if not data.get("found") and params.get("image"):
            # 复用图未命中：截图之后界面可能出现新文字——按当前画面再找一次
            for key in ("image", "scale", "srcX", "srcY"):
                params.pop(key, None)
            if win:
                frame = await _window_frame(win)
                if frame:
                    params["region"] = f"{frame[0]},{frame[1]},{frame[2]},{frame[3]}"
            try:
                data = await _call("findtext", params)
            except Exception as exc:  # noqa: BLE001
                return _ClickTarget(error=f"文字定位失败：{exc}")

        if not data.get("found"):
            visible = str(data.get("visible") or "")[:200]
            msg = f"屏幕上没有找到「{text}」。"
            if visible:
                msg += f"当前可见文字：{visible}"
            return _ClickTarget(error=msg)

        cands = list(data.get("candidates") or [])
        if len(cands) > 1:
            listing = "；".join(
                f"「{c.get('text')}」({c.get('x')},{c.get('y')})" for c in cands[:5]
            )
            return _ClickTarget(error=(
                f"「{text}」匹配到 {len(cands)} 处，无法确定点哪一个：{listing}。"
                "请改用更精确的文字，或先用 desktop_screenshot 拿编号后以 ref 指定。"
            ))
        return _ClickTarget(
            x=int(data.get("x") or 0),
            y=int(data.get("y") or 0),
            label=str(data.get("text") or text),
            coord_source="文字定位",
        )

    async def _resolve_target(
        self, args: dict[str, Any], ctx: ToolContext, handle: int, title: str,
    ) -> _ClickTarget:
        """把锚点（编号 / 文字 / 图片像素 / 图片比例 / 屏幕坐标）统一解析成屏幕坐标。"""
        ref = int(args.get("ref") or 0)
        if ref:
            target = desktop_shots.lookup_ref(ctx, ref)
            if target is None:
                # 文案不再引导「重新截图」——那会形成「重截 → 编号又失效」的死循环
                # （plan-365-1769 F2），改用锚点替代路径。
                return _ClickTarget(error=(
                    f"编号 #{ref} 不在本任务近期的编号表中（截图已超过有效期限）。"
                    "可改用 text_target=\"元素名\" 直接按文字点击，或重新截图获取最新编号。"
                ))
            return _ClickTarget(
                x=int(target.get("x") or 0),
                y=int(target.get("y") or 0),
                label=str(target.get("name") or ""),
                coord_source=f"元素编号 #{ref}（截图 {target.get('shot_id')}）",
                stale_ref=bool(target.get("stale")),
                ref_index=ref,
            )

        text_target = str(args.get("text_target") or "").strip()
        if text_target:
            return await self._by_text(text_target, handle, title, ctx)

        if args.get("image_px") is not None and args.get("image_py") is not None:
            rec = desktop_shots.get_shot(ctx, str(args.get("shot_id") or ""))
            if rec is None:
                return _ClickTarget(error=(
                    "image_px/image_py 需要参照一次截图，但当前没有可用截图（可能已过期）。"
                    "请先 desktop_screenshot。"
                ))
            px = float(args["image_px"])
            py = float(args["image_py"])
            # 越界校验（plan-365-1773）：明显超出图片范围的像素值多半是坐标给错了
            # （例如误把屏幕坐标当图片坐标），直接报错比“点到画面外”安全得多。
            mw = float(rec.meta.get("width") or 0)
            mh = float(rec.meta.get("height") or 0)
            if mw > 0 and mh > 0 and not (0 <= px <= mw and 0 <= py <= mh):
                return _ClickTarget(error=(
                    f"image_px/image_py=({px:.0f},{py:.0f}) 超出图片范围 "
                    f"{mw:.0f}x{mh:.0f}。这两个参数要填你在图上量到的像素位置，"
                    "不是屏幕坐标；若确实是屏幕坐标请改用 x/y。"
                ))
            sx, sy = desktop_marks.image_to_screen(rec.meta, px, py)
            return _ClickTarget(
                x=sx, y=sy,
                coord_source=f"图片像素坐标 ({px:.0f},{py:.0f}) → 截图 {rec.shot_id}",
            )

        if args.get("image_x") is not None and args.get("image_y") is not None:
            rec = desktop_shots.get_shot(ctx, str(args.get("shot_id") or ""))
            if rec is None:
                return _ClickTarget(error=(
                    "image_x/image_y 需要参照一次截图，但当前没有可用截图（可能已过期）。"
                    "请先 desktop_screenshot。"
                ))
            ix = float(args["image_x"])
            iy = float(args["image_y"])
            sx, sy = desktop_marks.parse_normalized(rec.meta, ix, iy)
            return _ClickTarget(
                x=sx, y=sy,
                coord_source=f"图片归一化坐标 ({ix:.3f},{iy:.3f}) → 截图 {rec.shot_id}",
            )

        if args.get("x") is None or args.get("y") is None:
            return _ClickTarget(error=(
                "请给出点击目标：ref（元素编号）/ text_target（可见文字）/ "
                "image_x,image_y（相对截图的 0~1 比例）/ x,y（屏幕物理像素）之一。"
            ))
        return _ClickTarget(
            x=int(args["x"]), y=int(args["y"]),
            coord_source="屏幕坐标（直接传入）",
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        handle = int(args.get("handle") or 0)
        title = str(args.get("title") or "")
        expect = str(args.get("expect") or "").strip()

        resolved = await self._resolve_target(args, ctx, handle, title)
        if resolved.error:
            return ToolResult(ok=False, output="", error=resolved.error)

        win = await _resolve_window(handle, title) if (handle or title) else None

        # 破坏性判定：模型显式声明，或目标文字命中风险词表（词表可在设置页编辑）。
        # 命中后不做自动纠偏，改为「重新定位确证后再执行」——纠偏会把一次明确的破坏性
        # 操作搬到别处，风险高于收益。
        words = desktop_marks.split_words(getattr(settings, "desktop_destructive_words", ""))
        destructive = bool(args.get("destructive", False)) or desktop_marks.is_destructive(
            expect or resolved.label, words)

        items = await self._elements(ctx, win, fresh=destructive)

        # 编号来自旧代次截图（或被写动作标记失效）：不直接照点——先用元素名在当前画面
        # 重新定位（plan-365-1769 F2）。界面变化后元素往往还在，按名定位能救回这次点击；
        # 救不回时交给点前校验统一裁决（并把元素名作为 expect，避免又点回旧位置）。
        if resolved.stale_ref and resolved.label:
            hits = desktop_marks.find_by_label(items, resolved.label)
            center = desktop_marks.element_center(hits[0]) if len(hits) == 1 else None
            if center is not None:
                resolved = _ClickTarget(
                    x=center[0], y=center[1], label=resolved.label,
                    coord_source=f"编号 #{resolved.ref_index}（已过期）→ 按名重新定位",
                )
            else:
                expect = expect or resolved.label
                resolved = _ClickTarget(
                    x=resolved.x, y=resolved.y, label=resolved.label,
                    coord_source=f"编号 #{resolved.ref_index}（已过期，未能按名唯一重定位）",
                )

        # 校验档：默认宽松（plan-365-1769 F5）；设置页可切 关闭/宽松/严格
        mode = desktop_marks.normalize_verify_mode(
            getattr(settings, "desktop_verify_click", desktop_marks.VERIFY_MODE_LENIENT))
        decision = desktop_marks.decide_click(
            items, resolved.x, resolved.y,
            expect=expect, destructive=destructive, mode=mode,
            allow_blank=bool(args.get("allow_blank", False)),
        )

        if decision.action == "refuse":
            _obs("click_refused", destructive=destructive, reason=decision.reason)
            return ToolResult(
                ok=False, output="", error=decision.reason,
                data={"action": "refuse", "at": [resolved.x, resolved.y],
                      "coord_source": resolved.coord_source, "notes": decision.notes},
            )

        x, y = decision.x, decision.y
        # plan-20261018-operator-boost：给了目标窗口时，点位必须落在该窗口可见范围内。
        # 坐标换算出错（例如模型在被缩小的图上读坐标）时点击会落到窗口外、点中别的程序或桌面——
        # 宁可拒绝并说明，也不静默点错。确需点窗口外（任务栏、托盘）时去掉 title/handle 或设 allow_outside。
        # 最小化的窗口边界在屏幕外（-32000），focus_first 会先还原它，此时不校验
        if win and not win.get("min") and not bool(args.get("allow_outside", False)):
            frame = await _window_frame(win)
            if frame:
                fx, fy, fw, fh = frame
                if not (fx <= x < fx + fw and fy <= y < fy + fh):
                    _obs("click_outside_window", at=f"{x},{y}", frame=f"{fx},{fy},{fw},{fh}",
                         source=resolved.coord_source)
                    return ToolResult(
                        ok=False, output="",
                        error=(
                            f"点击点 ({x},{y}) 不在目标窗口「{win.get('title') or title}」的范围内"
                            f"（窗口区域 x={fx} y={fy} 宽 {fw} 高 {fh}），已拒绝执行——多半是坐标换算有误。"
                            "请改用 text_target=\"可见文字\"，或按截图上量到的像素传 image_px/image_py（换算由工具完成），"
                            "不要自己把图片坐标换算成屏幕坐标；确需点击窗口外时去掉 title/handle 或设 allow_outside=true。"
                        ),
                        data={"action": "refuse", "at": [x, y], "frame": frame,
                              "coord_source": resolved.coord_source},
                    )
        params = {
            "x": x,
            "y": y,
            "title": title,
            "handle": handle,
            "button": str(args.get("button") or "left"),
            "count": int(args.get("count") or 1),
            "focusFirst": bool(args.get("focus_first", False)),
            "requireForeground": bool(getattr(settings, "desktop_require_foreground", True)),
            "text": str(args.get("text") or ""),
            "keys": str(args.get("keys") or ""),
            "clearFirst": bool(args.get("clear_first", False)),
        }
        try:
            data = await _call("click", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=await _not_foreground_hint(exc, handle, title))

        # 动作已改变界面：动作前的截图不再代表「当前画面」（plan-365-1769 F4）。
        # 必须在 _attach_after_action 之前——其内部的 then_shot 会写入新截图。
        desktop_shots.mark_stale(ctx)

        at = data.get("at") or [x, y]
        parts = [f"已在 ({at[0]},{at[1]}) 点击"]
        if decision.action == "correct":
            parts.append(f"（{decision.reason}）")
        elif decision.action == "relocate":
            parts.append(f"（{decision.reason}）")
        if resolved.coord_source:
            parts.append(f"坐标来源：{resolved.coord_source}")
        target_name = (desktop_marks.element_name(decision.target)
                       if decision.target is not None else resolved.label)
        if target_name:
            parts.append(f"目标元素「{target_name}」")
        if data.get("target"):
            parts.append(f"窗口「{data['target']}」")
        if data.get("typed"):
            parts.append(f"输入了 {data['typed']} 个字符")
        if data.get("keysError"):
            parts.append(f"按键失败：{data['keysError']}")
        output = "，".join(parts) + "。"
        if decision.notes:
            output += " " + "；".join(decision.notes) + "。"

        output = await _attach_after_action(ctx, args, handle, title, output, data)

        _obs("click", coord_source=resolved.coord_source, action=decision.action,
             dx=(decision.delta or (0, 0))[0], dy=(decision.delta or (0, 0))[1],
             destructive=destructive, mode=mode, target=target_name or "-")

        data.update({
            "coord_source": resolved.coord_source,
            "action": decision.action,
            "corrected": decision.action == "correct",
            "relocated": decision.action == "relocate",
            "delta": list(decision.delta) if decision.delta else None,
            "target_element": target_name,
            "destructive": destructive,
            "verify_mode": mode,
        })
        return ToolResult(ok=True, output=output, data=data)


class DesktopTypeTool(Tool):
    name = "desktop_type"
    risk_level = "high"
    description = (
        "向目标窗口输入文本（支持中文，走 Unicode 输入而非剪贴板）。\n"
        "**一步完成「输入 + 回车」**：提交类输入设 keys=\"ENTER\"，不必再单独调一次 desktop_keys。\n"
        "安全：默认要求目标窗口处于前台，否则拒绝执行——防止输入落到意外的窗口。\n"
        "若目标窗口当前不在前台，请给出 title/handle 并设 focus_first=true：\n"
        "本次调用会先激活它再输入，不必先单独跑一次 desktop_focus。\n"
        "若输入会让界面变化，可设 then_shot=true 让新画面在同一次调用里返回（省一轮往返）。\n"
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
                        "title": {"type": "string", "description": "目标窗口标题（用于安全校验）"},
                        "handle": {"type": "integer", "description": "目标窗口句柄"},
                        "focus_first": {
                            "type": "boolean",
                            "description": "输入前先激活目标窗口（需同时给出 title 或 handle），默认 false",
                        },
                        "clear_first": {"type": "boolean", "description": "输入前先全选清空，默认 false"},
                        "keys": {
                            "type": "string",
                            "description": "输入后紧接着按下的键，如 ENTER；一步完成「输入 + 提交」",
                        },
                        "then_snapshot": {
                            "type": "boolean",
                            "description": "输入完成后自动附带一张新快照（默认 false），可省一轮模型往返。",
                        },
                        "then_shot": {
                            "type": "boolean",
                            "description": "输入完成后自动附带一张带编号的截图（默认 false），"
                                           "「操作 + 看结果」一次调用完成。",
                        },
                        "then_wait_stable": {
                            "type": "boolean",
                            "description": "输入后先等画面稳定再返回（默认 false），配合 then_shot 使用。",
                        },
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

        try:
            data = await _call("type", {
                "text": text,
                "title": str(args.get("title") or ""),
                "handle": int(args.get("handle") or 0),
                "focusFirst": bool(args.get("focus_first", False)),
                "clearFirst": bool(args.get("clear_first", False)),
                "keys": str(args.get("keys") or ""),
                "requireForeground": bool(getattr(settings, "desktop_require_foreground", True)),
            })
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=await _not_foreground_hint(
                exc, int(args.get("handle") or 0), str(args.get("title") or "")))

        # 输入会改变界面：旧截图不再代表「当前画面」（plan-365-1769 F4）
        desktop_shots.mark_stale(ctx)
        focused = "（已先激活目标窗口）" if data.get("focusedNow") else ""
        output = f"已向「{data.get('target') or '当前窗口'}」输入 {data.get('typed', 0)} 个字符{focused}。"
        keys = str(args.get("keys") or "")
        if keys and not data.get("keysError"):
            output += f"随后按下了 {keys}。"
        if data.get("keysError"):
            output += f"按键失败：{data['keysError']}。"
        output = await _attach_after_action(
            ctx, args, int(args.get("handle") or 0), str(args.get("title") or ""), output, data)
        return ToolResult(ok=True, output=output, data=data)


class DesktopKeysTool(Tool):
    name = "desktop_keys"
    risk_level = "high"
    description = (
        "发送按键或组合键，如 ENTER / TAB / ESC / ctrl+c / alt+F4 / ctrl+shift+s。\n"
        "组合键会作为一次组合发送（修饰键全程按住），符合应用预期。\n"
        "安全：默认要求目标窗口处于前台。\n"
        "若目标窗口不在前台，可给出 title/handle 并设 focus_first=true 先激活再发送。\n"
        "若按键会让界面变化（如回车提交），可设 then_shot=true 让新画面在同一次调用里返回。\n"
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
                        "keys": {"type": "string", "description": "按键或组合键，如 ENTER 或 ctrl+s"},
                        "title": {"type": "string", "description": "目标窗口标题（用于安全校验）"},
                        "handle": {"type": "integer", "description": "目标窗口句柄"},
                        "focus_first": {
                            "type": "boolean",
                            "description": "发送前先激活目标窗口（需同时给出 title 或 handle），默认 false",
                        },
                        "then_snapshot": {
                            "type": "boolean",
                            "description": "发送后自动附带一张新快照（默认 false），可省一轮模型往返。",
                        },
                        "then_shot": {
                            "type": "boolean",
                            "description": "发送后自动附带一张带编号的截图（默认 false），"
                                           "「操作 + 看结果」一次调用完成。",
                        },
                        "then_wait_stable": {
                            "type": "boolean",
                            "description": "发送后先等画面稳定再返回（默认 false），配合 then_shot 使用。",
                        },
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

        try:
            data = await _call("keys", {
                "keys": keys,
                "title": str(args.get("title") or ""),
                "handle": int(args.get("handle") or 0),
                "focusFirst": bool(args.get("focus_first", False)),
                "requireForeground": bool(getattr(settings, "desktop_require_foreground", True)),
            })
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=await _not_foreground_hint(
                exc, int(args.get("handle") or 0), str(args.get("title") or "")))

        # 按键同样可能改变界面（回车提交/关闭等）：旧截图不再代表当前画面（F4）
        desktop_shots.mark_stale(ctx)
        output = f"已发送按键 {keys} 到「{data.get('target') or '当前窗口'}」。"
        output = await _attach_after_action(
            ctx, args, int(args.get("handle") or 0), str(args.get("title") or ""), output, data)
        return ToolResult(ok=True, output=output, data=data)


class DesktopScrollTool(Tool):
    name = "desktop_scroll"
    risk_level = "high"
    description = (
        "在指定位置滚动鼠标滚轮。不填坐标时在屏幕中央滚动。\n"
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
                        "x": {"type": "integer", "description": "横向坐标（可选）"},
                        "y": {"type": "integer", "description": "纵向坐标（可选）"},
                        "clicks": {"type": "integer", "description": "滚动格数，正数向上、负数向下，默认 3"},
                        "horizontal": {"type": "boolean", "description": "是否横向滚动，默认 false"},
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        params: dict[str, Any] = {
            "clicks": int(args.get("clicks") or 3),
            "horizontal": bool(args.get("horizontal", False)),
        }
        if args.get("x") is not None and args.get("y") is not None:
            params["x"] = int(args["x"])
            params["y"] = int(args["y"])

        try:
            data = await _call("scroll", params)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        # 滚动改变画面内容：旧截图不再代表「当前画面」（plan-365-1769 F4）
        desktop_shots.mark_stale(ctx)
        return ToolResult(ok=True, output=f"已滚动（delta={data.get('delta')}）。", data=data)


class DesktopFocusTool(Tool):
    name = "desktop_focus"
    risk_level = "low"
    description = (
        "把指定窗口激活到前台。\n"
        "当 desktop_click/desktop_type 提示「目标窗口不在前台」时，先用本工具激活它。\n"
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


class DesktopFindTextTool(Tool):
    name = "desktop_find_text"
    risk_level = "low"
    description = (
        "在屏幕上查找指定文字，直接返回可点击的屏幕坐标。\n"
        "这是自绘界面（desktop_snapshot 读不到控件）的主力手段：\n"
        "与其「截图→自己估坐标→点击→再截图确认」，直接问文字在哪里更准也更快。\n"
        "返回的 x,y 已是屏幕物理坐标，可直接传给 desktop_click。\n"
        "若未找到，会返回屏幕上实际可见的文字片段：请据此改关键词重试，\n"
        "不要退回到「重新截图再猜坐标」。\n"
        "注意：Chromium 系窗口应优先用 desktop_snapshot（能直接给出元素与坐标）；\n"
        "本工具基于屏幕文字识别，中文结果偏碎、易错。\n"
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
                        "text": {"type": "string", "description": "要查找的文字"},
                        "exact": {"type": "boolean", "description": "是否区分大小写精确匹配，默认 false"},
                        "region": {"type": "string", "description": "限定区域 \"x,y,w,h\"，可显著提速（可选）"},
                        "max_dim": {"type": "integer", "description": "识别前缩放长边像素，默认 1600"},
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

        # 提速（plan-365-1774）：先在最近截图的登记元素里内存匹配（0 成本）；未命中再
        # 把同一张图交给内核识别（OCR 内容哈希缓存命中时毫秒级）；最后才现截现识别。
        # 显式给了 region 时不走复用——那是「换个范围再看」的语义。
        region = str(args.get("region") or "")
        rec = None if region else _reusable_shot(ctx, None, stale_ttl=REUSE_TEXT_STALE_TTL_S)
        data: dict[str, Any] | None = None
        if rec is not None:
            hits = _shot_text_hits(rec, text)
            if hits:
                cands = []
                for h in hits[:8]:
                    center = desktop_marks.element_center(h) or (0, 0)
                    cands.append({
                        "text": desktop_marks.element_name(h) or text,
                        "x": center[0], "y": center[1],
                    })
                data = {
                    "found": True, "count": len(hits),
                    "x": cands[0]["x"], "y": cands[0]["y"], "text": cands[0]["text"],
                    "candidates": cands, "reused": True,
                }
        if data is None:
            params: dict[str, Any] = {
                "text": text,
                "exact": bool(args.get("exact", False)),
                "region": region,
                "maxDim": int(args.get("max_dim") or 1600),
            }
            if rec is not None and rec.meta.get("path"):
                params.update({
                    "image": _abs_image(str(rec.meta["path"])),
                    "scale": float(rec.meta.get("scale") or 1.0),
                    "srcX": int(rec.meta.get("srcX") or 0),
                    "srcY": int(rec.meta.get("srcY") or 0),
                })
            try:
                data = await _call("findtext", params)
            except Exception as exc:  # noqa: BLE001
                return _fail(exc)
            if not data.get("found") and params.get("image"):
                # 复用图未命中：截图之后界面可能出现新文字——按当前画面再找一次
                for key in ("image", "scale", "srcX", "srcY"):
                    params.pop(key, None)
                try:
                    data = await _call("findtext", params)
                except Exception as exc:  # noqa: BLE001
                    return _fail(exc)

        if not data.get("found"):
            visible = str(data.get("visible") or "")
            msg = f"屏幕上没有找到「{text}」。"
            if visible:
                msg += f"\n屏幕上实际可见的文字片段：{visible[:300]}"
                msg += "\n可用上面出现的确切文字重试。"
            return ToolResult(ok=True, output=msg, data=data)

        lines = [f"找到「{data.get('text')}」，坐标为 ({data.get('x')}, {data.get('y')})。"]
        if data.get("reused"):
            lines.append("（复用最近一次截图识别的结果，未重新截屏）")
        cands = data.get("candidates") or []
        if len(cands) > 1:
            lines.append(f"共 {len(cands)} 个匹配，按阅读顺序排列：")
            for c in cands[:5]:
                lines.append(f"  - {c.get('text')}  ({c.get('x')},{c.get('y')})")
        lines.append(
            f"可直接点击：desktop_click text_target=\"{data.get('text') or text}\""
            "　—— 由工具重新定位后点击，比按坐标点更稳。"
        )
        return ToolResult(ok=True, output=_text(lines), data=data)


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
                "\n命中路线时直接照此动手，不必先对整个窗口做快照；"
                "只有当界面与描述不符、或续找元素失败时，再调 desktop_snapshot 观察。"
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
                output=f"已启动「{data.get('title')}」（handle={data.get('handle')}，"
                       f"中心点 {data.get('x')},{data.get('y')}）。" + route,
                data=data,
            )
        return ToolResult(
            ok=True,
            output=f"已启动 {data.get('exe')}，但未在超时内检测到窗口。" + route,
            data=data,
        )

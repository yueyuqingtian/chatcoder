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
import logging
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core import desktop_cdp
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
    """截图默认落点：工作区 `.chatcoder/desktop-shots/`。

    为什么落工作区而不是内核的默认临时目录：截图接下来要做的事几乎总是「让模型看到」。
    落工作区后图片随工具结果直接内联给多模态模型，一次调用即可决策；此前落系统临时目录时，
    模型得先复制文件再调 view_image 才看得到，每次截图链路多付两轮工具往返（plan-334-1661）。
    工作区不可写时回退临时目录，保证工具本身仍可用。
    """
    ext = "png" if fmt.lower() == "png" else "jpg"
    name = f"shot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{ext}"
    root = (ctx.workspace_root or "").strip()
    if root:
        try:
            d = Path(root) / ".chatcoder" / "desktop-shots"
            d.mkdir(parents=True, exist_ok=True)
            return str(d / name)
        except OSError:
            pass
    return str(Path(tempfile.gettempdir()) / name)


# ── 感知通道分流（plan-340-1705 L1.1）───────────────────────────
# CDP 探测失败的 pid 缓存（pid → 失败时刻）。应用未带 --remote-debugging-port 启动时，
# 每次快照都跑一遍端口扫描会白等数百毫秒；缓存后短时间内直接走 UIA。
_CDP_FAILED: dict[int, float] = {}
_CDP_FAIL_TTL_S = 60.0


async def _resolve_window(handle: int, title: str) -> dict | None:
    """按 handle 或 title 取窗口信息（cls / pid / rect），供通道分流判断。"""
    try:
        data = await _call("windows", {"limit": 120})
    except Exception:  # noqa: BLE001
        return None
    items = data.get("items") or []
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
        if win and desktop_cdp.is_chromium_window(str(win.get("cls") or "")):
            cdp = await _try_cdp(win)
            if cdp:
                return _cdp_result(cdp, int(args.get("budget") or 200))

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
        return ToolResult(ok=True, output=_text(lines), data=data)


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
        "默认缩放长边到 1600 像素以减小体积；返回的 scale 可把图片坐标换算回屏幕坐标。\n"
        "截图后若要按可见文字点选，配合 desktop_find_text 使用。\n"
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
                        "max_dim": {"type": "integer", "description": "缩放后长边像素，默认 1600；0 表示不缩放"},
                        "region": {
                            "type": "string",
                            "description": "截图区域 \"x,y,w,h\"（屏幕物理像素），不填则截全屏",
                        },
                        "format": {"type": "string", "description": "jpg 或 png，默认 jpg"},
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

        fmt = str(args.get("format") or "jpg")
        params: dict[str, Any] = {
            "maxDim": int(args.get("max_dim") if args.get("max_dim") is not None else 1600),
            "format": fmt,
            "quality": int(getattr(settings, "desktop_screenshot_quality", 75)),
            "path": str(args.get("out_path") or "").strip() or _default_shot_path(ctx, fmt),
        }
        region = str(args.get("region") or "").strip()
        if region:
            try:
                parts = [int(p.strip()) for p in region.split(",")]
                if len(parts) != 4:
                    raise ValueError
                params.update({"x": parts[0], "y": parts[1], "w": parts[2], "h": parts[3]})
            except ValueError:
                return ToolResult(ok=False, output="", error='region 格式应为 "x,y,w,h"，例如 "0,0,1280,800"')

        try:
            data = await _call("shot", params)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        kb = round((data.get("bytes") or 0) / 1024, 1)
        scale = data.get("scale") or 1.0
        lines = [
            f"截图已保存：{data.get('path')}",
            f"实际尺寸 {data.get('width')}x{data.get('height')}（{kb} KB），"
            f"源区域 {data.get('srcW')}x{data.get('srcH')}，缩放比 {scale:.4f}",
        ]
        if scale and abs(scale - 1.0) > 1e-6:
            lines.append(
                f"坐标换算：屏幕坐标 = 源区域起点 + 图片坐标 / {scale:.4f}。"
                "点击时请用屏幕坐标。"
            )
        # 直接把图片送进对话（多模态模型）：截图的目的几乎总是「让模型看到」，
        # 内联后一次调用即可决策，省掉「复制文件 → view_image」两轮往返（plan-334-1661）。
        if bool(args.get("inline", True)):
            try:
                with open(str(data.get("path")), "rb") as f:
                    data["base64"] = base64.b64encode(f.read()).decode("ascii")
                lines.append("图片已随本次结果送入模型，无需再调用 view_image。")
            except OSError as exc:
                lines.append(f"（图片内联失败，可自行用 view_image 查看：{exc}）")
        else:
            lines.append("可用 view_image 查看该图片。")
        return ToolResult(ok=True, output=_text(lines), data=data)


# ════════════════════════════════════════════════════════════════
# 执行类（写操作，需审批）
# ════════════════════════════════════════════════════════════════


class DesktopClickTool(Tool):
    name = "desktop_click"
    risk_level = "high"
    description = (
        "在屏幕坐标处点击鼠标，可选在同一调用中接着输入文本并按键。\n"
        "把「点击 → 输入 → 回车」合成一次调用能显著提速：真正的延迟来自模型往返，\n"
        "而不是点击本身。若这次点击会改变界面，设 then_snapshot=true，\n"
        "新界面会在同一次调用里一并返回，省掉下一轮往返。\n"
        "安全：默认要求目标窗口处于前台，否则拒绝执行（防止输入打到错误窗口）。\n"
        "若目标窗口不在前台，可给出 title/handle 并设 focus_first=true：\n"
        "本次调用会先激活它再点击，省掉单独一次 desktop_focus。\n"
        "失败时先看错误信息给的修正建议（如补 title 或改 focus_first），\n"
        "不要原样重试同一次调用。\n"
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
                        "x": {"type": "integer", "description": "屏幕横向物理像素坐标"},
                        "y": {"type": "integer", "description": "屏幕纵向物理像素坐标"},
                        "title": {"type": "string", "description": "目标窗口标题（包含匹配），用于安全校验"},
                        "handle": {"type": "integer", "description": "目标窗口句柄"},
                        "button": {"type": "string", "description": "left / right / middle，默认 left"},
                        "count": {"type": "integer", "description": "点击次数，2 表示双击，默认 1"},
                        "focus_first": {"type": "boolean", "description": "点击前先激活目标窗口，默认 false"},
                        "then_snapshot": {
                            "type": "boolean",
                            "description": "动作完成后自动附带一张新快照（默认 false）。开启后"
                                           "「操作 + 看结果」一次调用完成，可省一轮模型往返。",
                        },
                        "text": {"type": "string", "description": "点击后紧接着输入的文本（可选）"},
                        "keys": {"type": "string", "description": "输入后紧接着按下的键，如 ENTER（可选）"},
                        "clear_first": {"type": "boolean", "description": "输入前先全选清空，默认 false"},
                    },
                    "required": ["x", "y"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_plain_ops()
        if err:
            return ToolResult(ok=False, output="", error=err)

        params = {
            "x": int(args.get("x") or 0),
            "y": int(args.get("y") or 0),
            "title": str(args.get("title") or ""),
            "handle": int(args.get("handle") or 0),
            "button": str(args.get("button") or "left"),
            "count": int(args.get("count") or 1),
            "focusFirst": bool(args.get("focus_first", False)),
            "requireForeground": bool(getattr(settings, "desktop_require_foreground", True)),
            "text": str(args.get("text") or ""),
            "keys": str(args.get("keys") or ""),
        }
        try:
            data = await _call("click", params)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        parts = [f"已在 ({data.get('at', [0, 0])[0]},{data.get('at', [0, 0])[1]}) 点击"]
        if data.get("target"):
            parts.append(f"目标窗口「{data['target']}」")
        if data.get("typed"):
            parts.append(f"输入了 {data['typed']} 个字符")
        if data.get("keysError"):
            parts.append(f"按键失败：{data['keysError']}")
        output = "，".join(parts) + "。"
        if bool(args.get("then_snapshot", False)):
            snap = await _snapshot_after_action(
                ctx, handle=int(params["handle"]), title=str(params["title"]))
            if snap:
                output += "\n\n[动作后的界面]\n" + snap
        return ToolResult(ok=True, output=output, data=data)


class DesktopTypeTool(Tool):
    name = "desktop_type"
    risk_level = "high"
    description = (
        "向目标窗口输入文本（支持中文，走 Unicode 输入而非剪贴板）。\n"
        "安全：默认要求目标窗口处于前台，否则拒绝执行——防止输入落到意外的窗口。\n"
        "若目标窗口当前不在前台，请给出 title/handle 并设 focus_first=true：\n"
        "本次调用会先激活它再输入，不必先单独跑一次 desktop_focus。\n"
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
                        "then_snapshot": {
                            "type": "boolean",
                            "description": "输入完成后自动附带一张新快照（默认 false），可省一轮模型往返。",
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
                "requireForeground": bool(getattr(settings, "desktop_require_foreground", True)),
            })
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        focused = "（已先激活目标窗口）" if data.get("focusedNow") else ""
        output = f"已向「{data.get('target') or '当前窗口'}」输入 {data.get('typed', 0)} 个字符{focused}。"
        if bool(args.get("then_snapshot", False)):
            snap = await _snapshot_after_action(
                ctx, handle=int(args.get("handle") or 0), title=str(args.get("title") or ""))
            if snap:
                output += "\n\n[动作后的界面]\n" + snap
        return ToolResult(ok=True, output=output, data=data)


class DesktopKeysTool(Tool):
    name = "desktop_keys"
    risk_level = "high"
    description = (
        "发送按键或组合键，如 ENTER / TAB / ESC / ctrl+c / alt+F4 / ctrl+shift+s。\n"
        "组合键会作为一次组合发送（修饰键全程按住），符合应用预期。\n"
        "安全：默认要求目标窗口处于前台。\n"
        "若目标窗口不在前台，可给出 title/handle 并设 focus_first=true 先激活再发送。\n"
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
            return _fail(exc)

        output = f"已发送按键 {keys} 到「{data.get('target') or '当前窗口'}」。"
        if bool(args.get("then_snapshot", False)):
            snap = await _snapshot_after_action(
                ctx, handle=int(args.get("handle") or 0), title=str(args.get("title") or ""))
            if snap:
                output += "\n\n[动作后的界面]\n" + snap
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
            return ToolResult(ok=True, output="窗口已激活到前台。", data=data)
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

        try:
            data = await _call("findtext", {
                "text": text,
                "exact": bool(args.get("exact", False)),
                "region": str(args.get("region") or ""),
                "maxDim": int(args.get("max_dim") or 1600),
            })
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        if not data.get("found"):
            visible = str(data.get("visible") or "")
            msg = f"屏幕上没有找到「{text}」。"
            if visible:
                msg += f"\n屏幕上实际可见的文字片段：{visible[:300]}"
                msg += "\n可用上面出现的确切文字重试。"
            return ToolResult(ok=True, output=msg, data=data)

        lines = [f"找到「{data.get('text')}」，可点击坐标为 ({data.get('x')}, {data.get('y')})。"]
        cands = data.get("candidates") or []
        if len(cands) > 1:
            lines.append(f"共 {len(cands)} 个匹配，按可信度排列：")
            for c in cands[:5]:
                lines.append(f"  - {c.get('text')}  ({c.get('x')},{c.get('y')})")
        return ToolResult(ok=True, output=_text(lines), data=data)


class DesktopRecipeTool(Tool):
    name = "desktop_recipe"
    risk_level = "low"
    description = (
        "记录或查询「操作路线」——某应用某操作意图的通用原则，用于减少重复推演。\n"
        "action=recall：在操控某个应用之前先查一次。若命中已有路线，按其 principle 执行即可，\n"
        "不必重新摸索界面（这是本工具的主要价值：降低模型思考与推理开销）。\n"
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
            hits = await svc.recall_recipe(ctx.db, app_name=app_name, intent=intent, limit=3)
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
            return await self._start(str(args.get("exe") or ""), str(args.get("keyword") or ""))
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

    async def _start(self, exe: str, keyword: str) -> ToolResult:
        if not exe and not keyword:
            return ToolResult(ok=False, output="", error="需要提供 exe 或 keyword。")
        try:
            data = await _call("startapp", {"exe": exe, "keyword": keyword})
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

        if data.get("title"):
            return ToolResult(
                ok=True,
                output=f"已启动「{data.get('title')}」（handle={data.get('handle')}，"
                       f"中心点 {data.get('x')},{data.get('y')}）。",
                data=data,
            )
        return ToolResult(
            ok=True,
            output=f"已启动 {data.get('exe')}，但未在超时内检测到窗口。",
            data=data,
        )

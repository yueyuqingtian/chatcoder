"""浏览器动作编排（plan-20261015-browser-agent）。

与宿主无关：所有工具都经这里把一次「动作」拆成宿主原语，并生成给模型看的结果文案。

宿主原语（DesktopHost = 前端 browserAgent/host.ts；PlaywrightHost = playwright_host.py，两边必须同步）：

| op | 参数 | 返回（标签类原语都带 tab/url/title/events） |
|---|---|---|
| tabs.list | — | tabs[{id,url,title,loading,active,owner}], active, agent_tab, visible |
| tabs.open | url?, activate? | 新标签 |
| tabs.select / tabs.close | tab | — |
| nav | tab?, url? / action(back·forward·reload), new_tab?, wait, timeout_ms | status? |
| kit | tab?, fn, args, label?, rect? | value（页面工具包返回值） |
| eval | tab?, script | value |
| mouse | tab?, action(click·move·wheel·drag), x, y, button, count, modifiers, dx, dy, to_x, to_y, label, rect, on_dialog, settle_ms | settle |
| keys | tab?, keys[], repeat, label, on_dialog, settle_ms | settle |
| text | tab?, text, label, rect, settle_ms | settle |
| capture | tab?, full_page?, clip?, max_side, quality, label | base64, mime, width, height, css_width, css_height |
| logs | tab?, kind(console·network), limit, only_errors, pattern, clear | entries, total |
| net_body | tab?, request_id | body, base64, mime |
| viz | tab?, label, rect? | — |
| clipboard | action(read·write), text? | text |

坐标一律为页面 CSS 像素；模型给的坐标若来自截图，按最近一次视口截图的缩放比换算。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.config import settings
from app.orchestration.browser.bridge import HostCallError, HostUnavailable
from app.orchestration.browser.host import BrowserHost, acquire_host
from app.orchestration.browser.keys import KeyParseError, describe, normalize_keys

logger = logging.getLogger(__name__)

_T_NAV = 45.0
_T_INPUT = 25.0
_T_KIT = 20.0
_T_CAPTURE = 25.0
_T_DEFAULT = 15.0


@dataclass
class Outcome:
    ok: bool
    text: str = ""
    error: str = ""
    data: dict = field(default_factory=dict)
    image_b64: str | None = None
    image_mime: str = "image/jpeg"


class ActionError(Exception):
    """动作语义失败（给模型看的中文说明）。"""


@dataclass
class _SessionState:
    host_kind: str = ""
    tab: str | None = None
    shot_scale: dict[str, float] = field(default_factory=dict)
    clipboard: str = ""
    urls: dict[str, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_states: dict[int, _SessionState] = {}


def _state(session_id: int) -> _SessionState:
    st = _states.get(session_id)
    if st is None:
        st = _SessionState()
        _states[session_id] = st
    return st


# ── 本地页面判定（审批：本地开发服务器与本机文件免确认）──────────
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


def is_local_url(url: str | None) -> bool:
    if not url:
        return False
    u = url.strip().lower()
    if u.startswith("file:") or u.startswith("about:") or u.startswith("data:"):
        return True
    try:
        host = (urlparse(u if "://" in u else "http://" + u).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS or host.endswith(".localhost") or host.endswith(".test")


def current_url(session_id: int) -> str:
    st = _states.get(session_id)
    if not st or not st.tab:
        return ""
    return st.urls.get(st.tab, "")


def normalize_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u:
        raise ActionError("url 不能为空")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", u) and not re.match(r"^[\w.-]+:\d+", u):
        return u
    host = u.split("/")[0].split(":")[0].lower()
    if host in _LOCAL_HOSTS or host.endswith(".localhost") or re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return "http://" + u
    return "https://" + u


# ── 文案工具 ─────────────────────────────────────────────
def _clip(s: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _page_line(resp: dict) -> str:
    title = _clip(resp.get("title") or "(无标题)", 60)
    return f"当前页面：{title} — {resp.get('url') or ''}"


def _events_lines(resp: dict) -> list[str]:
    out = []
    for ev in resp.get("events") or []:
        t = ev.get("type")
        if t == "dialog":
            kind = {"alert": "提示框", "confirm": "确认框", "prompt": "输入框", "beforeunload": "离开确认"}.get(ev.get("dialog_type"), "对话框")
            act = "已确认" if ev.get("accepted") else "已取消"
            out.append(f"页面弹出{kind}「{_clip(ev.get('message'), 80)}」，{act}")
        elif t == "popup":
            out.append(f"页面打开了新窗口，已作为新标签 {ev.get('tab')}：{ev.get('url') or ''}")
        elif t == "download":
            out.append(f"触发下载：{ev.get('filename') or ev.get('url') or ''}")
        elif t == "crash":
            out.append("页面渲染进程崩溃，已重新加载")
        elif t == "nav_error":
            out.append(f"页面加载失败：{ev.get('message') or ''}")
    return out


def _settle_lines(resp: dict, before_url: str = "") -> list[str]:
    st = resp.get("settle") or {}
    lines = []
    url = st.get("url") or resp.get("url") or ""
    if st.get("navigated") or (before_url and url and url != before_url):
        lines.append(f"页面已跳转：{_clip(st.get('title') or resp.get('title') or '', 60)} — {url}")
    elif st.get("mutations"):
        lines.append(f"页面内容有变化（{st.get('mutations')} 处 DOM 更新）")
    else:
        lines.append("页面没有可见变化")
    if st.get("dialog"):
        lines.append(f"页面当前有打开的对话框 {st.get('dialog')}")
    if st.get("focus"):
        lines.append(f"焦点在 {st.get('focus')}")
    return lines + _events_lines(resp)


def _target_args(args: dict) -> dict:
    out = {}
    for k in ("ref", "selector", "text"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    if "ref" in out and not out["ref"].startswith("ref_") and out["ref"].isdigit():
        out["ref"] = "ref_" + out["ref"]
    return out


class BrowserDriver:
    """会话级浏览器动作；同一会话内串行（多个子代理并发调用时不互相踩）。"""

    def __init__(self, session_id: int, cancel_event: asyncio.Event | None = None) -> None:
        self.sid = session_id
        self.cancel_event = cancel_event
        self.st = _state(session_id)
        self.host: BrowserHost | None = None

    # ── 宿主调用 ─────────────────────────────────────────
    async def _host(self) -> BrowserHost:
        if self.host is None:
            self.host = await acquire_host()
            if self.st.host_kind != self.host.kind:
                # 宿主切换（桌面端上线/下线）：标签编号体系不同，缓存全部作废
                self.st.host_kind = self.host.kind
                self.st.tab = None
                self.st.shot_scale.clear()
                self.st.urls.clear()
        return self.host

    async def call(self, op: str, args: dict | None = None, timeout: float = _T_DEFAULT) -> dict:
        host = await self._host()
        a = dict(args or {})
        # 显式 tab（模型指定）找不到要报错；缓存的 AI 标签只是“偏好”——用户关掉了它时，
        # 宿主回退到会话里 AI 当前的标签或新开一个，而不是让动作失败
        if "tab" not in a and self.st.tab and not op.startswith("tabs."):
            a["prefer_tab"] = self.st.tab
        try:
            resp = await host.call(self.sid, op, a, timeout, cancel_event=self.cancel_event)
        except HostUnavailable:
            if host.kind == "desktop":
                # 桌面端在请求途中断开：回退兜底宿主重试一次（标签体系不同，不带 tab）
                self.host = None
                self.st.host_kind = ""
                host = await self._host()
                a.pop("tab", None)
                a.pop("prefer_tab", None)
                resp = await host.call(self.sid, op, a, timeout, cancel_event=self.cancel_event)
            else:
                raise
        if isinstance(resp, dict):
            tab = resp.get("tab")
            if tab and not op.startswith("tabs.list"):
                self.st.tab = str(tab)
                if resp.get("url"):
                    self.st.urls[str(tab)] = str(resp.get("url"))
        return resp if isinstance(resp, dict) else {}

    async def kit(self, fn: str, args: dict | None = None, *, label: str = "", rect: dict | None = None,
                  timeout: float = _T_KIT) -> Any:
        payload: dict = {"fn": fn, "args": args or {}}
        if label:
            payload["label"] = label
        if rect:
            payload["rect"] = rect
        resp = await self.call("kit", payload, timeout=timeout)
        return resp.get("value")

    def _to_css(self, x: Any, y: Any) -> tuple[float, float]:
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError) as e:
            raise ActionError("x / y 必须是数字") from e
        scale = self.st.shot_scale.get(self.st.tab or "", 1.0) or 1.0
        return round(fx / scale, 1), round(fy / scale, 1)

    # ── 动作 ─────────────────────────────────────────────
    async def navigate(self, args: dict) -> Outcome:
        action = str(args.get("action") or "").strip().lower()
        payload: dict = {"wait": args.get("wait_until") or "load", "timeout_ms": 30000}
        if args.get("tab_id"):
            payload["tab"] = str(args["tab_id"])
        if action in ("back", "forward", "reload"):
            payload["action"] = action
            payload["label"] = {"back": "后退", "forward": "前进", "reload": "刷新页面"}[action]
        else:
            url = normalize_url(str(args.get("url") or ""))
            payload["url"] = url
            payload["label"] = f"打开 {_clip(url, 60)}"
            if args.get("new_tab"):
                payload["new_tab"] = True
        resp = await self.call("nav", payload, timeout=_T_NAV)
        lines = [f"已打开：{_clip(resp.get('title') or '(无标题)', 60)} — {resp.get('url') or ''}"
                 + (f"（HTTP {resp['status']}）" if isinstance(resp.get("status"), int) and resp["status"] >= 400 else "")]
        lines.append(f"标签：{resp.get('tab')}")
        try:
            digest = await self.kit("text", {"max_chars": 600})
            preview = (digest or {}).get("text") or ""
            if preview:
                lines.append("页面摘要：" + _clip(preview, 500))
        except Exception:  # noqa: BLE001
            pass
        lines += _events_lines(resp)
        lines.append("下一步：browser_snapshot 获取可交互元素（含 ref），或 browser_get_text 读正文。")
        return Outcome(True, "\n".join(lines), data={"url": resp.get("url"), "title": resp.get("title"), "tab": resp.get("tab")})

    async def tabs(self, args: dict) -> Outcome:
        action = str(args.get("action") or "list").lower()
        tab_id = str(args.get("tab_id") or "").strip() or None
        if action == "new":
            payload: dict = {"activate": True, "label": "新建标签"}
            if args.get("url"):
                payload["url"] = normalize_url(str(args["url"]))
            resp = await self.call("tabs.open", payload, timeout=_T_NAV)
            self.st.tab = resp.get("tab") or self.st.tab
            return Outcome(True, f"已新建标签 {resp.get('tab')}。{_page_line(resp)}", data=resp)
        if action == "select":
            if not tab_id:
                raise ActionError("select 需要 tab_id")
            resp = await self.call("tabs.select", {"tab": tab_id})
            self.st.tab = tab_id
            return Outcome(True, f"已切换到标签 {tab_id}。{_page_line(resp)}", data=resp)
        if action == "close":
            target = tab_id or self.st.tab
            if not target:
                raise ActionError("close 需要 tab_id")
            await self.call("tabs.close", {"tab": target})
            if self.st.tab == target:
                self.st.tab = None
            return Outcome(True, f"已关闭标签 {target}")
        resp = await self.call("tabs.list", {})
        tabs = resp.get("tabs") or []
        if not tabs:
            return Outcome(True, "当前没有打开的标签。用 browser_navigate 打开网页。", data=resp)
        lines = []
        for t in tabs:
            flags = []
            if t.get("id") == (resp.get("agent_tab") or self.st.tab):
                flags.append("AI 当前操作")
            if t.get("active"):
                flags.append("面板显示中")
            if t.get("owner") == "user":
                flags.append("用户标签")
            if t.get("loading"):
                flags.append("加载中")
            lines.append(f"- {t.get('id')}：{_clip(t.get('title') or '(无标题)', 50)} — {t.get('url') or '(空白页)'}"
                         + (f"（{'，'.join(flags)}）" if flags else ""))
        head = "浏览器标签（右侧面板" + ("当前可见" if resp.get("visible") else "当前未显示，AI 仍可在后台操作") + "）："
        if self.host and self.host.kind != "desktop":
            head = "浏览器标签（后台浏览器，用户界面不可见）："
        return Outcome(True, head + "\n" + "\n".join(lines), data=resp)

    async def snapshot(self, args: dict) -> Outcome:
        a = {"filter": args.get("filter") or "interactive", "max_chars": args.get("max_chars") or 12000}
        if args.get("ref"):
            a["ref"] = str(args["ref"])
        if args.get("query"):
            a["query"] = str(args["query"])
        if args.get("viewport_only"):
            a["viewport_only"] = True
        if args.get("tab_id"):
            await self.call("tabs.select", {"tab": str(args["tab_id"])})
        v = await self.kit("snapshot", a, label="读取页面结构")
        text = (v or {}).get("text") or "（页面为空）"
        return Outcome(True, text, data={"url": (v or {}).get("url"), "title": (v or {}).get("title"),
                                          "count": (v or {}).get("count"), "truncated": (v or {}).get("truncated")})

    async def get_text(self, args: dict) -> Outcome:
        a: dict = {"max_chars": args.get("max_chars") or 8000}
        a.update(_target_args(args))
        v = await self.kit("text", a, label="读取页面正文")
        v = v or {}
        head = f"{v.get('label') or '页面'}正文（{v.get('total', 0)} 字）— {v.get('url') or ''}"
        body = v.get("text") or "（没有可读文本）"
        if v.get("cross_frames"):
            body += f"\n（页面含 {v['cross_frames']} 个跨域框架，其内容不在正文中：请截图查看，再用坐标点击或输入）"
        return Outcome(True, head + "\n\n" + body, data={"url": v.get("url"), "title": v.get("title")})

    async def screenshot(self, args: dict) -> Outcome:
        if args.get("url"):
            await self.navigate({"url": args["url"]})
        payload: dict = {"max_side": 1280, "quality": 72, "label": "截图"}
        target = _target_args(args)
        viewport_shot = True
        if target:
            info = await self.kit("resolve", target, label="定位截图区域")
            r = info.get("rect") or {}
            pad = 8
            payload["clip"] = {"x": max(0, r.get("x", 0) - pad), "y": max(0, r.get("y", 0) - pad),
                               "width": max(1, r.get("width", 1) + pad * 2), "height": max(1, r.get("height", 1) + pad * 2)}
            payload["label"] = f"截图 {info.get('label') or ''}"
            viewport_shot = False
        elif args.get("full_page"):
            payload["full_page"] = True
            payload["label"] = "整页截图"
            viewport_shot = False
        resp = await self.call("capture", payload, timeout=_T_CAPTURE)
        b64 = resp.get("base64") or ""
        if not b64:
            raise ActionError("截图失败：没有拿到图像数据")
        w, h = int(resp.get("width") or 0), int(resp.get("height") or 0)
        css_w = float(resp.get("css_width") or w or 1)
        tab = str(resp.get("tab") or self.st.tab or "")
        if viewport_shot and css_w:
            self.st.shot_scale[tab] = (w / css_w) if w else 1.0
        rel, abs_path = _save_shot(b64, resp.get("mime") or "image/jpeg")
        lines = [f"截图完成：{_clip(resp.get('title') or '', 60)} — {resp.get('url') or ''}",
                 f"图片 {w}×{h}" + ("（视口截图：图上像素坐标可直接用于 browser_click 的 x/y）" if viewport_shot
                                   else "（非视口截图，坐标不能直接用于点击）")]
        if abs_path:
            lines.append(f"已保存: path=`{abs_path}`（模型不支持看图时可用 view_image 读取）")
        lines += _events_lines(resp)
        return Outcome(True, "\n".join(lines), image_b64=b64, image_mime=resp.get("mime") or "image/jpeg",
                       data={"url": resp.get("url"), "title": resp.get("title"), "screenshot_path": rel,
                             "abs_path": abs_path, "width": w, "height": h, "tab": tab})

    async def _resolve(self, args: dict, label_prefix: str) -> dict:
        target = _target_args(args)
        if not target:
            if args.get("x") is not None and args.get("y") is not None:
                x, y = self._to_css(args.get("x"), args.get("y"))
                info = await self.kit("resolve", {"x": x, "y": y}, label=f"{label_prefix}坐标 ({int(x)}, {int(y)})")
                info["x"], info["y"] = x, y
                return info
            raise ActionError("需要指定目标：ref（来自 browser_snapshot）、selector、text 或 x/y 坐标")
        info = await self.kit("resolve", target, label=f"定位{label_prefix}目标")
        if info.get("offscreen"):
            raise ActionError(f"{info.get('label')} 不在可视区域内且无法滚入，请先滚动或换一个目标")
        return info

    async def click(self, args: dict) -> Outcome:
        info = await self._resolve(args, "点击")
        covered = info.get("covered_by")
        if covered and not args.get("force"):
            raise ActionError(f"{info.get('label')} 被 {covered} 遮挡（可能是弹窗、浮层或框架），"
                              f"请先关闭遮挡，或确认无误后传 force=true")
        if info.get("disabled") and not args.get("force"):
            raise ActionError(f"{info.get('label')} 处于禁用状态，点击不会生效")
        count = max(1, min(int(args.get("click_count") or (2 if args.get("double") else 1)), 3))
        button = str(args.get("button") or "left")
        verb = {1: "点击", 2: "双击", 3: "三击"}[count] if button == "left" else ("右键点击" if button == "right" else "中键点击")
        mods = _modifiers(args.get("modifiers"))
        before = self.st.urls.get(self.st.tab or "", "")
        if covered and info.get("ref"):
            # 目标被遮挡且已确认（force）：坐标会落在遮挡物上，改为直接在元素上派发点击
            if count != 1 or button != "left" or mods:
                raise ActionError(f"{info.get('label')} 被 {covered} 遮挡，强制点击只支持左键单击（去掉双击、右键或修饰键）")
            resp = await self.call("mouse", {
                "action": "activate", "ref": info["ref"], "x": info["x"], "y": info["y"],
                "label": f"点击{info.get('label') or ''}", "rect": info.get("rect"), "settle_ms": 1500,
            }, timeout=_T_INPUT)
            lines = [f"已点击 {info.get('label') or ''}（{info['ref']}）：{covered} 遮挡了它，已直接在元素上触发点击"] \
                + _settle_lines(resp, before)
            return Outcome(True, "\n".join(lines), data={"url": resp.get("url"), "title": resp.get("title")})
        resp = await self.call("mouse", {
            "action": "click", "x": info["x"], "y": info["y"], "button": button, "count": count, "modifiers": mods,
            "label": f"{verb}{info.get('label') or ''}", "rect": info.get("rect"),
            "on_dialog": args.get("on_dialog") or "accept", "prompt_text": args.get("prompt_text") or "",
            "settle_ms": 1500,
        }, timeout=_T_INPUT)
        ref = f"（{info['ref']}）" if info.get("ref") else ""
        lines = [f"已{verb} {info.get('label') or ''}{ref}"] + _settle_lines(resp, before)
        return Outcome(True, "\n".join(lines), data={"url": resp.get("url"), "title": resp.get("title")})

    async def hover(self, args: dict) -> Outcome:
        info = await self._resolve(args, "悬停")
        resp = await self.call("mouse", {"action": "move", "x": info["x"], "y": info["y"],
                                         "label": f"悬停在{info.get('label') or ''}", "rect": info.get("rect"),
                                         "settle_ms": 500}, timeout=_T_INPUT)
        return Outcome(True, "\n".join([f"已悬停在 {info.get('label') or ''}"] + _settle_lines(resp)))

    async def type_text(self, args: dict) -> Outcome:
        text = args.get("text")
        if text is None:
            raise ActionError("text 不能为空")
        text = str(text)
        target = _target_args(args)
        clear = args.get("clear", args.get("clear_before", True))
        clear = bool(clear) if not isinstance(clear, str) else clear.lower() not in ("false", "0", "no")
        info = await self.kit("focus", {**target, "clear": clear}, label="聚焦输入框")
        label = info.get("label") or "输入框"
        before = self.st.urls.get(self.st.tab or "", "")
        submit = bool(args.get("submit") or args.get("press_enter"))
        resp = await self.call("text", {"text": text, "label": f"在{label}输入", "rect": info.get("rect"),
                                        "settle_ms": 0 if submit else 600}, timeout=_T_INPUT)
        lines = [f"已在 {label} 输入 {len(text)} 个字符" + ("（先清空了原内容）" if clear else "")]
        if submit:
            resp = await self.call("keys", {"keys": ["Enter"], "label": "按下 Enter 提交", "settle_ms": 2000,
                                            "on_dialog": args.get("on_dialog") or "accept"}, timeout=_T_INPUT)
            lines.append("已按 Enter 提交")
            lines += _settle_lines(resp, before)
        else:
            lines += _events_lines(resp)
        return Outcome(True, "\n".join(lines), data={"url": resp.get("url")})

    async def press(self, args: dict) -> Outcome:
        try:
            combos = normalize_keys(args.get("keys") or args.get("key"))
        except KeyParseError as e:
            raise ActionError(str(e)) from e
        repeat = max(1, min(int(args.get("repeat") or 1), 50))
        if args.get("ref") or args.get("selector") or args.get("text"):
            await self.kit("focus", _target_args(args), label="聚焦目标")
        shown = " ".join(describe(c) for c in combos) + (f" ×{repeat}" if repeat > 1 else "")
        before = self.st.urls.get(self.st.tab or "", "")
        resp = await self.call("keys", {"keys": combos, "repeat": repeat, "label": f"按键 {shown}",
                                        "on_dialog": args.get("on_dialog") or "accept", "settle_ms": 1200},
                               timeout=_T_INPUT)
        return Outcome(True, "\n".join([f"已按下 {shown}"] + _settle_lines(resp, before)))

    async def scroll(self, args: dict) -> Outcome:
        to = str(args.get("to") or "").lower()
        target = _target_args(args)
        direction = str(args.get("direction") or "").lower()
        if to in ("top", "bottom"):
            st = await self.kit("scrollTo", {"to": to}, label="滚动到" + ("顶部" if to == "top" else "底部"))
            return Outcome(True, f"已滚动到{'顶部' if to == 'top' else '底部'}（{st.get('scroll', {}).get('y', 0)}/{st.get('scroll', {}).get('max_y', 0)}）")
        if target and not direction:
            st = await self.kit("scrollTo", target, label="滚动到目标")
            return Outcome(True, f"已把 {st.get('label')} 滚入视口中央")
        if direction not in ("up", "down", "left", "right"):
            direction = "down"
        amount = args.get("amount")
        st0 = await self.kit("state", {})
        vp = (st0 or {}).get("viewport") or {"width": 1280, "height": 800}
        try:
            px = float(amount) if amount is not None else 0.0
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            px = vp["height"] * 0.8 if direction in ("up", "down") else vp["width"] * 0.8
        elif px <= 10:  # 小数字按「屏」理解
            px = px * (vp["height"] if direction in ("up", "down") else vp["width"])
        if target:
            info = await self.kit("resolve", target, label="定位滚动区域")
            x, y = info["x"], info["y"]
        elif args.get("x") is not None and args.get("y") is not None:
            x, y = self._to_css(args.get("x"), args.get("y"))
        else:
            x, y = vp["width"] / 2, vp["height"] / 2
        before = await self.kit("scrollPos", {"x": x, "y": y})
        dx = px if direction == "right" else (-px if direction == "left" else 0)
        dy = px if direction == "down" else (-px if direction == "up" else 0)
        zh = {"up": "向上", "down": "向下", "left": "向左", "right": "向右"}[direction]
        await self.call("mouse", {"action": "wheel", "x": x, "y": y, "dx": dx, "dy": dy,
                                  "label": f"{zh}滚动", "settle_ms": 350}, timeout=_T_INPUT)
        after = await self.kit("scrollPos", {"x": x, "y": y})
        moved = abs((after or {}).get("top", 0) - (before or {}).get("top", 0)) + abs((after or {}).get("left", 0) - (before or {}).get("left", 0))
        where = f"{(after or {}).get('label') or '页面'} 位置 {(after or {}).get('top', 0)}/{(after or {}).get('max', 0)}"
        if moved < 2:
            return Outcome(True, f"{zh}滚动没有移动（{where}，可能已到边界或该区域不可滚动）")
        return Outcome(True, f"已{zh}滚动 {int(moved)}px（{where}）")

    async def drag(self, args: dict) -> Outcome:
        src = await self._resolve({k[5:]: v for k, v in args.items() if k.startswith("from_")} or args, "拖拽")
        to_args = {k[3:]: v for k, v in args.items() if k.startswith("to_") and k != "to_"}
        if to_args.get("ref") or to_args.get("selector") or to_args.get("text"):
            dst = await self.kit("resolve", _target_args(to_args), label="定位放置目标")
            tx, ty, dlabel = dst["x"], dst["y"], dst.get("label")
        elif to_args.get("x") is not None and to_args.get("y") is not None:
            tx, ty = self._to_css(to_args.get("x"), to_args.get("y"))
            dlabel = f"坐标 ({int(tx)}, {int(ty)})"
        else:
            raise ActionError("需要放置目标：to_ref / to_selector / to_text 或 to_x + to_y")
        before = self.st.urls.get(self.st.tab or "", "")
        resp = await self.call("mouse", {"action": "drag", "x": src["x"], "y": src["y"], "to_x": tx, "to_y": ty,
                                         "steps": 14, "label": f"拖拽{src.get('label') or ''}", "rect": src.get("rect"),
                                         "settle_ms": 800}, timeout=_T_INPUT)
        return Outcome(True, "\n".join([f"已把 {src.get('label')} 拖到 {dlabel}"] + _settle_lines(resp, before)))

    async def fill(self, args: dict) -> Outcome:
        fields = args.get("fields")
        if not isinstance(fields, list) or not fields:
            fields = [{k: args.get(k) for k in ("ref", "selector", "text", "value") if args.get(k) is not None}]
        lines = []
        for f in fields[:30]:
            if not isinstance(f, dict) or "value" not in f:
                raise ActionError("每一项都需要 value（以及 ref / selector / text 之一）")
            target = _target_args(f)
            if not target:
                raise ActionError("每一项都需要 ref / selector / text 之一")
            v = await self.kit("fill", {**target, "value": f.get("value")}, label="填写表单")
            lines.append(f"已设置 {v.get('label')} = {v.get('value')}")
        return Outcome(True, "\n".join(lines))

    async def clipboard(self, args: dict) -> Outcome:
        action = str(args.get("action") or "copy").lower()
        system = bool(args.get("system"))
        target = _target_args(args)
        if action == "select_all":
            v = await self.kit("selectAll", target, label="全选")
            return Outcome(True, f"已全选 {v.get('label')}（{len(v.get('text') or '')} 字）")
        if action in ("copy", "cut"):
            if target:
                await self.kit("selectAll", target, label="选中目标")
            v = await self.kit("cut" if action == "cut" else "selection", {}, label="剪切" if action == "cut" else "复制")
            text = (v or {}).get("text") or ""
            if not text:
                raise ActionError("当前没有选中的内容：先用 select_all 或指定 ref 选中要复制的内容")
            self.st.clipboard = text
            if system:
                await self.call("clipboard", {"action": "write", "text": text})
            verb = "剪切" if action == "cut" else "复制"
            where = "系统剪贴板与 AI 剪贴板" if system else "AI 剪贴板（不影响用户的系统剪贴板）"
            return Outcome(True, f"已{verb} {len(text)} 字到{where}：「{_clip(text, 200)}」", data={"text": text[:20000]})
        if action == "paste":
            text = args.get("text")
            if text is None:
                if system:
                    text = (await self.call("clipboard", {"action": "read"})).get("text") or ""
                else:
                    text = self.st.clipboard
            text = str(text or "")
            if not text:
                raise ActionError("剪贴板为空：先 copy，或用 text 参数指定要粘贴的内容")
            info = await self.kit("focus", target, label="聚焦粘贴位置") if target else None
            v = await self.kit("paste", {"text": text}, label="粘贴")
            label = (info or {}).get("label") or v.get("label") or "当前焦点"
            if not v.get("handled"):
                if not v.get("editable") and not info:
                    raise ActionError(f"焦点在 {label}，不是可输入区域：请用 ref / selector 指定粘贴目标")
                await self.call("text", {"text": text, "label": f"粘贴到{label}", "rect": (info or {}).get("rect"),
                                         "settle_ms": 500}, timeout=_T_INPUT)
            return Outcome(True, f"已粘贴 {len(text)} 字到 {label}")
        raise ActionError("action 只能是 copy / cut / paste / select_all")

    async def wait(self, args: dict) -> Outcome:
        if args.get("ms") is not None and not (args.get("text") or args.get("selector") or args.get("ref")):
            ms = max(0, min(int(args.get("ms") or 0), 30000))
            await asyncio.sleep(ms / 1000)
            return Outcome(True, f"已等待 {ms} ms")
        a: dict = {"timeout_ms": int(args.get("timeout_ms") or 10000)}
        a.update(_target_args(args))
        if args.get("gone"):
            a["gone"] = True
        what = a.get("text") or a.get("selector") or a.get("ref") or "页面加载完成"
        v = await self.kit("wait", a, label=f"等待 {_clip(what, 30)}", timeout=a["timeout_ms"] / 1000 + 8)
        if not (v or {}).get("met"):
            return Outcome(False, error=f"等待超时（{v.get('waited')} ms）：{what} {'仍未消失' if a.get('gone') else '没有出现'}")
        return Outcome(True, f"条件已满足（{v.get('waited')} ms）：{what}{' 已消失' if a.get('gone') else ''}")

    async def evaluate(self, args: dict) -> Outcome:
        script = str(args.get("script") or "").strip()
        if not script:
            raise ActionError("script 不能为空")
        resp = await self.call("eval", {"script": script, "label": "执行页面脚本"}, timeout=_T_KIT)
        value = resp.get("value")
        try:
            shown = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            shown = str(value)
        if len(shown) > 8000:
            shown = shown[:8000] + "…（已截断）"
        return Outcome(True, "执行结果：\n" + shown, data={"result": value if len(shown) <= 8000 else None})

    async def logs(self, args: dict) -> Outcome:
        if args.get("request_id"):
            v = await self.call("net_body", {"request_id": str(args["request_id"])})
            body = v.get("body") or ""
            if v.get("base64"):
                return Outcome(True, f"响应体为二进制（{v.get('mime')}），base64 长度 {len(body)}")
            if len(body) > 20000:
                body = body[:20000] + "\n…（已截断）"
            return Outcome(True, f"响应体（{v.get('mime') or ''}）：\n{body}")
        kind = "network" if str(args.get("kind") or "").lower() == "network" else "console"
        v = await self.call("logs", {"kind": kind, "limit": args.get("limit") or 50, "only_errors": bool(args.get("only_errors")),
                                     "pattern": args.get("pattern") or "", "clear": bool(args.get("clear"))})
        entries = v.get("entries") or []
        if not entries:
            return Outcome(True, "没有" + ("网络请求" if kind == "network" else "控制台消息") + "记录" + ("（已按条件过滤）" if args.get("pattern") or args.get("only_errors") else ""))
        lines = [f"{'网络请求' if kind == 'network' else '控制台消息'}（共 {v.get('total')} 条，显示最近 {len(entries)} 条）："]
        for e in entries:
            if kind == "network":
                st = e.get("status")
                lines.append(f"- [{e.get('id')}] {e.get('method')} {st if st is not None else '…'} {_clip(e.get('url'), 160)}"
                             + (f" ({e.get('type')})" if e.get("type") else "") + (f" 错误: {e.get('error')}" if e.get("error") else ""))
            else:
                src = e.get("source") or ""
                lines.append(f"- [{e.get('level')}] {_clip(e.get('text'), 400)}" + (f"  ({_clip(src, 80)})" if src else ""))
        if kind == "network":
            lines.append("（需要响应内容时传 request_id）")
        return Outcome(True, "\n".join(lines))


# ── 批量 ─────────────────────────────────────────────────
_BATCH_ACTIONS = {
    "navigate": "navigate", "open": "navigate", "click": "click", "type": "type_text", "fill": "fill",
    "press": "press", "key": "press", "scroll": "scroll", "hover": "hover", "drag": "drag", "wait": "wait",
    "clipboard": "clipboard", "screenshot": "screenshot", "snapshot": "snapshot", "get_text": "get_text",
    "evaluate": "evaluate", "tabs": "tabs",
}


async def run_batch(driver: BrowserDriver, args: dict) -> Outcome:
    steps = args.get("actions") or args.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ActionError("actions 必须是非空数组，如 [{\"action\":\"click\",\"ref\":\"ref_3\"}]")
    if len(steps) > 40:
        raise ActionError("一次最多 40 步")
    lines: list[str] = []
    image: Outcome | None = None
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            return Outcome(False, "\n".join(lines), error=f"第 {i} 步格式错误：需要对象")
        name = str(step.get("action") or step.get("tool") or "").replace("browser_", "").lower()
        method = _BATCH_ACTIONS.get(name)
        if not method:
            return Outcome(False, "\n".join(lines), error=f"第 {i} 步动作「{name}」不支持；可用：{', '.join(sorted(_BATCH_ACTIONS))}")
        params = step.get("args") if isinstance(step.get("args"), dict) else {k: v for k, v in step.items() if k not in ("action", "tool")}
        try:
            out = await getattr(driver, method)(params)
        except (ActionError, HostCallError) as e:
            lines.append(f"{i}. {name} 失败：{e}")
            return Outcome(False, "\n".join(lines), error=f"第 {i} 步（{name}）失败：{e}；前 {i - 1} 步已执行，后续步骤未执行")
        if not out.ok:
            lines.append(f"{i}. {name} 失败：{out.error}")
            return Outcome(False, "\n".join(lines), error=f"第 {i} 步（{name}）失败：{out.error}")
        body = out.text if name in ("snapshot", "get_text", "evaluate") else out.text.split("\n")[0]
        lines.append(f"{i}. {body}")
        if out.image_b64:
            image = out
    then = str(args.get("then") or "").lower()
    if then == "screenshot":
        image = await driver.screenshot({})
        lines.append("末尾截图：" + image.text.split("\n")[0])
    elif then == "snapshot":
        snap = await driver.snapshot({})
        lines.append("末尾页面结构：\n" + snap.text)
    out = Outcome(True, f"批量执行完成（{len(steps)} 步）：\n" + "\n".join(lines))
    if image is not None:
        out.image_b64, out.image_mime, out.data = image.image_b64, image.image_mime, image.data
    return out


def _modifiers(raw) -> list[str]:
    if not raw:
        return []
    items = raw if isinstance(raw, list) else re.split(r"[+,\s]+", str(raw))
    out = []
    for m in items:
        k = str(m).strip().lower()
        v = {"ctrl": "Control", "control": "Control", "shift": "Shift", "alt": "Alt", "meta": "Meta",
             "cmd": "Meta", "command": "Meta", "win": "Meta"}.get(k)
        if v and v not in out:
            out.append(v)
    return out


def _save_shot(b64: str, mime: str) -> tuple[str, str]:
    """截图落盘到 uploads/browser-shots：聊天里的工具卡片展示缩略图，非多模态模型可用 view_image 读取。"""
    import base64

    try:
        root = Path(settings.uploads_dir).resolve()
        shot_dir = root / "browser-shots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        ext = "png" if "png" in mime else "jpg"
        name = f"shot_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}"
        path = shot_dir / name
        path.write_bytes(base64.b64decode(b64))
        return f"browser-shots/{name}", str(path).replace("\\", "/")
    except Exception as e:  # noqa: BLE001
        logger.warning("[browser] 截图落盘失败: %s", e)
        return "", ""


async def execute(session_id: int, action: str, args: dict, cancel_event: asyncio.Event | None = None) -> Outcome:
    """工具入口：会话内串行执行一个动作（批量动作整体持锁）。"""
    driver = BrowserDriver(session_id, cancel_event)
    st = driver.st
    async with st.lock:
        try:
            if action == "batch":
                return await run_batch(driver, args)
            return await getattr(driver, action)(args)
        except ActionError as e:
            return Outcome(False, error=str(e))
        except HostCallError as e:
            return Outcome(False, error=str(e))
        except HostUnavailable as e:
            return Outcome(False, error=f"浏览器不可用：{e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("[browser] 动作 %s 异常", action)
            return Outcome(False, error=f"浏览器操作异常：{e}")

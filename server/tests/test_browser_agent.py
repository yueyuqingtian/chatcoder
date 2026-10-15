"""AI 驱动内置浏览器（plan-20261015-browser-agent）单测。

覆盖最容易回归的环节：
1. 按键与网址规范化、本地页面判定；
2. 审批分类：读页面放行、外部导航按网络、写操作按页面是否本地、批量取最严、只读/计划模式拒绝写；
3. 宿主桥：桌面端登记 → 请求/响应配对 → 断线让在途请求立即失败；
4. 动作编排：ref 定位后点击、截图坐标换算、输入与提交、AI 剪贴板、批量失败即停；
5. 功能开关关闭时注册表不下发浏览器工具。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import settings
from app.orchestration.approval_policy import (
    ACTION_BROWSER,
    ACTION_BROWSER_LOCAL,
    ACTION_NETWORK,
    ACTION_READ,
    VERDICT_ALLOW,
    VERDICT_ASK,
    VERDICT_DENY,
    classify,
    decide,
)
from app.orchestration.browser import driver as drv
from app.orchestration.browser.bridge import BrowserBridge, HostCallError, HostUnavailable
from app.orchestration.browser.keys import KeyParseError, describe, normalize_keys
from app.orchestration.tools.base import ToolContext


def _ctx(tmp_path, session_id: int = 101, permission_mode: str = "agent", approval_mode: str = "ask") -> ToolContext:
    ctx = ToolContext(workspace_root=str(tmp_path), session_id=session_id, task_id=1, agent_id=1, agent_name="t")
    ctx.permission_mode = permission_mode
    ctx.approval_mode = approval_mode
    return ctx


@pytest.fixture(autouse=True)
def _reset_states():
    drv._states.clear()
    yield
    drv._states.clear()


# ══════════════════════════════════════════════════════════
# 1. 规范化
# ══════════════════════════════════════════════════════════
def test_normalize_keys_variants():
    assert normalize_keys("Enter") == ["Enter"]
    assert normalize_keys("ctrl+a") == ["Control+A"]
    assert normalize_keys("ctrl + shift + k") == ["Control+Shift+K"]
    assert normalize_keys("Tab Tab Enter") == ["Tab", "Tab", "Enter"]
    assert normalize_keys("Control+A Delete") == ["Control+A", "Delete"]
    assert normalize_keys(["esc", "down", "cmd+c"]) == ["Escape", "ArrowDown", "Meta+C"]
    assert normalize_keys("Space") == [" "]
    assert normalize_keys("Control++") == ["Control++"]  # 以加号为主键
    with pytest.raises(KeyParseError):
        normalize_keys("")
    assert describe("Control+A") == "Ctrl+A"
    assert describe(" ") == "Space"


def test_normalize_url_and_local_detection():
    assert drv.normalize_url("localhost:5173") == "http://localhost:5173"
    assert drv.normalize_url("127.0.0.1:8000/api") == "http://127.0.0.1:8000/api"
    assert drv.normalize_url("example.com") == "https://example.com"
    assert drv.normalize_url("https://a.b/c") == "https://a.b/c"
    assert drv.normalize_url("file:///C:/x.html") == "file:///C:/x.html"
    assert drv.is_local_url("http://localhost:3000/x")
    assert drv.is_local_url("http://app.localhost:3000")
    assert drv.is_local_url("file:///C:/a.html")
    assert drv.is_local_url("http://[::1]:8080/")
    assert not drv.is_local_url("https://example.com")
    assert not drv.is_local_url("")


# ══════════════════════════════════════════════════════════
# 2. 审批分类
# ══════════════════════════════════════════════════════════
def _set_page(session_id: int, url: str) -> None:
    st = drv._state(session_id)
    st.tab = "t1"
    st.urls["t1"] = url


def test_browser_read_tools_always_allowed(tmp_path):
    ctx = _ctx(tmp_path)
    for tool in ("browser_snapshot", "browser_get_text", "browser_screenshot", "browser_tabs",
                 "browser_scroll", "browser_hover", "browser_wait", "browser_logs"):
        assert classify(tool, {}, ctx).action == ACTION_READ, tool
        assert decide(tool, {}, ctx).verdict == VERDICT_ALLOW, tool


def test_navigate_local_vs_external(tmp_path):
    ctx = _ctx(tmp_path)
    assert decide("browser_navigate", {"url": "localhost:5173"}, ctx).verdict == VERDICT_ALLOW
    d = decide("browser_navigate", {"url": "https://example.com"}, ctx)
    assert d.action == ACTION_NETWORK and d.verdict == VERDICT_ASK
    assert decide("browser_navigate", {"url": "https://example.com"}, _ctx(tmp_path, approval_mode="auto")).verdict == VERDICT_ALLOW


def test_write_actions_depend_on_current_page(tmp_path):
    ctx = _ctx(tmp_path, session_id=7)
    _set_page(7, "http://localhost:5173/login")
    info = classify("browser_click", {"ref": "ref_1"}, ctx)
    assert info.action == ACTION_BROWSER_LOCAL
    assert decide("browser_type", {"text": "x"}, ctx).verdict == VERDICT_ALLOW
    _set_page(7, "https://shop.example.com/cart")
    assert classify("browser_click", {"ref": "ref_1"}, ctx).action == ACTION_BROWSER
    assert decide("browser_click", {"ref": "ref_1"}, ctx).verdict == VERDICT_ASK
    assert decide("browser_click", {"ref": "ref_1"}, _ctx(tmp_path, session_id=7, approval_mode="auto")).verdict == VERDICT_ALLOW
    # 复制只是读，粘贴是写
    assert classify("browser_clipboard", {"action": "copy"}, ctx).action == ACTION_READ
    assert classify("browser_clipboard", {"action": "paste"}, ctx).action == ACTION_BROWSER


def test_batch_takes_most_severe_step(tmp_path):
    ctx = _ctx(tmp_path, session_id=8)
    _set_page(8, "http://localhost:3000")
    only_read = {"actions": [{"action": "snapshot"}, {"action": "scroll", "direction": "down"}]}
    assert classify("browser_batch", only_read, ctx).action == ACTION_READ
    local_write = {"actions": [{"action": "type", "ref": "ref_1", "text": "a"}, {"action": "click", "ref": "ref_2"}]}
    assert classify("browser_batch", local_write, ctx).action == ACTION_BROWSER_LOCAL
    # 先导航到外部站点再点击：后续步骤按新页面判定
    nav_then_click = {"actions": [{"action": "navigate", "url": "https://example.com"}, {"action": "click", "ref": "ref_2"}]}
    assert classify("browser_batch", nav_then_click, ctx).action == ACTION_BROWSER


def test_readonly_and_plan_modes_deny_page_writes(tmp_path):
    _set_page(9, "http://localhost:3000")
    for mode in ("readonly", "plan"):
        ctx = _ctx(tmp_path, session_id=9, permission_mode=mode, approval_mode="full")
        assert decide("browser_click", {"ref": "ref_1"}, ctx).verdict == VERDICT_DENY
        assert decide("browser_snapshot", {}, ctx).verdict == VERDICT_ALLOW


# ══════════════════════════════════════════════════════════
# 3. 宿主桥
# ══════════════════════════════════════════════════════════
class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


async def test_bridge_roundtrip_and_disconnect():
    b = BrowserBridge()
    ws = _FakeWs()
    await b.register(ws, {"host_id": "h1", "protocol": 1})
    assert ws.sent[0]["event"] == "browser.host.welcome"
    assert "kit_source" in ws.sent[0]["payload"]
    assert b.has_host()

    async def answer():
        await asyncio.sleep(0)
        req = ws.sent[-1]["payload"]
        b.resolve({"request_id": req["request_id"], "ok": True, "result": {"tab": "t1", "value": 42}})

    task = asyncio.create_task(answer())
    res = await b.request(5, "kit", {"fn": "state"}, timeout=2)
    await task
    assert res["value"] == 42
    assert ws.sent[-1]["payload"]["session_id"] == 5

    async def fail():
        await asyncio.sleep(0)
        req = ws.sent[-1]["payload"]
        b.resolve({"request_id": req["request_id"], "ok": False, "error": "元素不存在"})

    task = asyncio.create_task(fail())
    with pytest.raises(HostCallError, match="元素不存在"):
        await b.request(5, "kit", {}, timeout=2)
    await task

    pending = asyncio.create_task(b.request(5, "kit", {}, timeout=5))
    await asyncio.sleep(0.01)
    b.unregister(ws)
    with pytest.raises(HostUnavailable):
        await pending
    assert not b.has_host()


async def test_bridge_rejects_incompatible_protocol_and_times_out():
    b = BrowserBridge()
    await b.register(_FakeWs(), {"protocol": 999})
    assert not b.has_host()
    with pytest.raises(HostUnavailable):
        await b.request(1, "kit", {}, timeout=0.1)
    ok_ws = _FakeWs()
    await b.register(ok_ws, {"protocol": 1})
    with pytest.raises(HostCallError, match="超时"):
        await b.request(1, "kit", {}, timeout=0.05)


# ══════════════════════════════════════════════════════════
# 4. 动作编排（假宿主）
# ══════════════════════════════════════════════════════════
class _FakeHost:
    kind = "desktop"
    visible_to_user = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.kit_values: dict[str, object] = {}

    async def call(self, session_id, op, args, timeout, cancel_event=None):
        self.calls.append((op, dict(args)))
        base = {"tab": "tab-ai", "url": "http://localhost:5173/", "title": "Demo", "events": []}
        if op == "kit":
            v = self.kit_values.get(args["fn"])
            if isinstance(v, Exception):
                raise v
            return {**base, "value": v}
        if op in ("mouse", "keys", "text"):
            return {**base, "settle": {"navigated": False, "mutations": 3, "url": base["url"], "focus": "输入框「邮箱」"}}
        if op == "capture":
            return {**base, "base64": "AAAA", "mime": "image/jpeg", "width": 1280, "height": 800, "css_width": 1600, "css_height": 1000}
        if op == "nav":
            return {**base, "status": 200}
        return base


@pytest.fixture
def fake_host(monkeypatch):
    host = _FakeHost()

    async def _acquire():
        return host

    monkeypatch.setattr(drv, "acquire_host", _acquire)
    monkeypatch.setattr(drv, "_save_shot", lambda b64, mime: ("browser-shots/x.jpg", "/tmp/x.jpg"))
    return host


async def test_click_by_ref_resolves_then_clicks(fake_host):
    fake_host.kit_values["resolve"] = {"x": 10, "y": 20, "rect": {"x": 0, "y": 0, "width": 20, "height": 40},
                                       "label": "按钮「登录」", "ref": "ref_3"}
    out = await drv.execute(1, "click", {"ref": "3"})
    assert out.ok, out.error
    ops = [c[0] for c in fake_host.calls]
    assert ops == ["kit", "mouse"]
    assert fake_host.calls[0][1]["args"] == {"ref": "ref_3"}
    mouse = fake_host.calls[1][1]
    assert (mouse["x"], mouse["y"], mouse["action"]) == (10, 20, "click")
    assert "登录" in mouse["label"]
    assert "已点击 按钮「登录」" in out.text and "DOM" in out.text


async def test_click_refuses_covered_target_unless_forced(fake_host):
    fake_host.kit_values["resolve"] = {"x": 1, "y": 1, "label": "按钮「付款」", "covered_by": "对话框「提示」", "rect": {}, "ref": "ref_7"}
    out = await drv.execute(1, "click", {"text": "付款"})
    assert not out.ok and "遮挡" in out.error
    assert [c[0] for c in fake_host.calls] == ["kit"]
    # 强制点击：坐标会落在遮挡物上，改为直接在元素上派发点击（activate）
    out = await drv.execute(1, "click", {"text": "付款", "force": True})
    assert out.ok and "直接在元素上触发点击" in out.text
    mouse = [c for c in fake_host.calls if c[0] == "mouse"][-1][1]
    assert mouse["action"] == "activate" and mouse["ref"] == "ref_7"


async def test_covered_forced_click_is_plain_left_click_only(fake_host):
    fake_host.kit_values["resolve"] = {"x": 1, "y": 1, "label": "按钮「付款」", "covered_by": "框架「遮罩」", "rect": {}, "ref": "ref_7"}
    out = await drv.execute(1, "click", {"text": "付款", "force": True, "double": True})
    assert not out.ok and "只支持左键单击" in out.error
    assert [c[0] for c in fake_host.calls] == ["kit"]


async def test_get_text_notes_cross_origin_frames(fake_host):
    fake_host.kit_values["text"] = {"text": "正文", "total": 2, "label": "页面", "url": "http://x/", "title": "t", "cross_frames": 2}
    out = await drv.execute(1, "get_text", {})
    assert out.ok and "2 个跨域框架" in out.text


async def test_screenshot_scale_converts_click_coordinates(fake_host):
    out = await drv.execute(1, "screenshot", {})
    assert out.ok and out.image_b64 == "AAAA"
    assert "可直接用于 browser_click" in out.text
    fake_host.kit_values["resolve"] = {"label": "空白处", "rect": None}
    out = await drv.execute(1, "click", {"x": 640, "y": 400})
    assert out.ok
    mouse = [c for c in fake_host.calls if c[0] == "mouse"][-1][1]
    # 截图 1280 宽对应 CSS 1600 宽：缩放 0.8，截图坐标 640 → CSS 800
    assert (mouse["x"], mouse["y"]) == (800.0, 500.0)


async def test_type_with_submit_focuses_inserts_and_presses_enter(fake_host):
    fake_host.kit_values["focus"] = {"label": "输入框「搜索」", "rect": {"x": 0, "y": 0, "width": 10, "height": 10}}
    out = await drv.execute(1, "type_text", {"ref": "ref_2", "text": "你好", "submit": True})
    assert out.ok, out.error
    ops = [c[0] for c in fake_host.calls]
    assert ops == ["kit", "text", "keys"]
    assert fake_host.calls[0][1]["args"]["clear"] is True
    assert fake_host.calls[1][1]["text"] == "你好"
    assert fake_host.calls[2][1]["keys"] == ["Enter"]


async def test_clipboard_uses_ai_buffer_not_system(fake_host):
    fake_host.kit_values["selection"] = {"text": "复制的内容", "label": "页面"}
    out = await drv.execute(2, "clipboard", {"action": "copy"})
    assert out.ok and "AI 剪贴板" in out.text
    assert all(c[0] != "clipboard" for c in fake_host.calls)
    fake_host.kit_values["paste"] = {"handled": False, "editable": True, "label": "输入框「备注」"}
    out = await drv.execute(2, "clipboard", {"action": "paste"})
    assert out.ok
    text_call = [c for c in fake_host.calls if c[0] == "text"][-1][1]
    assert text_call["text"] == "复制的内容"


async def test_batch_stops_at_first_failure(fake_host):
    fake_host.kit_values["resolve"] = HostCallError("ref_9 已失效")
    out = await drv.execute(1, "batch", {"actions": [
        {"action": "wait", "ms": 1},
        {"action": "click", "ref": "ref_9"},
        {"action": "press", "keys": "Enter"},
    ]})
    assert not out.ok
    assert "第 2 步" in out.error and "已失效" in out.error
    assert all(c[0] != "keys" for c in fake_host.calls)


async def test_cached_tab_is_preference_not_requirement(fake_host):
    fake_host.kit_values["text"] = {"text": "x"}
    await drv.execute(3, "navigate", {"url": "localhost:5173"})
    fake_host.kit_values["snapshot"] = {"text": "# 页面", "count": 0}
    await drv.execute(3, "snapshot", {})
    kit_call = [c for c in fake_host.calls if c[0] == "kit"][-1][1]
    assert kit_call.get("prefer_tab") == "tab-ai" and "tab" not in kit_call


# ══════════════════════════════════════════════════════════
# 5. 功能开关
# ══════════════════════════════════════════════════════════
def test_registry_hides_browser_tools_when_disabled(monkeypatch):
    from app.orchestration.tools.registry import tool_registry

    monkeypatch.setattr(settings, "browser_enabled", False, raising=False)
    monkeypatch.setattr(settings, "desktop_enabled", False, raising=False)
    names = {t.name for t in tool_registry.for_agent(None)}
    assert not any(n.startswith("browser_") for n in names)
    assert tool_registry.get("browser_click") is not None  # 执行与历史回放仍可取到

    monkeypatch.setattr(settings, "browser_enabled", True, raising=False)
    names = {t.name for t in tool_registry.for_agent(None)}
    assert {"browser_navigate", "browser_snapshot", "browser_click", "browser_batch"} <= names


def test_browser_prompt_injected_only_when_enabled():
    from app.orchestration.prompts.main import build_main_system_prompt

    assert "Built-in Browser" in build_main_system_prompt(browser_enabled=True)
    assert "Built-in Browser" not in build_main_system_prompt(browser_enabled=False)


# ══════════════════════════════════════════════════════════
# 6. 截图持久化（消息流时间线预览）
# ══════════════════════════════════════════════════════════
def test_persist_tool_image_writes_preview(tmp_path, monkeypatch):
    import base64
    import io

    from PIL import Image

    from app.orchestration.tool_images import persist_tool_image
    from app.orchestration.tools.base import ToolResult

    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path), raising=False)
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, format="JPEG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    out = persist_tool_image("desktop_screenshot", ToolResult(ok=True, output="", data={"base64": b64}))
    assert out and out["url"].startswith("/api/uploads/tool-shots/")
    assert (out["width"], out["height"]) == (40, 30)
    assert (tmp_path / out["url"].split("/api/uploads/")[1]).is_file()
    # 浏览器截图已在 uploads/browser-shots：复用其路径，不重复写盘
    out2 = persist_tool_image("browser_screenshot", ToolResult(ok=True, output="", data={
        "base64": b64, "screenshot_path": "browser-shots/a.jpg", "width": 40, "height": 30,
        "url": "http://localhost:5173/", "title": "Demo"}))
    assert out2["url"] == "/api/uploads/browser-shots/a.jpg" and out2["page"] == "http://localhost:5173/"
    # 非截图类工具 / 失败结果 / 无图片：不产生预览
    assert persist_tool_image("fs_read", ToolResult(ok=True, output="", data={"base64": b64})) is None
    assert persist_tool_image("browser_click", ToolResult(ok=False, output="", data={"base64": b64})) is None
    assert persist_tool_image("browser_click", ToolResult(ok=True, output="", data={})) is None

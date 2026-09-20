"""调试后端客户端（plan-282-1441 #8 内置 MCP「开发调试」）。

包含三块能力：
1. `http_call` —— 接口调用与响应核对（无状态，最简单）。
2. `CdpSession` —— Web 前端调试，走 Chrome DevTools Protocol（WebSocket + JSON-RPC）。
3. `JdwpSession` —— Java 调试，走 JDWP（纯 stdlib socket + 二进制协议）。

设计取舍：
- CDP 用 `websockets`（项目已依赖）而不是 Playwright，避免为调试再拉一个浏览器栈；
  用户可在自己的浏览器上开 `--remote-debugging-port`，也可用内置浏览器面板。
- JDWP 手写实现明确**只覆盖主链路**：断点/命中/单步/继续/读变量。
  热替换、Kotlin 内联调试等高级特性不在范围内（已在计划文档中标注）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1) 接口调用
# ═══════════════════════════════════════════════════════════════

async def http_call(method: str, url: str, headers: dict | None = None,
                    body: str | None = None, timeout_s: int = 20) -> dict:
    """发起一次 HTTP 请求并返回结构化结果（状态码/耗时/头/正文）。"""
    import aiohttp

    method = (method or "GET").upper()
    started = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            kwargs: dict[str, Any] = {"headers": headers or {}}
            if body:
                kwargs["data"] = body.encode("utf-8")
            async with session.request(
                method, url, timeout=aiohttp.ClientTimeout(total=timeout_s), **kwargs,
            ) as resp:
                text = await resp.text(errors="replace")
                return {
                    "ok": True,
                    "status": resp.status,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "headers": dict(resp.headers),
                    "body": text[:20000],
                    "truncated": len(text) > 20000,
                }
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"请求超时（{timeout_s}s）", "url": url, "method": method}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "url": url, "method": method}


# ═══════════════════════════════════════════════════════════════
# 2) Web 调试（CDP）
# ═══════════════════════════════════════════════════════════════

class CdpSession:
    """一个 CDP 调试会话（连接某个 target 的 ws 端点）。

    CDP 要点：
    - 命令：`{"id": n, "method": "Debugger.enable", "params": {...}}`
    - 事件：`{"method": "Debugger.paused", "params": {...}}`（无 id）
    - 断点用 `Debugger.setBreakpointByUrl`（按 urlRegex + 行号），命中后
      `Debugger.paused` 事件里带 callFrames（含 location / scopeChain）。
    """

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._paused: asyncio.Queue = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self.breakpoints: dict[str, str] = {}   # "url:line" -> breakpointId
        self.scripts: dict[str, str] = {}       # scriptId -> url

    async def connect(self, timeout_s: int = 15) -> None:
        import websockets

        self._ws = await asyncio.wait_for(
            websockets.connect(self.ws_url, max_size=32 * 1024 * 1024), timeout=timeout_s)
        self._reader = asyncio.create_task(self._read_loop())
        await self.send("Debugger.enable", {})
        # 跟踪脚本地址，便于把 scriptId 映射成文件路径
        await self.send("Runtime.enable", {})

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "id" in msg and msg["id"] in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if not fut.done():
                        fut.set_result(msg)
                    continue
                method = msg.get("method")
                params = msg.get("params") or {}
                if method == "Debugger.scriptParsed":
                    sid = params.get("scriptId")
                    if sid:
                        self.scripts[str(sid)] = str(params.get("url") or "")
                elif method == "Debugger.paused":
                    await self._paused.put(params)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[cdp] 读取循环结束", exc_info=True)

    async def send(self, method: str, params: dict, timeout_s: int = 20) -> dict:
        if self._ws is None:
            raise RuntimeError("CDP 未连接")
        self._id += 1
        mid = self._id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        finally:
            self._pending.pop(mid, None)

    async def set_breakpoint(self, url_regex: str, line: int) -> dict:
        """按 URL 正则 + **1-based 行号**下断点（CDP 内部行号 0-based，此处减 1）。"""
        resp = await self.send("Debugger.setBreakpointByUrl", {
            "urlRegex": url_regex,
            "lineNumber": max(0, int(line) - 1),
        })
        bp_id = (resp.get("result") or {}).get("breakpointId")
        if bp_id:
            self.breakpoints[f"{url_regex}:{line}"] = str(bp_id)
        return {"ok": bp_id is not None, "breakpointId": bp_id, "raw": resp.get("error")}

    async def remove_breakpoint(self, url_regex: str, line: int) -> bool:
        key = f"{url_regex}:{line}"
        bp_id = self.breakpoints.pop(key, None)
        if not bp_id:
            return False
        await self.send("Debugger.removeBreakpoint", {"breakpointId": bp_id})
        return True

    async def wait_paused(self, timeout_s: int = 60) -> dict | None:
        """等待下一次命中（返回 CDP 的 Debugger.paused params）。"""
        try:
            return await asyncio.wait_for(self._paused.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None

    async def step(self, action: str = "over") -> dict:
        """单步：over（跳过）/ into（步入）/ out（跳出）。"""
        method = {
            "over": "Debugger.stepOver",
            "into": "Debugger.stepInto",
            "out": "Debugger.stepOut",
        }.get(action, "Debugger.stepOver")
        await self.send(method, {})
        return await self.wait_paused()

    async def resume(self) -> None:
        await self.send("Debugger.resume", {})

    async def evaluate_on_frame(self, frame_id: str, expression: str) -> dict:
        resp = await self.send("Debugger.evaluateOnCallFrame", {
            "callFrameId": frame_id, "expression": expression, "returnByValue": True,
        })
        result = resp.get("result") or {}
        if resp.get("error"):
            return {"ok": False, "error": resp["error"]}
        val = result.get("result") or {}
        return {"ok": True, "value": val.get("value"), "type": val.get("type"),
                "description": val.get("description")}

    def frame_summary(self, paused: dict, limit: int = 6) -> dict:
        """把 CDP paused 事件压缩成用户可读的"停在哪一行 + 调用栈 + 变量"。"""
        frames = paused.get("callFrames") or []
        loc = frames[0].get("location", {}) if frames else {}
        script_id = str(loc.get("scriptId") or "")
        url = self.scripts.get(script_id, "")
        stack = []
        for f in frames[:limit]:
            fl = f.get("location", {}) or {}
            stack.append({
                "function": f.get("functionName") or "(anonymous)",
                "url": self.scripts.get(str(fl.get("scriptId") or ""), url),
                "line": int(fl.get("lineNumber", 0)) + 1,   # 转回 1-based
                "column": int(fl.get("columnNumber", 0)) + 1,
            })
        # 首个作用域的局部变量（尽量少取，避免一次拉太多）
        variables: list[dict] = []
        scopes = frames[0].get("scopeChain") if frames else None
        if scopes:
            for sc in scopes:
                if sc.get("type") in ("local", "closure"):
                    variables.append({"scope": sc.get("type"), "objectId": (sc.get("object") or {}).get("objectId")})
                    break
        return {
            "file": url,
            "line": int(loc.get("lineNumber", 0)) + 1,
            "function": frames[0].get("functionName") if frames else "",
            "reason": paused.get("reason"),
            "callFrameId": frames[0].get("callFrameId") if frames else None,
            "stack": stack,
            "variableScopes": variables,
        }

    async def read_locals(self, paused: dict, max_vars: int = 30) -> list[dict]:
        """读取首个局部作用域的变量（name/value/type）。

        实现走 CDP 的 `Runtime.getProperties`：作用域对象自带 objectId，
        展开它即可拿到局部变量。不用 `evaluateOnCallFrame` 拼表达式——
        后者拿不到真正的局部变量（局部变量不挂在 this 上），且容易触发副作用。
        """
        frames = paused.get("callFrames") or []
        if not frames:
            return []
        scopes = frames[0].get("scopeChain") or []
        target = next((s for s in scopes if s.get("type") in ("local", "closure")), None)
        if not target:
            return []
        obj_id = (target.get("object") or {}).get("objectId")
        if not obj_id:
            return []
        props = await self.send("Runtime.getProperties", {
            "objectId": obj_id, "ownProperties": True, "accessorPropertiesOnly": False,
        })
        out: list[dict] = []
        for p in (props.get("result") or {}).get("result") or []:
            if len(out) >= max_vars:
                break
            val = p.get("value") or {}
            out.append({
                "name": p.get("name"),
                "type": val.get("type"),
                "value": val.get("value", val.get("description")),
            })
        return out


async def cdp_list_targets(port: int) -> list[dict]:
    """列出可调试的页面/目标（Chrome 的 /json/list）。"""
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}/json/list",
                         timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json(content_type=None)
    return [
        {
            "id": t.get("id"), "title": t.get("title"), "url": t.get("url"),
            "type": t.get("type"), "ws": t.get("webSocketDebuggerUrl"),
        }
        for t in (data or []) if isinstance(t, dict)
    ]


# ═══════════════════════════════════════════════════════════════
# 3) Java 调试（JDWP，纯 stdlib 实现）
# ═══════════════════════════════════════════════════════════════

# JDWP 常量（subset）
_JDWP_HANDSHAKE = b"JDWP-Handshake"
_CSET_VIRTUAL_MACHINE = 1
_CSET_REFERENCE_TYPE = 2
_CSET_METHOD = 6
_CSET_STACK_FRAME = 16
_CSET_EVENT = 64
_CSET_EVENT_REQUEST = 15
_CSET_OBJECT_REFERENCE = 9


class JdwpSession:
    """JDWP 客户端（同步 socket，包在 to_thread 里用）。

    只实现主链路：
      VirtualMachine.Version / AllClasses / ClassesBySignature
      ReferenceType.Methods / Method.LineTable
      EventRequest.Set / Event.Composite 轮询
      StackFrame.GetValues / ObjectReference 基础取值
      VirtualMachine.Resume / Suspend
    """

    def __init__(self, host: str, port: int, timeout_s: float = 10.0) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = timeout_s
        self.sock: Any = None
        self._rid = 0

    # ── 连接 ──

    def connect(self) -> None:
        import socket

        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.sendall(_JDWP_HANDSHAKE)
        got = self._recv_exact(len(_JDWP_HANDSHAKE))
        if got != _JDWP_HANDSHAKE:
            raise RuntimeError(f"JDWP 握手失败（收到 {got!r}）")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:  # noqa: BLE001
                pass
            self.sock = None

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("JDWP 连接已关闭")
            buf += chunk
        return buf

    # ── 命令封装 ──

    def _command(self, command_set: int, command: int, data: bytes = b"") -> bytes:
        self._rid += 1
        rid = self._rid
        packet = struct.pack(">IIBBB", 11 + len(data), rid, 0, command_set, command) + data
        self.sock.sendall(packet)
        # 读回复（跳过事件包，直到拿到我们这条 id）
        while True:
            header = self._recv_exact(11)
            length, got_id, flags = struct.unpack(">IIB", header[:9])
            body = self._recv_exact(length - 11) if length > 11 else b""
            if flags & 0x80:
                # 事件包：暂存（避免丢失命中信息）
                self._pending_events = getattr(self, "_pending_events", [])
                self._pending_events.append((header[9], header[10], body))
                continue
            if got_id == rid:
                error_code = struct.unpack(">H", body[:2])[0] if len(body) >= 2 else 0
                if error_code:
                    raise RuntimeError(f"JDWP 命令失败（error={error_code}）")
                return body[2:]

    def version(self) -> str:
        data = self._command(_CSET_VIRTUAL_MACHINE, 1)
        # 跳过 description/jdwpVersion/vmVersion/major/minor 等字符串
        return f"JDWP {len(data)}B 响应"

    def classes_by_signature(self, signature: str) -> list[tuple[int, int]]:
        """按类签名（如 `Lcom/example/Order;`）取 (classID, refTypeTag)。"""
        payload = struct.pack(">I", len(signature)) + signature.encode() + struct.pack(">I", 0)
        data = self._command(_CSET_VIRTUAL_MACHINE, 2, payload)
        count = struct.unpack(">I", data[:4])[0]
        out: list[tuple[int, int]] = []
        off = 4
        for _ in range(count):
            tag = data[off]
            cid = struct.unpack(">Q", data[off + 1:off + 9])[0]
            off += 9
            out.append((cid, tag))
        return out

    def methods(self, class_id: int) -> list[tuple[int, str, str]]:
        """返回 (methodID, name, signature)。"""
        data = self._command(_CSET_REFERENCE_TYPE, 2, struct.pack(">Q", class_id))
        count = struct.unpack(">I", data[:4])[0]
        out: list[tuple[int, str, str]] = []
        off = 4
        for _ in range(count):
            mid = struct.unpack(">Q", data[off:off + 8])[0]
            off += 8
            nlen = struct.unpack(">I", data[off:off + 4])[0]
            off += 4
            name = data[off:off + nlen].decode("utf-8", "replace")
            off += nlen
            slen = struct.unpack(">I", data[off:off + 4])[0]
            off += 4
            sig = data[off:off + slen].decode("utf-8", "replace")
            off += slen
            out.append((mid, name, sig))
        return out

    def line_table(self, class_id: int, method_id: int) -> list[tuple[int, int]]:
        """返回 [(lineCodeIndex, lineNumber)]。"""
        data = self._command(_CSET_METHOD, 1,
                             struct.pack(">QQ", class_id, method_id))
        start = struct.unpack(">Q", data[:8])[0]
        end = struct.unpack(">Q", data[8:16])[0]
        count = struct.unpack(">I", data[16:20])[0]
        out = []
        off = 20
        for _ in range(count):
            code_idx = struct.unpack(">Q", data[off:off + 8])[0]
            line = struct.unpack(">I", data[off + 8:off + 12])[0]
            off += 12
            out.append((code_idx, line))
        _ = (start, end)
        return out

    def set_breakpoint(self, class_id: int, code_index: int) -> int:
        """在 (class, codeIndex) 设断点，返回 requestID。

        EventRequest.Set 载荷：eventKind(1=Breakpoint) + suspendPolicy(1=ALL)
        + modifiers 数量 + 每个 modifier（Type 1 = LocationOnly）。
        """
        payload = struct.pack(">BB", 2, 1)          # eventKind=Breakpoint, suspendPolicy=ALL
        mod = struct.pack(">B", 7)                  # modifier kind 7 = LocationOnly
        mod += struct.pack(">B", 1)                 # refTypeTag = class
        mod += struct.pack(">Q", class_id)
        mod += struct.pack(">Q", code_index)
        payload += struct.pack(">I", 1) + mod
        data = self._command(_CSET_EVENT_REQUEST, 1, payload)
        return struct.unpack(">I", data[:4])[0]

    def clear_breakpoint(self, request_id: int) -> None:
        self._command(_CSET_EVENT_REQUEST, 2, struct.pack(">BI", 2, request_id))

    def resume(self) -> None:
        self._command(_CSET_VIRTUAL_MACHINE, 9)

    def stack_frames(self, thread_id: int, start: int = 0, length: int = 12) -> list[dict]:
        payload = struct.pack(">QII", thread_id, start, length)
        data = self._command(_CSET_VIRTUAL_MACHINE, 6, payload)
        count = struct.unpack(">I", data[:4])[0]
        out = []
        off = 4
        for _ in range(count):
            fid = struct.unpack(">Q", data[off:off + 8])[0]
            off += 8
            loc = data[off]
            cid = struct.unpack(">Q", data[off + 1:off + 9])[0]
            mid = struct.unpack(">Q", data[off + 9:off + 17])[0]
            idx = struct.unpack(">Q", data[off + 17:off + 25])[0]
            off += 25
            out.append({"frameId": fid, "loc": loc, "classID": cid, "methodID": mid, "index": idx})
        return out

    def drain_events(self) -> list[dict]:
        """取出已收到的事件（由 _command 在等待回复时顺带收集）。"""
        pending = getattr(self, "_pending_events", [])
        self._pending_events = []
        out = []
        for _kind, _fl, body in pending:
            try:
                out.append(self._parse_composite(body))
            except Exception:  # noqa: BLE001
                logger.debug("[jdwp] 事件解析失败", exc_info=True)
        return out

    @staticmethod
    def _parse_composite(body: bytes) -> dict:
        """解析 Event.Composite，只取 Breakpoint(2)/Step(3)/ClassPrepare(8)。

        该结构较繁琐（每类事件有自己的 union），这里按"足够定位到线程/断点"的原则
        解析常用片段，失败即返回空事件（不阻塞主流程）。
        """
        n = struct.unpack(">I", body[:4])[0] if len(body) >= 4 else 0
        off = 4
        events: list[dict] = []
        for _ in range(n):
            if off + 5 > len(body):
                break
            kind = body[off]
            off += 5  # eventKind(1) + requestID(4)
            # 各事件体的公共部分：threadID(8)
            if off + 8 > len(body):
                break
            thread = struct.unpack(">Q", body[off:off + 8])[0]
            off += 8
            if kind in (2, 3):  # Breakpoint / Step
                if off + 9 > len(body):
                    break
                loc = body[off]
                cid = struct.unpack(">Q", body[off + 1:off + 9])[0]
                off += 9
                # location 还可能带方法/索引（取决于 loc tag）
                mid = idx = None
                if loc == 2 and off + 16 <= len(body):
                    mid = struct.unpack(">Q", body[off:off + 8])[0]
                    idx = struct.unpack(">Q", body[off + 8:off + 16])[0]
                    off += 16
                events.append({"kind": "breakpoint" if kind == 2 else "step",
                               "threadId": thread, "classID": cid,
                               "methodID": mid, "codeIndex": idx})
            else:
                # 其他事件：无法可靠跳过后续变长字段，停止解析剩余事件
                events.append({"kind": f"event-{kind}", "threadId": thread})
                break
        return events[0] if len(events) == 1 else {"kind": "batch", "events": events}

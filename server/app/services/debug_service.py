"""调试服务（plan-282-1441 #8 内置 MCP「开发调试」的**状态宿主**）。

架构说明（关键设计）：
MCP 客户端（mcp_wrapper._call_stdio）每调用一个工具就 spawn 一个新进程、调完即 kill。
而调试会话（CDP / JDWP）本质是**长连接 + 跨调用保持状态**（断点表、暂停位置、
单步上下文）。因此会话状态不能放在 MCP 子进程里——必须由**主服务进程**持有：

    内置调试 MCP（子进程） --HTTP--> 主服务 /api/debug/*  -->  本模块持有会话
                                                        \\--> WS 广播 debug.paused

这样既满足"用户能看到断点停在哪一行"（主服务有 WS 广播能力），
也避免了在 MCP 子进程里做进程间状态共享。

会话按 (session_id, target) 隔离：不同会话的调试互不干扰。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# key = (chatcoder session_id, "web" | "java")；值是 CdpSession / JdwpSession
_sessions: dict[tuple[int, str], Any] = {}
# 每个会话的运行元信息（供面板状态条展示）
_meta: dict[tuple[int, str], dict] = {}
# 调试暂停后的"等待继续"信号：保持调试上下文，以便前端按钮（步入/继续）能接力
_lock = asyncio.Lock()

_MAX_SESSIONS = 8


def _key(session_id: int, target: str) -> tuple[int, str]:
    return (int(session_id), target)


async def _broadcast(session_id: int, payload: dict) -> None:
    """把调试状态推给前端（失败不阻塞）。"""
    try:
        from app.orchestration.agent_events import broadcast
        await broadcast(session_id, {"event": "debug.paused", "payload": payload})
    except Exception:  # noqa: BLE001
        logger.debug("[debug] 广播失败(非阻塞)", exc_info=True)


def status(session_id: int, target: str = "web") -> dict:
    k = _key(session_id, target)
    sess = _sessions.get(k)
    m = _meta.get(k) or {}
    return {
        "connected": sess is not None,
        "target": target,
        "breakpoints": len(getattr(sess, "breakpoints", {}) or {}),
        "paused": bool(m.get("paused")),
        "file": m.get("file"),
        "line": m.get("line"),
        "function": m.get("function"),
        "hitCount": int(m.get("hitCount") or 0),
        "stack": m.get("stack") or [],
        "variables": m.get("variables") or [],
        "reason": m.get("reason"),
    }


# ── Web（CDP）──

async def web_start(session_id: int, *, port: int = 9222, target_url: str | None = None) -> dict:
    """连接浏览器调试端口并建立 CDP 会话。

    port：浏览器以 `--remote-debugging-port=<port>` 启动后的端口。
    target_url：为空时取第一个 page 目标。
    """
    from app.mcp_servers.debug_client import CdpSession, cdp_list_targets

    async with _lock:
        k = _key(session_id, "web")
        old = _sessions.pop(k, None)
        if old is not None:
            try:
                await old.close()
            except Exception:  # noqa: BLE001
                pass

        try:
            targets = await cdp_list_targets(port)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"无法连接调试端口 {port}：{e}。"
                                          f"请以 --remote-debugging-port={port} 启动浏览器。"}
        pages = [t for t in targets if t.get("type") == "page" and t.get("ws")]
        chosen = None
        if target_url:
            chosen = next((t for t in pages if target_url in str(t.get("url") or "")), None)
        chosen = chosen or (pages[0] if pages else None)
        if chosen is None:
            return {"ok": False, "error": "没有可调试的页面（请先在浏览器中打开目标页面）"}

        sess = CdpSession(str(chosen["ws"]))
        try:
            await sess.connect()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"CDP 连接失败：{e}"}

        # 会话语义：连接即暂停（不自动继续），否则后续步骤会跑飞
        _sessions[k] = sess
        _meta[k] = {"paused": False, "started_at": time.time(), "page": chosen.get("url"),
                    "port": port, "hitCount": 0}
        if len(_sessions) > _MAX_SESSIONS:
            # 简单容量保护：丢掉最早建立的会话
            oldest = min(_meta.items(), key=lambda kv: kv[1].get("started_at", 0))[0]
            if oldest != k:
                await _close(oldest[0], oldest[1])
    return {"ok": True, "page": chosen.get("url"), "title": chosen.get("title"), "targets": len(pages)}


async def _close(session_id: int, target: str) -> None:
    k = _key(session_id, target)
    sess = _sessions.pop(k, None)
    _meta.pop(k, None)
    if sess is None:
        return
    try:
        await sess.close()
    except Exception:  # noqa: BLE001
        logger.debug("[debug] 关闭会话失败", exc_info=True)


async def web_breakpoint(session_id: int, url_regex: str, line: int) -> dict:
    k = _key(session_id, "web")
    sess = _sessions.get(k)
    if sess is None:
        return {"ok": False, "error": "尚未开启 Web 调试会话（先调用 web_debug_start）"}
    res = await sess.set_breakpoint(url_regex, line)
    if res.get("ok"):
        return {"ok": True, "breakpointId": res.get("breakpointId"),
                "url_regex": url_regex, "line": line}
    return {"ok": False, "error": f"下断点失败：{res.get('raw') or '未知原因'}"}


async def _after_pause(session_id: int, target: str, paused: dict | None) -> dict:
    """命中后整理信息、广播给前端，并返回结构化结果。"""
    k = _key(session_id, target)
    sess = _sessions.get(k)
    if sess is None:
        return {"ok": False, "error": "调试会话已关闭"}
    if paused is None:
        m = _meta.setdefault(k, {})
        m["paused"] = False
        await _broadcast(session_id, {"target": target, "phase": "timeout",
                                      **{kk: m.get(kk) for kk in ("file", "line", "function")}})
        return {"ok": False, "error": "等待命中超时（这段时间内没有触发断点）"}

    summary = sess.frame_summary(paused)
    variables = []
    try:
        variables = await sess.read_locals(paused)
    except Exception:  # noqa: BLE001
        logger.debug("[debug] 读取局部变量失败", exc_info=True)
    m = _meta.setdefault(k, {})
    m.update({"paused": True, "file": summary["file"], "line": summary["line"],
              "function": summary["function"], "stack": summary["stack"],
              "variables": variables, "reason": summary.get("reason"),
              # 保存当前帧 id：web_eval 需要它在暂停帧上求值
              "callFrameId": summary.get("callFrameId"),
              "hitCount": int(m.get("hitCount") or 0) + 1})
    payload = {"target": target, "phase": "paused", **{kk: m.get(kk) for kk in
               ("file", "line", "function", "stack", "variables", "reason", "hitCount")}}
    await _broadcast(session_id, payload)
    return {"ok": True, **payload}


async def web_wait(session_id: int, timeout_s: int = 60) -> dict:
    k = _key(session_id, "web")
    sess = _sessions.get(k)
    if sess is None:
        return {"ok": False, "error": "尚未开启 Web 调试会话"}
    paused = await sess.wait_paused(timeout_s)
    return await _after_pause(session_id, "web", paused)


async def web_step(session_id: int, action: str = "over", timeout_s: int = 60) -> dict:
    k = _key(session_id, "web")
    sess = _sessions.get(k)
    if sess is None:
        return {"ok": False, "error": "尚未开启 Web 调试会话"}
    m = _meta.setdefault(k, {})
    if not m.get("paused"):
        return {"ok": False, "error": "当前未处于暂停状态，无法单步（先触发断点或暂停）"}
    paused = await sess.step(action)
    return await _after_pause(session_id, "web", paused)


async def web_resume(session_id: int) -> dict:
    k = _key(session_id, "web")
    sess = _sessions.get(k)
    if sess is None:
        return {"ok": False, "error": "尚未开启 Web 调试会话"}
    await sess.resume()
    m = _meta.setdefault(k, {})
    m["paused"] = False
    await _broadcast(session_id, {"target": "web", "phase": "resumed",
                                  "file": m.get("file"), "line": m.get("line")})
    return {"ok": True, "message": "已继续运行"}


async def web_eval(session_id: int, expression: str) -> dict:
    """在当前暂停帧上求值（需处于暂停态）。"""
    k = _key(session_id, "web")
    sess = _sessions.get(k)
    if sess is None:
        return {"ok": False, "error": "尚未开启 Web 调试会话"}
    m = _meta.setdefault(k, {})
    # 命中时已保存 callFrameId（见 _after_pause）；未暂停则无帧可求值
    frame_id = m.get("callFrameId")
    if not frame_id:
        return {"ok": False, "error": "当前未暂停，无法在帧上求值表达式"}
    return await sess.evaluate_on_frame(str(frame_id), expression)


async def web_stop(session_id: int) -> dict:
    await _close(session_id, "web")
    await _broadcast(session_id, {"target": "web", "phase": "stopped"})
    return {"ok": True, "message": "Web 调试会话已关闭"}


# ── Java（JDWP）──

async def java_attach(session_id: int, host: str, port: int) -> dict:
    """附加到以 JDWP 启动的 JVM。"""
    from app.mcp_servers.debug_client import JdwpSession

    def _do() -> tuple[Any, str]:
        s = JdwpSession(host, int(port))
        s.connect()
        return s, s.version()

    try:
        sess, ver = await asyncio.wait_for(asyncio.to_thread(_do), timeout=15)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"JDWP 附加失败：{e}。请确认 JVM 以 "
                                      f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:{port} 启动。"}
    async with _lock:
        k = _key(session_id, "java")
        old = _sessions.pop(k, None)
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        _sessions[k] = sess
        _meta[k] = {"paused": False, "started_at": time.time(),
                    "host": host, "port": port, "hitCount": 0, "breakpoints": {}}
    return {"ok": True, "host": host, "port": port, "version": ver}


def _class_signature(class_name: str) -> str:
    """Java 类名 → JVM 签名：com.example.Order → Lcom/example/Order;"""
    n = class_name.strip().replace(".", "/")
    if n.startswith("L") and n.endswith(";"):
        return n
    return f"L{n};"


async def java_breakpoint(session_id: int, class_name: str, line: int) -> dict:
    """在类的指定行下断点（自动定位到该行所在的 codeIndex）。"""
    k = _key(session_id, "java")
    sess = _sessions.get(k)
    if sess is None:
        return {"ok": False, "error": "尚未附加 Java 调试会话（先调用 java_debug_attach）"}
    sig = _class_signature(class_name)

    def _do() -> dict:
        found = sess.classes_by_signature(sig)
        if not found:
            return {"ok": False, "error": f"未找到类 {class_name}（签名 {sig}）。"
                                          f"该 JVM 中可能尚未加载此类（请先触发它的加载）。"}
        class_id = found[0][0]
        # 找到 codeIndex 对应的行号最接近 line 的方法
        best: tuple[int, int] | None = None  # (codeIndex, methodID)
        for mid, _name, _msig in sess.methods(class_id):
            for code_idx, ln in sess.line_table(class_id, mid):
                if ln == int(line):
                    best = (code_idx, mid)
                    break
                # 行号不完全相等时取最接近的（受编译器优化/行号表影响）
                if best is None or abs(ln - int(line)) < 2:
                    best = (code_idx, mid)
            if best and best[0] is not None:
                break
        if best is None:
            return {"ok": False, "error": f"在 {class_name} 中定位不到第 {line} 行"}
        req_id = sess.set_breakpoint(class_id, best[0])
        return {"ok": True, "requestId": req_id, "class": class_name, "line": int(line)}

    try:
        res = await asyncio.wait_for(asyncio.to_thread(_do), timeout=25)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"下断点失败：{e}"}
    if res.get("ok"):
        m = _meta.setdefault(k, {})
        bps = m.setdefault("breakpoints", {})
        bps[f"{class_name}:{line}"] = res["requestId"]
    return res


async def java_wait(session_id: int, timeout_s: int = 60) -> dict:
    """等待断点命中（轮询事件）。"""
    k = _key(session_id, "java")
    sess = _sessions.get(k)
    if sess is None:
        return {"ok": False, "error": "尚未附加 Java 调试会话"}
    deadline = time.monotonic() + max(5, int(timeout_s))
    while time.monotonic() < deadline:
        ev = await asyncio.to_thread(sess.drain_events)
        hit = next((e for e in (ev or []) if e.get("kind") in ("breakpoint", "step")), None)
        if hit:
            return await _java_after_pause(session_id, sess, hit)
        await asyncio.sleep(0.25)
    m = _meta.setdefault(k, {})
    m["paused"] = False
    await _broadcast(session_id, {"target": "java", "phase": "timeout",
                                  "file": m.get("file"), "line": m.get("line")})
    return {"ok": False, "error": "等待命中超时（这段时间内没有触发断点）"}


async def _java_after_pause(session_id: int, sess: Any, hit: dict) -> dict:
    """"命中的整理：线程/栈帧/变量 → 结构化 + 广播。"""
    thread_id = int(hit.get("threadId") or 0)
    frames: list[dict] = []
    try:
        raw = await asyncio.to_thread(sess.stack_frames, thread_id, 0, 12)
        # 把 classID 换成可读类名（尽力而为：查不到就显示 id）
        for f in raw:
            frames.append({
                "classID": f.get("classID"),
                "methodID": f.get("methodID"),
                "index": f.get("index"),
                "line": None,
            })
    except Exception:  # noqa: BLE001
        logger.debug("[debug] 读取 Java 栈帧失败", exc_info=True)

    k = _key(session_id, "java")
    m = _meta.setdefault(k, {})
    m.update({
        "paused": True, "threadId": thread_id,
        "file": m.get("file") or "(Java 线程栈)",
        "line": m.get("line"),
        "function": m.get("function"),
        "stack": frames,
        "variables": [],
        "reason": hit.get("kind"),
        "hitCount": int(m.get("hitCount") or 0) + 1,
    })
    payload = {"target": "java", **{kk: m.get(kk) for kk in
               ("file", "line", "function", "stack", "variables", "reason", "hitCount")}}
    await _broadcast(session_id, payload)
    return {"ok": True, **payload}


async def java_resume(session_id: int) -> dict:
    k = _key(session_id, "java")
    sess = _sessions.get(k)
    if sess is None:
        return {"ok": False, "error": "尚未附加 Java 调试会话"}
    try:
        await asyncio.to_thread(sess.resume)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"继续执行失败：{e}"}
    m = _meta.setdefault(k, {})
    m["paused"] = False
    await _broadcast(session_id, {"target": "java", "phase": "resumed",
                                  "file": m.get("file"), "line": m.get("line")})
    return {"ok": True, "message": "已继续运行"}


async def java_step(session_id: int, _action: str = "over", timeout_s: int = 60) -> dict:
    """Java 单步：当前实现为"继续并等待下一次命中"。

    说明（明确边界）：JDWP 的 Step 需要设置 Step 事件请求并配合线程级
    suspend 策略；本实现先打通"继续 → 等待下一次断点命中"这一主链路，
    逐步单步（into/over/out 的精细区分）留待后续增强。
    """
    k = _key(session_id, "java")
    if k not in _sessions:
        return {"ok": False, "error": "尚未附加 Java 调试会话"}
    await java_resume(session_id)
    return await java_wait(session_id, timeout_s)


async def java_stop(session_id: int) -> dict:
    await _close(session_id, "java")
    await _broadcast(session_id, {"target": "java", "phase": "stopped"})
    return {"ok": True, "message": "Java 调试会话已关闭"}


def cleanup_session(session_id: int) -> None:
    """会话结束时清理其调试资源（避免长连接泄漏）。"""
    for target in ("web", "java"):
        k = _key(session_id, target)
        sess = _sessions.pop(k, None)
        _meta.pop(k, None)
        if sess is not None:
            try:
                closer = getattr(sess, "close", None)
                if closer:
                    closer()
            except Exception:  # noqa: BLE001
                pass


# ── 面板配置（落库，修"改了不保存"）──
#
# Web 调试端口 / JDWP 主机·端口此前只活在前端组件 state 里，刷新即丢。
# 现在落库为单行全局配置（见 models/debug_setting.py 的选型说明）。


def _settings_to_dict(row: Any) -> dict:
    from app.persistence.models.debug_setting import (
        DEFAULT_JDWP_HOST, DEFAULT_JDWP_PORT, DEFAULT_WEB_PORT,
    )

    return {
        "web_port": int(row.web_port or DEFAULT_WEB_PORT),
        "jdwp_host": (row.jdwp_host or DEFAULT_JDWP_HOST),
        "jdwp_port": int(row.jdwp_port or DEFAULT_JDWP_PORT),
        "saved": True,
        "updated_at": row.updated_at,
    }


async def get_settings(db: Any) -> dict:
    """读调试面板配置（从未保存过时返回默认值，saved=False）。"""
    from sqlalchemy import select

    from app.persistence.models.debug_setting import (
        DEFAULT_JDWP_HOST, DEFAULT_JDWP_PORT, DEFAULT_WEB_PORT, DebugSetting,
    )

    row = (await db.execute(select(DebugSetting).order_by(DebugSetting.id.asc()))).scalars().first()
    if row is None:
        return {"web_port": DEFAULT_WEB_PORT, "jdwp_host": DEFAULT_JDWP_HOST,
                "jdwp_port": DEFAULT_JDWP_PORT, "saved": False, "updated_at": None}
    return _settings_to_dict(row)


async def save_settings(db: Any, *, web_port: int | None = None,
                        jdwp_host: str | None = None, jdwp_port: int | None = None) -> dict:
    """写调试面板配置（单行 upsert），返回保存后的完整配置。

    只更新显式传入且取值合法的字段——避免前端漏传/传空把已有配置抹掉。
    """
    from sqlalchemy import select

    from app.persistence.models.debug_setting import (
        DEFAULT_JDWP_HOST, DEFAULT_JDWP_PORT, DEFAULT_WEB_PORT, DebugSetting,
    )

    row = (await db.execute(select(DebugSetting).order_by(DebugSetting.id.asc()))).scalars().first()
    if row is None:
        row = DebugSetting(web_port=DEFAULT_WEB_PORT, jdwp_host=DEFAULT_JDWP_HOST,
                           jdwp_port=DEFAULT_JDWP_PORT)
        db.add(row)
    # 端口合法性：1-65535（0 或越界一律忽略，保留原值）
    if web_port is not None and 1 <= int(web_port) <= 65535:
        row.web_port = int(web_port)
    if jdwp_port is not None and 1 <= int(jdwp_port) <= 65535:
        row.jdwp_port = int(jdwp_port)
    if jdwp_host is not None and str(jdwp_host).strip():
        row.jdwp_host = str(jdwp_host).strip()
    await db.commit()
    return _settings_to_dict(row)

"""内置 MCP：开发调试（plan-282-1441 #8）。

与「数据库连接」不同，调试是**有状态**的：断点表、暂停位置、单步上下文必须跨调用保持。
而 MCP 客户端每调用一个工具就 spawn 一个新进程（见 mcp_wrapper._call_stdio），
因此本进程**不持有状态**，而是作为代理回连主服务的 `/api/debug/*`
（状态由 services/debug_service 按会话隔离持有，并能借主服务的 WS 广播把
"断点停在哪一行"推给前端）。

三类能力（选型理由见《chatcoder 调试 MCP 集成 Arthas 方案》）：
  debug_http / debug_probe  —— 接口调用与响应核对（无状态）
  web_debug_*  （CDP）       —— Web 前端真断点/单步/求值
  java_debug_* （JDWP）       —— Java 真断点/单步（**独占**：IDEA 调试中连不上）
  java_* / arthas_*（Arthas） —— Java 现场观测（watch/trace/stack/thread/jad），
                                走 Attach API，**可与 IDEA 调试并存**（方案 §1 实测结论）

运行上下文由主服务注入环境变量：
  CHATCODER_SERVER_PORT / CHATCODER_SESSION_ID / CHATCODER_WORKSPACE
"""
from __future__ import annotations

import asyncio
import json
import os

from app.mcp_servers.base import ToolSpec, run
from app.mcp_servers.debug_client import http_call


def _base() -> str:
    port = os.environ.get("CHATCODER_SERVER_PORT") or "12973"
    return f"http://127.0.0.1:{port}/api/debug"


def _sid() -> int | None:
    raw = os.environ.get("CHATCODER_SESSION_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


async def _call(path: str, payload: dict) -> dict:
    """调用主服务调试端点。"""
    sid = _sid()
    if sid is None:
        return {"ok": False, "error": "缺少会话上下文（CHATCODER_SESSION_ID），"
                                      "请在会话内通过 / 命令调用本 MCP。"}
    body = json.dumps({**payload, "session_id": sid}, ensure_ascii=False)
    res = await http_call("POST", f"{_base()}/{path}", headers={"Content-Type": "application/json"},
                          body=body, timeout_s=120)
    if not res.get("ok"):
        return {"ok": False, "error": f"调用主服务失败：{res.get('error')}"}
    status = res.get("status")
    try:
        data = json.loads(res.get("body") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": f"主服务返回非 JSON（HTTP {status}）"}
    if status and status >= 400:
        return {"ok": False, "error": data.get("detail") or f"HTTP {status}"}
    return data if isinstance(data, dict) else {"ok": True, "data": data}


def _run(coro) -> dict:
    return asyncio.run(coro)


def _fmt_location(data: dict) -> str:
    """把命中信息渲染成"停在哪一行"的可读文本（用户可见性的核心）。"""
    f = data.get("file") or "(未知文件)"
    line = data.get("line")
    fn = data.get("function") or ""
    loc = f"{f}:{line}" if line else str(f)
    lines = [f"⏸ 已暂停：{loc}" + (f"（{fn}）" if fn else "")]
    if data.get("reason"):
        lines.append(f"原因：{data['reason']}")
    stack = data.get("stack") or []
    if stack:
        lines.append("\n调用栈：")
        for i, fr in enumerate(stack[:8]):
            u = fr.get("url") or fr.get("classID") or "?"
            lines.append(f"  #{i} {fr.get('function') or '(anonymous)'} — {u}:{fr.get('line') or fr.get('index')}")
    variables = data.get("variables") or []
    if variables:
        lines.append("\n局部变量：")
        for v in variables[:30]:
            lines.append(f"  {v.get('name')} = {v.get('value')}"
                         + (f"  ({v.get('type')})" if v.get("type") else ""))
    return "\n".join(lines)


# ── 工具实现 ──

def tool_http(args: dict) -> tuple[str, dict]:
    """接口调用与响应核对。"""
    method = str(args.get("method") or "GET")
    url = str(args.get("url") or "").strip()
    if not url:
        return ("缺少 url 参数。", {"error": "missing url"})
    headers = args.get("headers") if isinstance(args.get("headers"), dict) else None
    body = args.get("body")
    res = _run(http_call(method, url, headers=headers,
                        body=str(body) if body is not None else None,
                        timeout_s=int(args.get("timeout_s") or 20)))
    if not res.get("ok"):
        return (f"请求失败：{res.get('error')}\n`{method} {url}`", res)
    status = res.get("status")
    ok_mark = "✅" if isinstance(status, int) and status < 400 else "❌"
    text = (f"{ok_mark} `{method} {url}` → HTTP {status} · {res.get('elapsed_ms')}ms\n"
            f"```json\n{json.dumps(res.get('headers') or {}, ensure_ascii=False, indent=2)[:1200]}\n```\n"
            f"```text\n{(res.get('body') or '')[:4000]}\n```")
    if res.get("truncated"):
        text += "\n（响应体已截断，仅显示前 20000 字符）"
    return text, res


def tool_probe(args: dict) -> tuple[str, dict]:
    """一组 URL 依次探测并对比状态码。"""
    urls = args.get("urls")
    if not isinstance(urls, list) or not urls:
        return ("缺少 urls 参数（字符串数组）。", {"error": "missing urls"})
    expect = args.get("expect_status")
    results = []
    lines = ["# 接口探测对比"]
    for u in urls[:20]:
        res = _run(http_call("GET", str(u), timeout_s=int(args.get("timeout_s") or 15)))
        row = {"url": str(u), "ok": res.get("ok"), "status": res.get("status"),
               "elapsed_ms": res.get("elapsed_ms"), "error": res.get("error")}
        results.append(row)
        mark = "✅" if res.get("ok") and (expect is None or res.get("status") == expect) else "❌"
        lines.append(f"{mark} {u} → {res.get('status') or res.get('error')} · {res.get('elapsed_ms')}ms")
    return "\n".join(lines), {"results": results}


def tool_web_start(args: dict) -> tuple[str, dict]:
    data = _run(_call("web/start", {
        "port": int(args.get("port") or 9222),
        "target_url": args.get("target_url"),
    }))
    if not data.get("ok"):
        return (f"开启 Web 调试失败：{data.get('error')}", data)
    return (f"已连接浏览器调试端口 {args.get('port') or 9222}\n"
            f"页面：{data.get('page')}\n标题：{data.get('title')}", data)


def tool_web_breakpoint(args: dict) -> tuple[str, dict]:
    url_regex = str(args.get("url_regex") or "")
    line = int(args.get("line") or 0)
    if not url_regex or not line:
        return ("需要 url_regex（页面地址匹配）与 line（1-based 行号）。", {"error": "missing args"})
    data = _run(_call("web/breakpoint", {"url_regex": url_regex, "line": line}))
    if not data.get("ok"):
        return (f"下断点失败：{data.get('error')}", data)
    return (f"已在 {url_regex} 第 {line} 行下断点。请让用户操作页面触发该代码，"
            f"然后调用 web_debug_wait 读取命中信息。", data)


def tool_web_wait(args: dict) -> tuple[str, dict]:
    data = _run(_call("web/wait", {"timeout_s": int(args.get("timeout_s") or 60)}))
    if not data.get("ok"):
        return (f"等待命中失败：{data.get('error')}", data)
    return (_fmt_location(data), data)


def tool_web_step(args: dict) -> tuple[str, dict]:
    action = str(args.get("action") or "over")
    data = _run(_call("web/step", {"action": action, "timeout_s": int(args.get("timeout_s") or 60)}))
    if not data.get("ok"):
        return (f"单步失败：{data.get('error')}", data)
    return (_fmt_location(data), data)


def tool_web_resume(args: dict) -> tuple[str, dict]:
    data = _run(_call("web/resume", {}))
    return ((data.get("message") or "已继续运行") if data.get("ok")
            else f"继续失败：{data.get('error')}", data)


def tool_web_eval(args: dict) -> tuple[str, dict]:
    expr = str(args.get("expression") or "").strip()
    if not expr:
        return ("缺少 expression 参数。", {"error": "missing expression"})
    data = _run(_call("web/eval", {"expression": expr}))
    if not data.get("ok"):
        return (f"求值失败：{data.get('error')}", data)
    return (f"`{expr}` = {data.get('value')}"
            + (f"  ({data.get('type')})" if data.get("type") else ""), data)


def tool_web_stop(args: dict) -> tuple[str, dict]:
    data = _run(_call("web/stop", {}))
    return ((data.get("message") or "已关闭") if data.get("ok")
            else f"关闭失败：{data.get('error')}", data)


def tool_java_attach(args: dict) -> tuple[str, dict]:
    host = str(args.get("host") or "127.0.0.1")
    port = int(args.get("port") or 5005)
    data = _run(_call("java/attach", {"host": host, "port": port}))
    if not data.get("ok"):
        return (f"附加失败：{data.get('error')}", data)
    return (f"已附加到 JVM {host}:{port}。接下来可用 java_debug_breakpoint 下断点。", data)


def tool_java_breakpoint(args: dict) -> tuple[str, dict]:
    cls = str(args.get("class_name") or "").strip()
    line = int(args.get("line") or 0)
    if not cls or not line:
        return ("需要 class_name（如 com.example.OrderService）与 line（1-based 行号）。",
                {"error": "missing args"})
    data = _run(_call("java/breakpoint", {"class_name": cls, "line": line}))
    if not data.get("ok"):
        return (f"下断点失败：{data.get('error')}", data)
    msg = f"已在 {cls}:{line} 下断点。触发后调用 java_debug_wait 读取命中信息。"
    # plan-308-1542 需求7-B（用户决策 D：双向写入）：可选同步到 IDEA 配置，
    # 使"本软件/AI 打的断点 IDEA 里也能看到"。必须明确告知生效条件。
    if args.get("sync_idea"):
        file = str(args.get("file") or "").strip()
        pp = _project_path(args)
        if file and pp:
            idea_res = _run(_call("idea/breakpoints/add",
                                  {"project_path": pp, "file": file, "line": line}))
            if idea_res.get("ok"):
                msg += f"\n已同步到 IDEA 配置（{file}:{line}）。注意：{idea_res.get('warning') or ''}"
            else:
                msg += f"\n⚠ IDEA 同步失败：{idea_res.get('error')}"
        else:
            msg += "\n（sync_idea 已开启，但缺少 file / 工作区路径，未同步到 IDEA）"
    return (msg, data)


def tool_java_wait(args: dict) -> tuple[str, dict]:
    data = _run(_call("java/wait", {"timeout_s": int(args.get("timeout_s") or 60)}))
    if not data.get("ok"):
        return (f"等待命中失败：{data.get('error')}", data)
    return (_fmt_location(data), data)


def tool_java_step(args: dict) -> tuple[str, dict]:
    data = _run(_call("java/step", {"action": str(args.get("action") or "over"),
                                    "timeout_s": int(args.get("timeout_s") or 60)}))
    if not data.get("ok"):
        return (f"单步失败：{data.get('error')}", data)
    return (_fmt_location(data), data)


def tool_java_resume(args: dict) -> tuple[str, dict]:
    data = _run(_call("java/resume", {}))
    return ((data.get("message") or "已继续运行") if data.get("ok")
            else f"继续失败：{data.get('error')}", data)


def tool_java_stop(args: dict) -> tuple[str, dict]:
    data = _run(_call("java/stop", {}))
    return ((data.get("message") or "已关闭") if data.get("ok")
            else f"关闭失败：{data.get('error')}", data)


# ── plan-308-1542 需求7-A：断点枚举 / 删除（AI 与面板共用的可观测能力）──

def _fmt_breakpoints(rows: list[dict]) -> str:
    if not rows:
        return "当前没有断点。"
    lines = [f"当前共 {len(rows)} 个断点："]
    for b in rows:
        loc = b.get("file") or b.get("class") or "?"
        line = b.get("line")
        lines.append(f"- [{b.get('target')}] {loc}:{line if line is not None else '?'} "
                     f"(id={b.get('id')}, source={b.get('source', 'app')})")
    return "\n".join(lines)


def tool_debug_breakpoints(args: dict) -> tuple[str, dict]:
    """列出当前调试会话的断点（web / java）。"""
    target = str(args.get("target") or "web")
    data = _run(_call("breakpoints", {"target": target}))
    if not data.get("ok", True):
        return (f"读取断点失败：{data.get('error')}", data)
    return (_fmt_breakpoints(data.get("breakpoints") or []), data)


def tool_web_debug_remove_breakpoint(args: dict) -> tuple[str, dict]:
    bp_id = str(args.get("breakpoint_id") or "").strip()
    if not bp_id:
        return ("需要 breakpoint_id（可用 debug_breakpoints 查询）。", {"error": "missing breakpoint_id"})
    data = _run(_call("breakpoints/remove", {"target": "web", "breakpoint_id": bp_id}))
    return ((f"已删除 Web 断点 {bp_id}") if data.get("ok")
            else f"删除失败：{data.get('error')}", data)


def tool_java_debug_remove_breakpoint(args: dict) -> tuple[str, dict]:
    bp_id = str(args.get("breakpoint_id") or "").strip()
    if not bp_id:
        return ("需要 breakpoint_id（可用 debug_breakpoints 查询）。", {"error": "missing breakpoint_id"})
    data = _run(_call("breakpoints/remove", {"target": "java", "breakpoint_id": bp_id}))
    return ((f"已删除 Java 断点 {bp_id}") if data.get("ok")
            else f"删除失败：{data.get('error')}", data)


# ── plan-308-1542 需求7-B：与 IntelliJ IDEA 的双向断点通道 ──

def _project_path(args: dict) -> str:
    """工程路径：优先显式参数，其次 CHATCODER_WORKSPACE（内置 MCP 已注入）。"""
    return str(args.get("project_path") or os.environ.get("CHATCODER_WORKSPACE") or "").strip()


def _fmt_idea_breakpoints(rows: list[dict]) -> str:
    if not rows:
        return "IDEA 工程中当前没有断点。"
    lines = [f"IDEA 工程中已配置 {len(rows)} 个断点："]
    for b in rows:
        lines.append(f"- {b.get('file')}:{b.get('line')}"
                     f"{'（已禁用）' if b.get('enabled') is False else ''}")
    return "\n".join(lines)


def tool_idea_list_breakpoints(args: dict) -> tuple[str, dict]:
    """读取 IDEA 工程内的断点（.idea/workspace.xml）。"""
    pp = _project_path(args)
    if not pp:
        return ("缺少工程路径（可通过 project_path 指定）", {"error": "missing project_path"})
    data = _run(_call("idea/breakpoints", {"project_path": pp}))
    if not data.get("ok"):
        return (f"读取失败：{data.get('error')}", data)
    if not data.get("available", True):
        return (f"未启用 IDEA 联动：{data.get('reason')}", data)
    return (_fmt_idea_breakpoints(data.get("breakpoints") or []), data)


def tool_idea_add_breakpoint(args: dict) -> tuple[str, dict]:
    """把断点写入 IDEA 配置（需重启 IDEA 生效；写前自动备份）。"""
    pp = _project_path(args)
    file = str(args.get("file") or "").strip()
    line = int(args.get("line") or 0)
    if not pp or not file or not line:
        return ("需要 project_path（或工作区上下文）、file 与 line。", {"error": "missing args"})
    data = _run(_call("idea/breakpoints/add", {"project_path": pp, "file": file, "line": line}))
    if not data.get("ok"):
        return (f"写入失败：{data.get('error')}", data)
    warn = data.get("warning") or ""
    dup = "（该断点已存在，未重复写入）" if data.get("duplicated") else ""
    return (f"已写入 IDEA 断点 {file}:{line}{dup}。\n注意事项：{warn}", data)


def tool_idea_remove_breakpoint(args: dict) -> tuple[str, dict]:
    pp = _project_path(args)
    file = str(args.get("file") or "").strip()
    line = int(args.get("line") or 0)
    if not pp or not file or not line:
        return ("需要 project_path、file 与 line。", {"error": "missing args"})
    data = _run(_call("idea/breakpoints/remove", {"project_path": pp, "file": file, "line": line}))
    if not data.get("ok"):
        return (f"移除失败：{data.get('error')}", data)
    return (f"已从 IDEA 配置移除 {file}:{line}。{data.get('warning') or ''}", data)


def tool_idea_debug_session(args: dict) -> tuple[str, dict]:
    """探测 IDEA 调试会话（JDWP 占用）：其通道被占用时改用 Arthas 观测。"""
    pp = _project_path(args)
    data = _run(_call("idea/session", {"project_path": pp or ""}))
    if not data.get("ok"):
        return (f"探测失败：{data.get('error')}", data)
    lines = [f"IDEA 运行中：{'是' if data.get('idea_running') else '否'}"]
    sess = data.get("sessions") or []
    if sess:
        lines.append(f"检测到 {len(sess)} 个 JDWP 调试会话：")
        for s in sess:
            lines.append(f"- pid={s.get('pid')} {s.get('main_class')} (JDWP {s.get('jdwp_port')})")
    lines.append(str(data.get("note") or ""))
    return ("\n".join([x for x in lines if x]), data)


def tool_idea_method_at_line(args: dict) -> tuple[str, dict]:
    """由「文件:行」推导 class#method（供为 IDEA 断点建立 Arthas 观测）。"""
    pp = _project_path(args)
    file = str(args.get("file") or "").strip()
    line = int(args.get("line") or 0)
    if not pp or not file or not line:
        return ("需要 project_path、file 与 line。", {"error": "missing args"})
    data = _run(_call("idea/method-at-line", {"project_path": pp, "file": file, "line": line}))
    if not data.get("ok"):
        return (f"未识别方法：{data.get('error')}", data)
    return (f"该行所在方法：{data.get('target')}（可用 arthas_watch 建立观测）", data)


# ── Arthas（现场诊断：IDEA 调试中也能用）──
#
# 与 java_debug_*（JDWP）的分工：JDWP 一个 JVM 同时只能被一个调试器连接，
# IDEA 调试启动的进程连不上（实测 ConnectionRefused）；Arthas 走 Attach API，
# 是另一条独立通道，可并存。代价是只能"观测"，没有真断点/单步。

_WATCH_DEFAULT_EXPRESS = "{params, returnObj, throwable}"


def _exec_sync(command: str, timeout_ms: int = 20000) -> tuple[str, dict]:
    """同步执行一条命令并返回渲染好的文本。"""
    data = _run(_call("arthas/exec", {"command": command,
                                     "exec_timeout_ms": timeout_ms, "async": False}))
    if not data.get("ok"):
        return (f"命令执行失败：{data.get('error')}\n`{command}`", data)
    return (data.get("text") or "（无输出）", data)


def tool_java_list_processes(args: dict) -> tuple[str, dict]:
    """枚举本机 JVM（Arthas attach 的第一步）。"""
    data = _run(_call("arthas/processes", {"java_home": args.get("java_home")}))
    if not data.get("ok"):
        return (f"枚举 Java 进程失败：{data.get('error')}", data)
    procs = data.get("processes") or []
    if not procs:
        return ("没有发现可 attach 的 Java 进程。", data)
    lines = ["# 本机 Java 进程", "",
             "| PID | 主类 | 是否已在调试 | JDWP 地址 |", "| --- | --- | --- | --- |"]
    for p in procs:
        lines.append(f"| {p.get('pid')} | `{p.get('main_class')}` | "
                     f"{'是（IDEA 已占用 JDWP）' if p.get('debugging') else '否'} | "
                     f"{p.get('jdwp_address') or '-'} |")
    lines += ["",
              "说明：「已在调试 = 是」的进程其 **JDWP 通道已被 IDEA 独占**，"
              "`java_debug_attach` 连接会被拒绕；这种场景改用 `java_attach_process(pid)` 走 Arthas。",
              f"JDK：`{data.get('java_home')}`（{data.get('java_home_source')}）"]
    return ("\n".join(lines), data)


def tool_java_attach_process(args: dict) -> tuple[str, dict]:
    """按 PID attach（Arthas 通道）。"""
    pid = int(args.get("pid") or 0)
    if not pid:
        return ("需要 pid（先用 java_list_processes 获取）。", {"error": "missing pid"})
    payload: dict = {"pid": pid}
    if args.get("http_port"):
        payload["http_port"] = int(args["http_port"])
    if args.get("java_home"):
        payload["java_home"] = str(args["java_home"])
    data = _run(_call("arthas/attach", payload))
    if not data.get("ok"):
        return (f"attach 失败：\n{data.get('error')}", data)
    return (f"已 attach 到 PID {pid}（Arthas {data.get('version')}，"
            f"HTTP 端口 {data.get('http_port')}，JDK {data.get('java_home')}）。\n"
            f"接下来可用 arthas_watch / arthas_trace / arthas_thread / arthas_jad 等做现场诊断；"
            f"**用完请调用 arthas_stop**（它会先 reset 还原被增强的类，避免长期影响 IDEA 调试）。",
            data)


def _observe(kind: str, args: dict) -> tuple[str, dict]:
    """构造并执行 watch / trace / tt 观测命令。

    连续输出类命令默认 **异步提交**（立即拿 jobId），因为典型用法是：
    先提交观测 → 再触发业务（debug_http）→ 最后 arthas_pull 取结果。
    同步执行会阻塞到命中 -n 次或窗口超时，中间没机会触发业务。
    """
    cls = str(args.get("class_name") or "").strip()
    method = str(args.get("method") or "").strip()
    if not cls or not method:
        return ("需要 class_name（全限定类名，如 com.x.DemoService）与 method（方法名，含重载任选一个名即可）。",
                {"error": "missing args"})
    times = max(1, int(args.get("times") or 5))
    depth = max(1, min(5, int(args.get("depth") or 2)))
    timeout_ms = max(5000, min(90000, int(args.get("exec_timeout_ms") or 30000)))
    run_async = args.get("run_async")
    run_async = True if run_async is None else bool(run_async)
    cond = str(args.get("condition") or "").strip()

    # 实测：arthas 4.3.5 下 `watch com.x.Y#method` 报 No class or method is affected
    # （enhancer 未生效），必须用**空格**分隔的 `watch com.x.Y method` 写法。
    if kind == "watch":
        express = str(args.get("express") or _WATCH_DEFAULT_EXPRESS)
        cmd = f"watch {cls} {method} '{express}'"
        if cond:
            cmd += f" '{cond}'"
        cmd += f" -n {times} -x {depth}"
    elif kind == "trace":
        cmd = f"trace {cls} {method}"
        if cond:
            cmd += f" '{cond}'"
        cmd += f" -n {times}"
    else:  # tt
        cmd = f"tt -t {cls} {method} -n {times}"

    if run_async:
        data = _run(_call("arthas/exec", {"command": cmd,
                                          "exec_timeout_ms": timeout_ms, "async": True}))
        if not data.get("ok"):
            return (f"提交观测失败：{data.get('error')}\n`{cmd}`", data)
        return (f"已提交观测：`{cmd}`\njobId = {data.get('job_id')}\n\n"
                f"下一步：① 触发业务（用 debug_http 调接口，或让用户操作页面）；"
                f"② 调 arthas_pull 取结果。\n"
                f"（观测窗口 {timeout_ms}ms，`-n {times}` 次；未命中时会返回 timeExpired 提示，"
                f"**不代表方法没被调用**）", data)
    return _exec_sync(cmd, timeout_ms)


def tool_arthas_watch(args: dict) -> tuple[str, dict]:
    return _observe("watch", args)


def tool_arthas_trace(args: dict) -> tuple[str, dict]:
    return _observe("trace", args)


def tool_arthas_tt(args: dict) -> tuple[str, dict]:
    return _observe("tt", args)


def tool_arthas_pull(args: dict) -> tuple[str, dict]:
    payload: dict = {"timeout_s": float(args.get("timeout_s") or 30)}
    if args.get("job_id"):
        payload["job_id"] = str(args["job_id"])
    data = _run(_call("arthas/pull", payload))
    if not data.get("ok"):
        return (f"拉取观测结果失败：{data.get('error')}", data)
    return (data.get("text") or "（无输出）", data)


def tool_arthas_stop(args: dict) -> tuple[str, dict]:
    data = _run(_call("arthas/stop", {}))
    return ((data.get("message") or "已断开") if data.get("ok")
            else f"断开失败：{data.get('error')}", data)


def tool_arthas_interrupt(args: dict) -> tuple[str, dict]:
    job_id = args.get("job_id")
    if job_id is None or str(job_id).strip() == "":
        return ("需要 job_id（提交观测时返回的值）。", {"error": "missing job_id"})
    data = _run(_call("arthas/interrupt", {"job_id": job_id}))
    if not data.get("ok"):
        return (f"中止观测失败：{data.get('error')}", data)
    return (data.get("message") or "已请求中断该观测作业。", data)


def tool_arthas_jad(args: dict) -> tuple[str, dict]:
    cls = str(args.get("class_name") or "").strip()
    if not cls:
        return ("需要 class_name（全限定类名）。", {"error": "missing class_name"})
    method = str(args.get("method") or "").strip()
    cmd = f"jad {cls}" + (f" {method}" if method else "")
    if args.get("source_only", True):
        cmd += " --source-only"
    return _exec_sync(cmd, int(args.get("exec_timeout_ms") or 30000))


def tool_arthas_sc(args: dict) -> tuple[str, dict]:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return ("需要 pattern（类名或通配符，如 com.x.*Service）。", {"error": "missing pattern"})
    cmd = f"sc {pattern}"
    if args.get("detail"):
        cmd += " -d"
    return _exec_sync(cmd, int(args.get("exec_timeout_ms") or 20000))


def tool_arthas_sm(args: dict) -> tuple[str, dict]:
    cls = str(args.get("class_name") or "").strip()
    if not cls:
        return ("需要 class_name（全限定类名）。", {"error": "missing class_name"})
    cmd = f"sm {cls}"
    if args.get("method"):
        cmd += f" {str(args['method']).strip()}"
    if args.get("detail", True):
        cmd += " -d"
    return _exec_sync(cmd, int(args.get("exec_timeout_ms") or 20000))


def tool_arthas_thread(args: dict) -> tuple[str, dict]:
    if args.get("thread_id"):
        cmd = f"thread {int(args['thread_id'])}"
    elif args.get("top_n"):
        cmd = f"thread -n {int(args['top_n'])}"
    elif args.get("state"):
        cmd = f"thread --state {str(args['state']).strip()}"
    else:
        cmd = "thread"
    return _exec_sync(cmd, int(args.get("exec_timeout_ms") or 20000))


def tool_arthas_stack(args: dict) -> tuple[str, dict]:
    cls = str(args.get("class_name") or "").strip()
    if not cls:
        return ("需要 class_name（全限定类名）。", {"error": "missing class_name"})
    cmd = f"stack {cls}"
    if args.get("method"):
        cmd += f" {str(args['method']).strip()}"
    cmd += f" -n {max(1, int(args.get('times') or 1))}"
    return _exec_sync(cmd, int(args.get("exec_timeout_ms") or 20000))


def tool_arthas_dashboard(args: dict) -> tuple[str, dict]:
    # dashboard 是持续刷新视图；同步执行只会拿到快照（exec 超时后返回）
    return _exec_sync("dashboard -n 1", int(args.get("exec_timeout_ms") or 15000))


def tool_arthas_jvm(args: dict) -> tuple[str, dict]:
    return _exec_sync("jvm", int(args.get("exec_timeout_ms") or 20000))


def tool_arthas_exec(args: dict) -> tuple[str, dict]:
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return ("需要 command（Arthas 命令原文）。", {"error": "missing command"})
    return _exec_sync(cmd, int(args.get("exec_timeout_ms") or 20000))


def tool_debug_status(args: dict) -> tuple[str, dict]:
    """当前调试状态（用户与模型都可用它确认"现在停在哪"）。"""
    out: dict = {}
    lines = ["# 调试状态"]
    for target in ("web", "java"):
        data = _run(_call(f"{target}/status", {}))
        out[target] = data
        if not data.get("connected"):
            lines.append(f"- {target}：未连接")
            continue
        state = "已暂停" if data.get("paused") else "运行中"
        loc = f"{data.get('file') or '?'}:{data.get('line')}" if data.get("line") else ""
        lines.append(f"- {target}：{state} {loc} · 断点 {data.get('breakpoints', 0)} 个"
                     f" · 已命中 {data.get('hitCount', 0)} 次")
    # Arthas 现场诊断通道（可与 IDEA 调试并存）
    ar = _run(_call("arthas/status", {}))
    out["arthas"] = ar
    if ar.get("attached"):
        lines.append(f"- arthas：已 attach PID {ar.get('pid')} · HTTP {ar.get('http_port')}"
                     f" · Arthas {ar.get('version')} · 进行中的观测 {ar.get('active_jobs', 0)} 个")
    else:
        lines.append("- arthas：未 attach（IDEA 调试中使用 java_attach_process 连接）")
    return "\n".join(lines), out


_HTTP_PROPS = {
    "method": {"type": "string", "description": "HTTP 方法，默认 GET"},
    "url": {"type": "string", "description": "完整 URL"},
    "headers": {"type": "object", "description": "请求头（可选）"},
    "body": {"type": "string", "description": "请求体（可选）"},
    "timeout_s": {"type": "integer", "description": "超时秒数，默认 20"},
}


def _build_tools() -> list[ToolSpec]:
    return [
        # ── 接口调用 ──
        ToolSpec(
            "debug_http",
            "发起一次 HTTP 请求并返回状态码/耗时/响应头/正文，用于核对接口行为。",
            {"type": "object", "properties": _HTTP_PROPS, "required": ["url"]},
            tool_http, risk_level="low",
        ),
        ToolSpec(
            "debug_probe",
            "对一组 URL 依次探测并对比状态码（快速定位哪个接口异常）。",
            {"type": "object", "properties": {
                "urls": {"type": "array", "items": {"type": "string"}},
                "expect_status": {"type": "integer", "description": "期望状态码（可选）"},
                "timeout_s": {"type": "integer"},
            }, "required": ["urls"]},
            tool_probe, risk_level="low",
        ),
        # ── Web 前端调试（CDP）──
        ToolSpec(
            "web_debug_start",
            "连接浏览器调试端口，开启 Web 前端调试会话（浏览器需以 --remote-debugging-port 启动）。",
            {"type": "object", "properties": {
                "port": {"type": "integer", "description": "调试端口，默认 9222"},
                "target_url": {"type": "string", "description": "指定页面 URL（可选，默认第一个页面）"},
            }},
            tool_web_start, risk_level="medium",
        ),
        ToolSpec(
            "web_debug_breakpoint",
            "在页面脚本的指定行下断点（url_regex 匹配脚本地址，line 为 1-based 行号）。",
            {"type": "object", "properties": {
                "url_regex": {"type": "string"},
                "line": {"type": "integer"},
            }, "required": ["url_regex", "line"]},
            tool_web_breakpoint, risk_level="medium",
        ),
        ToolSpec(
            "web_debug_wait",
            "等待断点命中，返回暂停位置（文件:行号）、调用栈与局部变量。",
            {"type": "object", "properties": {"timeout_s": {"type": "integer"}}},
            tool_web_wait, risk_level="medium",
        ),
        ToolSpec(
            "web_debug_step",
            "单步执行：over（跳过）/ into（步入）/ out（跳出），返回新位置。",
            {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["over", "into", "out"]},
                "timeout_s": {"type": "integer"},
            }},
            tool_web_step, risk_level="medium",
        ),
        ToolSpec(
            "web_debug_resume",
            "继续运行（取消暂停）。",
            {"type": "object", "properties": {}},
            tool_web_resume, risk_level="medium",
        ),
        ToolSpec(
            "web_debug_eval",
            "在当前暂停帧上求值 JS 表达式（用于查看变量与验证假设）。",
            {"type": "object", "properties": {"expression": {"type": "string"}},
             "required": ["expression"]},
            tool_web_eval, risk_level="medium",
        ),
        ToolSpec(
            "web_debug_stop",
            "关闭 Web 调试会话。",
            {"type": "object", "properties": {}},
            tool_web_stop, risk_level="low",
        ),
        # ── Java 调试（JDWP）──
        ToolSpec(
            "java_debug_attach",
            "附加到以 JDWP 启动的 JVM（-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:<port>）。",
            {"type": "object", "properties": {
                "host": {"type": "string", "description": "默认 127.0.0.1"},
                "port": {"type": "integer", "description": "JDWP 端口，默认 5005"},
            }},
            tool_java_attach, risk_level="medium",
        ),
        ToolSpec(
            "java_debug_breakpoint",
            "在 Java 类的指定行下断点。可选 sync_idea=true 同时写入 IDEA 配置"
            "（需重启 IDEA 生效，且运行期可能被 IDEA 覆盖）——实现「AI 打的断点 IDEA 也能看到」。",
            {"type": "object", "properties": {
                "class_name": {"type": "string", "description": "全限定类名，如 com.example.OrderService"},
                "line": {"type": "integer"},
                "sync_idea": {"type": "boolean", "description": "是否同步写入 IDEA 配置（默认 false）"},
                "file": {"type": "string", "description": "sync_idea 时的工程相对源码路径，如 src/main/java/A.java"},
            }, "required": ["class_name", "line"]},
            tool_java_breakpoint, risk_level="medium",
        ),
        ToolSpec(
            "java_debug_wait",
            "等待 Java 断点命中，返回暂停位置、线程与栈帧。",
            {"type": "object", "properties": {"timeout_s": {"type": "integer"}}},
            tool_java_wait, risk_level="medium",
        ),
        ToolSpec(
            "java_debug_step",
            "Java 单步（当前实现：继续并等待下一次命中）。",
            {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["over", "into", "out"]},
                "timeout_s": {"type": "integer"},
            }},
            tool_java_step, risk_level="medium",
        ),
        ToolSpec(
            "java_debug_resume",
            "继续运行 Java 程序。",
            {"type": "object", "properties": {}},
            tool_java_resume, risk_level="medium",
        ),
        ToolSpec(
            "java_debug_stop",
            "断开 Java 调试会话。",
            {"type": "object", "properties": {}},
            tool_java_stop, risk_level="low",
        ),
        # ── plan-308-1542 需求7-A：断点可观测能力 ──
        ToolSpec(
            "debug_breakpoints",
            "列出当前调试会话的断点明细（target=web|java）；配合 web_debug_remove_breakpoint / "
            "java_debug_remove_breakpoint 可逐条删除。",
            {"type": "object", "properties": {
                "target": {"type": "string", "enum": ["web", "java"], "description": "默认 web"},
            }},
            tool_debug_breakpoints, risk_level="low",
        ),
        ToolSpec(
            "web_debug_remove_breakpoint",
            "删除一个 Web 断点（breakpoint_id 由 debug_breakpoints 获取）。",
            {"type": "object", "properties": {
                "breakpoint_id": {"type": "string"},
            }, "required": ["breakpoint_id"]},
            tool_web_debug_remove_breakpoint, risk_level="medium",
        ),
        ToolSpec(
            "java_debug_remove_breakpoint",
            "删除一个 Java 断点（breakpoint_id 由 debug_breakpoints 获取）。",
            {"type": "object", "properties": {
                "breakpoint_id": {"type": "string"},
            }, "required": ["breakpoint_id"]},
            tool_java_debug_remove_breakpoint, risk_level="medium",
        ),
        # ── plan-308-1542 需求7-B：与 IntelliJ IDEA 的双向断点通道 ──
        ToolSpec(
            "idea_list_breakpoints",
            "读取 IntelliJ IDEA 工程内已配置的断点（.idea/workspace.xml）。"
            "用于「IDEA 里打了断点，本软件也能看到」。",
            {"type": "object", "properties": {
                "project_path": {"type": "string", "description": "工程根目录（可选，默认当前工作区）"},
            }},
            tool_idea_list_breakpoints, risk_level="low",
        ),
        ToolSpec(
            "idea_add_breakpoint",
            "把断点写入 IDEA 配置，使「本软件/AI 打的断点 IDEA 里也能看到」。"
            "写前自动备份 workspace.xml；注意需重启 IDEA 生效，且运行期可能被 IDEA 覆盖。",
            {"type": "object", "properties": {
                "project_path": {"type": "string"},
                "file": {"type": "string", "description": "工程相对路径，如 src/main/java/A.java"},
                "line": {"type": "integer"},
            }, "required": ["file", "line"]},
            tool_idea_add_breakpoint, risk_level="medium",
        ),
        ToolSpec(
            "idea_remove_breakpoint",
            "从 IDEA 配置移除断点（写前备份；需重启 IDEA 生效）。",
            {"type": "object", "properties": {
                "project_path": {"type": "string"},
                "file": {"type": "string"},
                "line": {"type": "integer"},
            }, "required": ["file", "line"]},
            tool_idea_remove_breakpoint, risk_level="medium",
        ),
        ToolSpec(
            "idea_debug_session",
            "探测 IDEA 是否在调试运行（JDWP 占用）：其通道被占用时真断点连不上，"
            "应改用 Arthas 观测（可与 IDEA 调试并存）。",
            {"type": "object", "properties": {
                "project_path": {"type": "string"},
            }},
            tool_idea_debug_session, risk_level="low",
        ),
        ToolSpec(
            "idea_method_at_line",
            "由「文件:行」推导 class#method，用于为 IDEA 断点建立 Arthas 方法级观测。",
            {"type": "object", "properties": {
                "project_path": {"type": "string"},
                "file": {"type": "string"},
                "line": {"type": "integer"},
            }, "required": ["file", "line"]},
            tool_idea_method_at_line, risk_level="low",
        ),
        # ── Arthas 现场诊断（走 Attach API，可与 IDEA 调试并存）──
        # 使用顺序（写进描述，避免模型乱试）：
        #   java_list_processes → java_attach_process(pid) → arthas_watch/arthas_trace
        #   → debug_http 触发业务 → arthas_pull 取结果 → arthas_stop 还原并断开
        ToolSpec(
            "java_list_processes",
            "列出本机可 attach 的 Java 进程（jps -lvm），并标注哪些已被调试器占用 JDWP 通道。"
            "排查 Java 服务问题的第一步。",
            {"type": "object", "properties": {
                "java_home": {"type": "string", "description": "指定 JDK 目录（可选，默认自动探测）"},
            }},
            tool_java_list_processes, risk_level="low",
        ),
        ToolSpec(
            "java_attach_process",
            "按 **PID** attach 到运行中的 JVM（Arthas 通道，基于 Attach API）。"
            "与 java_debug_attach（按 **JDWP 端口**）是两条不同通道：IDEA 以 Debug 模式启动的服务，"
            "其 JDWP 端口已被 IDEA 独占、java_debug_attach 会被拒绝，此时**必须**用本工具按 PID 连接。"
            "首次使用会自动下载 Arthas（需联网，可在面板指定本地 arthas-boot.jar）。",
            {"type": "object", "properties": {
                "pid": {"type": "integer", "description": "目标 JVM 进程号（java_list_processes 获取）"},
                "http_port": {"type": "integer", "description": "Arthas 在目标 JVM 内的 HTTP 端口（可选，默认 8563-8599 自动选空闲）"},
                "java_home": {"type": "string", "description": "运行 arthas-boot 的 JDK 目录（可选，默认自动探测）"},
            }, "required": ["pid"]},
            tool_java_attach_process, risk_level="medium",
        ),
        ToolSpec(
            "arthas_watch",
            "观测方法调用：入参、返回值、异常、耗时。不需要断点，IDEA 调试中也能用。"
            "**默认异步提交**（立即返回 jobId）：先本工具提交 → 再 debug_http 触发业务 → 最后 arthas_pull 取结果。"
            "注意：观测会对方法做字节码增强，可能使该方法上已下的 IDEA 断点失效（需重新下断点）；"
            "用完调用 arthas_stop 还原。",
            {"type": "object", "properties": {
                "class_name": {"type": "string", "description": "全限定类名，如 com.x.DemoService"},
                "method": {"type": "string", "description": "方法名"},
                "express": {"type": "string", "description": "观测表达式，默认 '{params, returnObj, throwable}'"},
                "condition": {"type": "string", "description": "条件表达式（可选，如 'params[0]==1'）"},
                "times": {"type": "integer", "description": "最多观测次数（-n），默认 5"},
                "depth": {"type": "integer", "description": "对象展开层数（-x），默认 2"},
                "exec_timeout_ms": {"type": "integer", "description": "观测窗口（毫秒），默认 30000"},
                "run_async": {"type": "boolean", "description": "是否异步提交，默认 true（推荐）"},
            }, "required": ["class_name", "method"]},
            tool_arthas_watch, risk_level="medium",
        ),
        ToolSpec(
            "arthas_trace",
            "追踪方法调用路径与各节点耗时（定位性能瓶颈）。"
            "同样默认异步提交，配合 arthas_pull 取结果。",
            {"type": "object", "properties": {
                "class_name": {"type": "string"},
                "method": {"type": "string"},
                "condition": {"type": "string", "description": "条件表达式（可选，如 '#cost > 100'）"},
                "times": {"type": "integer", "description": "最多追踪次数（-n），默认 5"},
                "exec_timeout_ms": {"type": "integer", "description": "观测窗口（毫秒），默认 30000"},
                "run_async": {"type": "boolean", "description": "是否异步提交，默认 true"},
            }, "required": ["class_name", "method"]},
            tool_arthas_trace, risk_level="medium",
        ),
        ToolSpec(
            "arthas_tt",
            "时间隧道：记录每次调用的现场（入参/返回/异常）供事后回放查看。",
            {"type": "object", "properties": {
                "class_name": {"type": "string"},
                "method": {"type": "string"},
                "times": {"type": "integer", "description": "记录条数（-n），默认 5"},
                "exec_timeout_ms": {"type": "integer", "description": "观测窗口（毫秒），默认 30000"},
                "run_async": {"type": "boolean", "description": "是否异步提交，默认 true"},
            }, "required": ["class_name", "method"]},
            tool_arthas_tt, risk_level="medium",
        ),
        ToolSpec(
            "arthas_pull",
            "拉取异步观测（watch/trace/tt）的结果。不传 job_id 时取最近提交的作业。"
            "返回 timeExpired 代表**窗口内未命中**，不代表方法没被调用。",
            {"type": "object", "properties": {
                "job_id": {"type": "string", "description": "作业 id（可选，默认最近一个）"},
                "timeout_s": {"type": "number", "description": "长轮询超时秒数，默认 30"},
            }},
            tool_arthas_pull, risk_level="low",
        ),
        ToolSpec(
            "arthas_stack",
            "查看某方法当前的调用栈（谁在调它）——无需触发业务，适合排查“这个方法到底被谁调用”。",
            {"type": "object", "properties": {
                "class_name": {"type": "string"},
                "method": {"type": "string"},
                "times": {"type": "integer", "description": "采集次数（-n），默认 1"},
            }, "required": ["class_name"]},
            tool_arthas_stack, risk_level="low",
        ),
        ToolSpec(
            "arthas_jad",
            "反编译目标 JVM 中**实际加载**的类（确认线上代码与本地源码是否一致）。",
            {"type": "object", "properties": {
                "class_name": {"type": "string"},
                "method": {"type": "string", "description": "只看某个方法（可选）"},
                "source_only": {"type": "boolean", "description": "只输出源码体，默认 true"},
            }, "required": ["class_name"]},
            tool_arthas_jad, risk_level="low",
        ),
        ToolSpec(
            "arthas_sc",
            "查找类（支持通配符），确认类是否已加载及其 classloader。",
            {"type": "object", "properties": {
                "pattern": {"type": "string", "description": "类名或通配符，如 com.x.*Service"},
                "detail": {"type": "boolean", "description": "输出详细信息（-d）"},
            }, "required": ["pattern"]},
            tool_arthas_sc, risk_level="low",
        ),
        ToolSpec(
            "arthas_sm",
            "查看类的方法列表（签名与行号）。",
            {"type": "object", "properties": {
                "class_name": {"type": "string"},
                "method": {"type": "string", "description": "只看某个方法（可选）"},
                "detail": {"type": "boolean", "description": "输出详细信息（-d），默认 true"},
            }, "required": ["class_name"]},
            tool_arthas_sm, risk_level="low",
        ),
        ToolSpec(
            "arthas_thread",
            "查看线程状态：无参列出全部线程，top_n 看最忙的前 N 个，state 按状态过滤，thread_id 看指定线程栈。",
            {"type": "object", "properties": {
                "thread_id": {"type": "integer", "description": "指定线程 id（可选）"},
                "top_n": {"type": "integer", "description": "最忙的前 N 个线程（-n）"},
                "state": {"type": "string", "description": "过滤状态，如 RUNNABLE / BLOCKED / WAITING"},
            }},
            tool_arthas_thread, risk_level="low",
        ),
        ToolSpec(
            "arthas_dashboard",
            "JVM 总览快照（线程/内存/GC/运行时）。",
            {"type": "object", "properties": {}},
            tool_arthas_dashboard, risk_level="low",
        ),
        ToolSpec(
            "arthas_jvm",
            "查看 JVM 详细信息（运行时/类加载/编译/GC/内存/操作系统）。",
            {"type": "object", "properties": {}},
            tool_arthas_jvm, risk_level="low",
        ),
        ToolSpec(
            "arthas_exec",
            "执行任意 Arthas 命令（兵底）。高风险：命令直接作用于目标 JVM，已默认禁用 "
            "redefine/retransform/dump/heapdump/classloader。",
            {"type": "object", "properties": {
                "command": {"type": "string", "description": "完整 Arthas 命令，如 'sysprop java.version'"},
                "exec_timeout_ms": {"type": "integer", "description": "命令超时（毫秒），默认 20000"},
            }, "required": ["command"]},
            tool_arthas_exec, risk_level="high",
        ),
        ToolSpec(
            "arthas_interrupt",
            "中止一个进行中的异步观测（watch/trace）。Arthas 同一会话同时只允许一个作业，"
            "上一个未结束时新的提交会被拒绝（Another job is running），此时先中止再重提。",
            {"type": "object", "properties": {
                "job_id": {"type": "string", "description": "作业 id（arthas_watch 等提交时返回）"},
            }, "required": ["job_id"]},
            tool_arthas_interrupt, risk_level="low",
        ),
        ToolSpec(
            "arthas_stop",
            "断开 Arthas 会话：先 reset 还原被增强的类（恢复 IDEA 断点），再关闭观察服务。"
            "诊断完成后必须调用。",
            {"type": "object", "properties": {}},
            tool_arthas_stop, risk_level="low",
        ),
        # ── 状态查询 ──
        ToolSpec(
            "debug_status",
            "查询当前调试状态（Web/Java 断点会话 + Arthas 现场诊断会话）。",
            {"type": "object", "properties": {}},
            tool_debug_status, risk_level="low",
        ),
    ]


def main() -> None:
    run("chatcoder-debugger", "1.1.0", _build_tools)


if __name__ == "__main__":
    main()

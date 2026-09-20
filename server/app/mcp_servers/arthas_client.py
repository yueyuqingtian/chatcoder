"""Arthas HTTP API 轻客户端（内置 MCP「开发调试」的 Arthas 通道）。

为什么走 HTTP 而不是 telnet
--------------------------
Arthas attach 成功后会在**目标 JVM 内**同时起 telnet(http) 两个 server。telnet 是
行式交互协议（要匹配提示符、处理 ANSI 控制符与分页 more），程序化驱动脆弱；HTTP
`/api` 是 JSON 请求 / JSON 响应，天然适合"一次调用拿一份结构化结果"。

协议事实（Arthas 官方 http-api 文档 + 本机实测 arthas 4.3.5）
-----------------------------------------------------------
- 端点：`POST http://127.0.0.1:<httpPort>/api`，请求体与响应体均为 JSON（必须 POST）
- 请求字段：`action` / `requestId` / `sessionId` / `consumerId` / `command` / `execTimeout`
- `action` 取值：`exec`（同步）/ `async_exec`（异步，配 `pull_results`）/
  `pull_results`（长轮询取结果）/ `interrupt_job` / `init_session` / `close_session`
- 响应 `state`：`SCHEDULED` / `SUCCEEDED` / `FAILED` / `REFUSED`
- 响应 `body`：`results[]`（元素 `type` 与命令名一致）、`jobId`、`jobStatus`、`timeExpired`

实测补充（文档未写清、但实现必须按此处理）：
- 一次性命令**不需要** sessionId（服务端自动建临时会话）——但那个临时会话**不能复用**
  （下一条命令就报 `session not found`）；
- `async_exec` / `pull_results` / `interrupt_job` **必须带** sessionId + consumerId，
  且两个值都要用 **`init_session` 响应里回显的**（服务端自己分配 consumerId：
  自己编一个（如 `probe_1`）会在 `pull_results` 阶段报 `consumer not found: <自编值>`）；
  `init_session` 也不返回 `body`，字段就在响应顶层。
- `jobStatus` 一次性命令是 **TERMINATED**（不是 FINISHED）；
- 同步观测（`watch -n` 未命中、执行到 execTimeout）结束时是 `state=INTERRUPTED`
  （而非 FAILED），**结果仍然有效**，须按 timeExpired 语义处理；
- `watch/trace/tt` 的类与方法需用 **空格分隔**：`watch com.x.Y method`。
  实测 `watch com.x.Y#method` 在 arthas 4.3.5 下报
  `No class or method is affected`（enhancer 未生效），日志还提示用 `sm` 核对。

本模块只做"请求组装 + 响应归一 + 渲染成模型易读文本"，**不持有进程状态**：
Arthas 常驻的是目标 JVM 内的 server，进程与端口由 services/arthas_service 持有。

代理注意：目标地址是 127.0.0.1，必须 `trust_env=False`。项目在设置里会把
HTTP_PROXY/HTTPS_PROXY 写进环境变量（供 web 工具走代理），若沿用 httpx 默认的
trust_env=True，本机请求会被塞进代理从而连不上 Arthas。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 渲染上限：模型侧工具输出不需要全量（超长结果反而挤占上下文）
_MAX_TEXT_CHARS = 12000
_MAX_ENTRIES = 30
# 这些 result 元素是协议噪音（命令结束标记 / 影响行数），不渲染给模型
_NOISE_TYPES = {"status", "row_affect", "command"}


class ArthasError(RuntimeError):
    """Arthas 侧不可用（进程未就绪 / 端口不通 / 返回非 JSON）。"""


def _to_text(value: Any) -> str:
    """把 Arthas 返回的值渲染成单行文本（字符串优先，其他走 JSON）。"""
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _fmt_ts(ts: Any) -> str:
    """Arthas 的 ts 两种形态：HTTP 结果是字符串时间，内部是毫秒数。"""
    if isinstance(ts, str):
        return ts
    try:
        return time.strftime("%H:%M:%S", time.localtime(int(ts) / 1000))
    except (TypeError, ValueError, OSError):
        return str(ts)


def _indent(text: Any, pad: str = "    ") -> str:
    return "\n".join(pad + ln for ln in str(text).splitlines())


def _fmt_watch_item(item: dict) -> list[str]:
    """watch / tt 的一条命中记录。

    字段名以 HTTP API 的实际输出为准（实测 arthas 4.3.5）：
    `className` / `methodName` / `accessPoint`(AtEnter|AtExit) / `cost` /
    `ts`(字符串时间) / `value`（观测表达式的结果，Arthas 自己渲染的文本，
    如 `@ArrayList[\n    @Object[][isEmpty=false;size=2],\n    null,\n]`）。
    为了兼容命令行形态，同时认 `class`/`method`/`params`/`returnObj`。
    """
    cls = item.get("className") or item.get("class")
    method = item.get("methodName") or item.get("method")
    head = f"● {cls}#{method}"
    if item.get("accessPoint"):
        head += f"  @{item['accessPoint']}"
    if item.get("location"):
        head += f"  ({item['location']})"
    if item.get("ts") is not None:
        head += f"  {_fmt_ts(item['ts'])}"
    if item.get("cost") is not None:
        head += f"  cost={item['cost']}ms"
    lines = [head]
    # value = 观测表达式（默认 {params, returnObj, throwable}）的结果，可能多行
    if item.get("value") is not None:
        lines.append(_indent(item["value"]))
    params = item.get("params")
    if params is not None and item.get("value") is None:
        if isinstance(params, list):
            for i, p in enumerate(params):
                lines.append(f"    params[{i}] = {_to_text(p)}")
        else:
            lines.append(f"    params = {_to_text(params)}")
    if item.get("returnObj") is not None:
        lines.append(f"    return   = {_to_text(item['returnObj'])}")
    if item.get("throwable") is not None:
        lines.append(f"    throw    = {_to_text(item['throwable'])}")
    return lines


def _fmt_trace_tree(nodes: list[dict], depth: int = 0) -> list[str]:
    """trace 的调用路径树（children 递归）。"""
    lines: list[str] = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        pad = "  " * depth
        item = (f"{pad}├─ {n.get('className') or n.get('class')}."
                f"{n.get('methodName') or n.get('method')}")
        if n.get("cost") is not None:
            item += f"  cost={n['cost']}ms"
        if n.get("location"):
            item += f"  ({n['location']})"
        lines.append(item)
        lines.extend(_fmt_trace_tree(n.get("children") or [], depth + 1))
    return lines


def _fmt_thread_row(th: dict) -> str:
    return (f"  {str(th.get('id') or '?'):>6}  {th.get('name')}  [{th.get('state')}]"
            f"  group={th.get('group')}  cpu={th.get('cpu')}")


def _fmt_stack_item(item: dict) -> list[str]:
    """stack 命令的命中：线程 + 调用链。"""
    lines = [f"● {item.get('threadName')} [{item.get('threadState')}] "
             f"{item.get('className')}#{item.get('methodName')} "
             f"({item.get('fileName')}:{item.get('lineNumber')})"]
    for fr in (item.get("stackTrace") or [])[:12]:
        if isinstance(fr, dict):
            lines.append(f"    at {fr.get('className')}.{fr.get('methodName')}"
                         f"({fr.get('fileName')}:{fr.get('lineNumber')})")
    return lines


def _render_one(res: dict) -> list[str]:
    """渲染单个 result 元素（`type` 与命令名一致，实测取值见各分支）。"""
    if not isinstance(res, dict):
        return [_to_text(res)]
    kind = str(res.get("type") or "result")
    if kind in _NOISE_TYPES:
        # 协议噪音：status={statusCode:0} 结束标记、row_affect={rowCount:n}。
        # 非 0 状态码才值得回报给模型，其余直接吞掉（否则每个结果尾部都是 JSON 噪音）。
        code = res.get("statusCode")
        if code not in (None, 0):
            return [f"  ⚠️ 命令结束状态码 {code}"]
        return []
    lines: list[str] = []
    inner = res.get("results")

    # watch / tt / trace / stack 的**两种形态**都要认：
    #  ① HTTP API：每条命中就是 results[] 里的一个顶层元素（带 className/methodName/value）
    #  ② 命令行/作业流：一个元素内部再嵌一个 results 列表
    if kind in ("watch", "tt", "trace", "stack"):
        if isinstance(inner, list) and inner:
            items = inner
        elif res.get("className") or res.get("class") or res.get("methodName"):
            items = [res]
        else:
            items = []
        for it in items[:_MAX_ENTRIES]:
            if not isinstance(it, dict):
                lines.append(_to_text(it))
            elif kind == "trace":
                lines.append(f"● {it.get('className') or it.get('class')}."
                             f"{it.get('methodName') or it.get('method')}"
                             + (f"  ({it.get('location')})" if it.get("location") else ""))
                lines.extend(_fmt_trace_tree(it.get("children") or [], 1))
            elif kind == "stack":
                lines.extend(_fmt_stack_item(it))
            else:
                lines.extend(_fmt_watch_item(it))
    elif kind == "thread":
        # 实测：`thread` / `-n` 的结果键是 threads / busyThreads
        rows = res.get("threads") or res.get("busyThreads") or []
        for th in rows[:60]:
            if isinstance(th, dict):
                lines.append(_fmt_thread_row(th))
    elif kind == "sm":
        infos = res.get("methodInfos") or ([res["methodInfo"]] if res.get("methodInfo") else [])
        for mi in infos[:80]:
            info = (mi.get("methodInfo") if isinstance(mi, dict) else None) or mi or {}
            lines.append(f"  {info.get('name') or info.get('methodName')}"
                         f"  descriptor={info.get('descriptor')}"
                         f"  line={info.get('lineNumber')}")
    elif kind == "sc":
        # 非 -d：classNames（字符串数组）；-d：classInfos（带 classInfo 结构）
        for name in (res.get("classNames") or [])[:80]:
            lines.append(f"  {name}")
        for ci in (res.get("classInfos") or [])[:60]:
            info = (ci.get("classInfo") if isinstance(ci, dict) else None) or {}
            lines.append(f"  {info.get('name')}  loader={info.get('classLoaderHash')}")
    elif kind == "jad":
        src = res.get("source")
        if isinstance(src, list):
            lines.extend(str(x) for x in src[:400])
        elif src:
            lines.append(str(src))
        else:
            lines.append(f"  {res.get('location') or res.get('class')}")
    elif kind == "version":
        lines.append(f"  arthas {res.get('version')}")
    elif kind == "reset":
        affect = res.get("affect") or {}
        lines.append(f"  已还原被增强的类：classCount={affect.get('classCount')} "
                     f"methodCount={affect.get('methodCount')}")
    if not lines:
        # 未知类型 / 空结果：退回 JSON 原文（宁可多给信息，不要静默丢结果）
        lines.append(_to_text({k: v for k, v in res.items() if k != "results"}))
        # 兜底分支可能产出空串（结果里只有空对象）——此时给个明确说明
        if not lines[-1].strip() or lines[-1] == "{}":
            lines[-1] = f"  （{kind} 无输出）"
    return lines


def render_results(results: Any, *, time_expired: bool = False,
                   job_status: str | None = None) -> str:
    """把 `body.results[]` 渲染成模型易读的 markdown 文本。"""
    out: list[str] = []
    if isinstance(results, list):
        for res in results[:20]:
            out.extend(_render_one(res))
    elif results is not None:
        out.append(_to_text(results))

    if not out:
        out.append("（无输出）")
    # 关键提示：超时不等于"方法没被调用"——不给模型误判的机会（方案 §5.3）
    if time_expired:
        out.insert(0, "⚠️ 观测窗口超时结束（timeExpired=true）："
                      "这段窗口内**未命中**该观测点，不代表方法没有被调用——"
                      "可加长 exec_timeout、增大 -n 次数后重试。")
    elif job_status and str(job_status).upper() in ("SCHEDULED", "RUNNING"):
        out.insert(0, f"⏳ 作业仍在运行（jobStatus={job_status}）：用 arthas_pull 取后续结果。")
    text = "\n".join(out)
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + f"\n…（输出过长已截断，共 {len(text)} 字符）"
    return text


class ArthasClient:
    """指向目标 JVM 内 Arthas HTTP server 的客户端。

    生命周期：由 arthas_service 按会话创建并复用（同一 http_port 只留一个实例）。
    """

    def __init__(self, port: int, *, host: str = "127.0.0.1",
                 session_id: str | None = None, consumer_id: str | None = None,
                 timeout_s: float = 30.0) -> None:
        self.host = host
        self.port = int(port)
        self.session_id = session_id or ""
        # consumerId 必须与提交观测时一致（且是 init_session 回显的那个），
        # 否则 pull_results 报 consumer not found
        self.consumer_id = consumer_id or ""
        self.timeout_s = timeout_s
        self._req_seq = 0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/api"

    def _next_ids(self) -> tuple[str, str]:
        self._req_seq += 1
        stamp = f"{int(time.time() * 1000)}"
        return f"req{self._req_seq}_{stamp}", f"chatcoder_{stamp}_{self._req_seq}"

    async def call(self, action: str, *, command: str | None = None,
                   exec_timeout_ms: int | None = None, job_id: str | None = None,
                   timeout_s: float | None = None) -> dict:
        """发一次 `/api` 请求并归一化响应。

        归一化结果字段：
          ok / state / message / results / job_id / job_status / time_expired /
          session_id / raw
        """
        req_id, consumer_id = self._next_ids()
        payload: dict[str, Any] = {
            "action": action,
            "requestId": req_id,
        }
        # 只有已持有（服务端分配的）consumerId 时才带上；否则让服务端自己分配，
        # 并在响应里把分配结果回写（见函数尾）
        if self.consumer_id:
            payload["consumerId"] = self.consumer_id
        if self.session_id:
            payload["sessionId"] = self.session_id
        if command is not None:
            payload["command"] = command
        if exec_timeout_ms is not None:
            payload["execTimeout"] = str(int(exec_timeout_ms))
        if job_id is not None:
            payload["jobId"] = job_id

        # trust_env=False：127.0.0.1 绝不能走全局代理（见模块头注释）
        try:
            async with httpx.AsyncClient(timeout=timeout_s or self.timeout_s,
                                         trust_env=False) as client:
                resp = await client.post(self.base_url, json=payload,
                                         headers={"Content-Type": "application/json"})
        except httpx.TimeoutException as e:
            raise ArthasError(f"Arthas HTTP 请求超时（{timeout_s or self.timeout_s}s）：{e}") from e
        except httpx.ConnectError as e:
            raise ArthasError(f"连接 Arthas HTTP API 失败（{self.host}:{self.port}）：{e}") from e
        except httpx.HTTPError as e:
            # 其余传输层异常一律归一成 ArthasError。
            # 实测必需：Arthas server 刚绑定端口时**会接受连接但不回响应**，httpx 抛
            # `RemoteProtocolError: Server disconnected without sending a response.`；
            # 早期只捕 ConnectError/Timeout，就绪探测的第一次抖动会直接冒到路由层
            # 变成 HTTP 500，把整条 Arthas 工具链堵死（attach 永远失败）。
            # 归一到 ArthasError 后，attach 的就绪循环可以继续轮询直到真正就绪。
            raise ArthasError(f"Arthas HTTP 传输异常（{type(e).__name__}）：{e}") from e

        if resp.status_code >= 400:
            return {"ok": False, "state": "FAILED", "message": f"HTTP {resp.status_code}",
                    "results": None, "raw": resp.text[:2000]}
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise ArthasError(f"Arthas 返回非 JSON（可能端口被别的服务占用）：{e}") from e

        body = data.get("body") or {}
        state = str(data.get("state") or "")
        message = str(body.get("message") or data.get("message") or "") if isinstance(body, dict) else ""
        # 服务端回显的会话/消费者 id：记下来供后续（异步观测）复用
        sid = str(data.get("sessionId") or "") or self.session_id
        if sid:
            self.session_id = sid
        if data.get("consumerId"):
            self.consumer_id = str(data["consumerId"])
        return {
            # INTERRUPTED：观测类命令执行到 execTimeout 被强行中断，**结果仍然有效**
            #（要按 timeExpired 语义解释，而不是当成失败）
            "ok": state.upper() in ("SUCCEEDED", "SCHEDULED", "INTERRUPTED"),
            "state": state,
            "message": message,
            "results": (body or {}).get("results") if isinstance(body, dict) else None,
            "job_id": (body or {}).get("jobId") if isinstance(body, dict) else None,
            "job_status": (body or {}).get("jobStatus") if isinstance(body, dict) else None,
            "time_expired": bool((body or {}).get("timeExpired")) if isinstance(body, dict) else False,
            "session_id": sid,
            "consumer_id": self.consumer_id,
            "raw": data,
        }

    # ── 语义化封装 ──

    async def init_session(self) -> dict:
        """申请一个有会话 id + 服务端分配 consumerId 的持久会话。

        这两个值在 async_exec / pull_results / interrupt_job 中必须**原样复用**：
        临时会话下一跳就失效，自编 consumerId 会在 pull 阶段报
        `consumer not found`（均为实测结论）。
        """
        data = await self.call("init_session", timeout_s=15.0)
        return {"session_id": data.get("session_id") or "",
                "consumer_id": data.get("consumer_id") or ""}

    async def close_session(self) -> None:
        """关闭 Arthas 会话（断开时调用；失败不影响本地清理）。"""
        if not self.session_id:
            return
        try:
            await self.call("close_session", timeout_s=8.0)
        except ArthasError:
            pass

    async def exec(self, command: str, *, exec_timeout_ms: int = 10000,
                   timeout_s: float | None = None) -> dict:
        """同步执行一条命令（含持续输出命令；调用方必须给 execTimeout 兜底）。"""
        return await self.call("exec", command=command, exec_timeout_ms=exec_timeout_ms,
                               timeout_s=timeout_s)

    async def async_exec(self, command: str, *, exec_timeout_ms: int = 10000) -> dict:
        """异步提交：立即返回 jobId，结果用 pull_results 拉。"""
        return await self.call("async_exec", command=command, exec_timeout_ms=exec_timeout_ms)

    async def pull_results(self, *, job_id: str | None = None,
                           timeout_s: float | None = None) -> dict:
        """拉取异步作业结果（长轮询，单次）。"""
        return await self.call("pull_results", job_id=job_id,
                               timeout_s=timeout_s or max(30.0, self.timeout_s))

    async def interrupt_job(self, job_id: str) -> dict:
        return await self.call("interrupt_job", job_id=job_id)

    async def version(self) -> str | None:
        """探测就绪：成功返回 arthas 版本号（如 4.3.5），失败返回 None。"""
        data = await self.call("exec", command="version", exec_timeout_ms=8000, timeout_s=10.0)
        if not data.get("ok"):
            return None
        results = data.get("results")
        if isinstance(results, list):
            for r in results:
                if isinstance(r, dict) and r.get("version"):
                    return str(r["version"])
        return None


__all__ = ["ArthasClient", "ArthasError", "render_results"]

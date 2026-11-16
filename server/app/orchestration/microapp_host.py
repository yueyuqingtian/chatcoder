"""宿主桥 server 层（plan-693-2868 · `docs/app1/11-host-bridge-protocol.md`）。

页面（guest webview）发起宿主操作的链路是「页面 → preload → 主进程 → 渲染层 → **本模块**
→ 回页面」。模块负责最小的三件事：

1. **op 分派**（契约 §4 + 设计文档 `docs/app1/12-capability-boundaries.md` 的 v3 补齐面）：
   `exec.start / exec.stdin / exec.stop / exec.status / fs.read / fs.list /
   workspace.info / host.capabilities`，请求形状
   `{ reqId, uid, appId, version, sessionId, op, args }`。op 表 `OP_HANDLERS` 是唯一事实
   来源：`host.capabilities` 的 `ops` 由它生成，不再手写第二份清单（加 op 忘了登记会
   「调不到」，不会「宣了却调不到」）；
2. **回执**：`microapp.host.response`（按 `reqId` 配对）与流式下行 `microapp.host.push`
   （契约 §5，100ms 或 200 行先到者聚批）；
3. **审计**（契约 §2）：每个请求一条 JSON Lines，落 `~/.chatcoder/logs/microapp-host.log`。

**为什么这里没有新造进程管理**：三个 `exec.*` 全部落在既有的后台命令注册表
（`app/orchestration/tools/bg_process.py` 的 `bg_process_registry`）上——
`exec.start` 直接走 `TerminalExecTool` 的 `waitForCompletion=false` 分支（与 AI 调
`terminal_exec` 起后台命令是同一条路：同一套 shell 解析、同一份环形输出缓冲、
同一套 `kill_process_tree` 整树终止），增量日志读注册表的 `poll()`，
`exec.stop` 复用注册表的 `kill()`。本模块只做「按契约改名 + 聚批 + 审计」，
不复制任何进程生命周期逻辑（那正是最容易两套漂移的地方）。

**协议 v3 的两个新 op**（`docs/app1/12-capability-boundaries.md` §4）
- `exec.stdin`：补上双向 I/O。页面经 `exec.start` 起的进程保留 stdin 管道
  （`TerminalExecTool(stdin_pipe=True)`），页面用 `exec.stdin` 发数据、`eof: true` 关流；
  AI 侧 `terminal_exec` 的默认行为**不变**（依旧不保留管道——等待输入的命令会立刻读到
  EOF，改成 PIPE 会让它变成永久挂起的孤儿进程）。
- `host.capabilities`：能力自描述。页面启动时读一次，据此决定显示哪些控件，而不是
  给一个「点了没反应」的按钮；AI 生成微应用前也照它核对边界。

**安全（维护者决策，契约 §2）**
- 页面发起的请求**不进审批链**（不弹卡），直接执行；能力面与 AI 侧一致。
- 每个请求写一条审计记录（appId / version / uid / sessionId / op / 参数摘要 / 结果 /
  耗时），写失败只告警，不影响请求本身。
- `fs.read` / `fs.list` 的**工作区边界是硬约束**（契约 §4）：越界一律
  `microapp-host-fs-outside-workspace`。这条不走 `safe_resolve_read` 的附件兜底——
  那是给 AI 读用户上传附件用的白名单，页面没有这个身份。
- `exec.*` 与 `terminal_exec` 保持同一能力面（`cwd` 允许工作区外，见
  `TerminalExecTool._resolve_path`）：契约只对 fs 两个 op 定了硬边界，其余靠审计留痕。

**已知口径**（改之前先看协议）
- 单条 WS 上行/下行都走全局通道（契约 §7），`microapp.host.request` 自带 `sessionId`，
  后端据此定位会话工作区根目录。
- `streamId` 就是后台命令注册表的 `shell_id`：页面拿到它即可 `exec.stop/status`，
  也让排障时日志（`[bg.start] shell_id=…`）与页面看到的 id 对得上。
- 后台缓冲区 stdout/stderr 合并成一路（`bg_process._collect` 用 `-- stderr --` 行分段），
  因此 `stream.log` 的 `stream` 字段恒为 `"stdout"`，与 `terminal_bg_status` 同一口径。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.orchestration.tools.bg_process import (
    StdinClosedError,
    bg_process_registry,
    decode_output,
)
from app.orchestration.tools.microapp import MICROAPP_RESULT_MAX_CHARS

logger = logging.getLogger(__name__)

# ── 契约常量（改名字必须同步 docs/app1/11-host-bridge-protocol.md 与 electron 侧）──
EVENT_REQUEST = "microapp.host.request"
EVENT_RESPONSE = "microapp.host.response"
EVENT_PUSH = "microapp.host.push"

# 契约 §5：主进程按 100ms 或 200 行（先到者）聚成一批再发，避免逐行 IPC
BATCH_MAX_LINES = 200
BATCH_INTERVAL_S = 0.1
# 拉取后台缓冲的轮询间隔：比聚批窗口小一半，保证「100ms 聚批」不会被轮询拖长
_POLL_INTERVAL_S = 0.05

# 桥协议版本（`host.capabilities` 的 protocol 字段）：v1 单 op 面、v2 加审计、v3 加
# `exec.stdin` + `host.capabilities`。页面可据此判断某 op 是否存在。
PROTOCOL_VERSION = 3

# fs.read 单次返回上限（与右侧预览面板的 TEXT_LIMIT 同量级，避免一屏日志吃掉渲染层内存）
FS_READ_MAX_BYTES = 512 * 1024
# fs.list 单次数目上限（防御超大目录；超限在结果里标 truncated，页面可再按子目录展开）
FS_LIST_MAX_ENTRIES = 2000
# 审计里参数摘要的长度上限（契约 §2：只留命令/路径前 200 字符，不写全量内容）
AUDIT_DIGEST_CHARS = 200
# 审计日志落盘路径（契约 §2）；环境变量仅供测试改道，部署侧不要设
AUDIT_LOG_ENV = "CHATCODER_MICROAPP_HOST_LOG"

# ── 错误码（契约 §3：页面侧 Error.message 以 microapp-host-<reason> 开头）──
ERR_BAD_REQUEST = "microapp-host-bad-request"
ERR_UNKNOWN_OP = "microapp-host-unknown-op"
ERR_SESSION_NOT_FOUND = "microapp-host-session-not-found"
ERR_FS_OUTSIDE = "microapp-host-fs-outside-workspace"
ERR_FS_NOT_FOUND = "microapp-host-fs-not-found"
ERR_EXEC_FAILED = "microapp-host-exec-failed"
ERR_STREAM_NOT_FOUND = "microapp-host-stream-not-found"
# v3 `exec.stdin` 专用（设计文档 §4.1）：streamId 不存在**或不属于本会话**都报这一个码——
# 两者对外不可区分，页面就无法用它探测别的会话有没有某个 streamId。
ERR_STREAM_UNKNOWN = "microapp-host-stream-unknown"
# v3 `exec.stdin` 专用：想写 stdin 但写不进去（进程已退出 / 没保留管道 / 管道已断）。
# 页面拿到它该做的是「把输入框置灰 + 提示进程已结束」，而不是重试。
ERR_STDIN_CLOSED = "microapp-host-stdin-closed"
ERR_INTERNAL = "microapp-host-internal"


class HostError(Exception):
    """页面请求的失败原因；`code` 即契约里的 `microapp-host-<reason>` 全串。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ───────────────────────── 审计日志（契约 §2）─────────────────────────


def audit_log_path() -> Path:
    """审计文件路径：`~/.chatcoder/logs/microapp-host.log`（契约 §2 指定位置）。"""
    override = (os.environ.get(AUDIT_LOG_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".chatcoder" / "logs" / "microapp-host.log"


def build_audit_record(
    *,
    app_id: str,
    version: str,
    uid: str,
    session_id: int,
    op: str,
    args: dict[str, Any],
    ok: bool,
    duration_ms: int,
    error: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """组装一条审计记录（字段与契约 §2 一一对应，纯函数便于单测）。

    `argsDigest` 只留命令/路径的前 200 字符——审计要能回答「页面拿这份权限做了什么」，
    不需要把整段输出或整个文件内容再抄一遍（那会把日志变成第二份数据副本）。
    """
    return {
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "appId": app_id,
        "version": version,
        "uid": uid,
        "sessionId": session_id,
        "op": op,
        "argsDigest": _args_digest(args),
        "ok": bool(ok),
        "durationMs": int(duration_ms),
        **({"error": error} if error else {}),
    }


def _args_digest(args: dict[str, Any]) -> str:
    """参数摘要：命令 / 路径 / streamId 三类里第一个非空的，截到 200 字符。

    `exec.stdin` 的 `data` **故意**不落摘要（设计文档 §2「只留命令/路径」的更严版本）：
    喂给进程的输入里可能是用户粘贴的 SQL、口令、调试命令，审计只需要回答「页面往哪个
    进程写了」（streamId 已足够），不必把数据再抄一份到日志里。
    """
    for key in ("command", "path", "streamId"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:AUDIT_DIGEST_CHARS]
    return ""


_AUDIT_LOCK = threading.Lock()


def _append_audit_line(record: dict[str, Any], path: Path) -> None:
    """同步追加一行 JSON（调用方放线程里跑，不占事件循环）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _AUDIT_LOCK, path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


async def write_audit(record: dict[str, Any]) -> None:
    """落盘一条审计；任何失败只告警——审计不能反过来挡住页面操作（契约 §2「不阻塞」）。"""
    try:
        await asyncio.to_thread(_append_audit_line, record, audit_log_path())
    except Exception:  # noqa: BLE001 — 磁盘满/无权限都不该让请求失败
        logger.warning("[microapp-host] 审计写入失败 record=%s", record, exc_info=True)


# ───────────────────────── 日志聚批（契约 §5）─────────────────────────


class LineBatcher:
    """把增量日志文本按「200 行 或 100ms（先到者）」聚成批。

    纯逻辑、不碰 IO：单测直接喂文本与时间戳即可验证聚批窗口。文本可能在某一行中间被
    切断（`readline` 之外的读取没有行边界），所以未完成的行留在 `_carry` 里，
    等下一次增量补齐——否则页面会看到被劈开的半行。
    """

    def __init__(self, max_lines: int = BATCH_MAX_LINES,
                 interval_s: float = BATCH_INTERVAL_S) -> None:
        self.max_lines = max_lines
        self.interval_s = interval_s
        self._pending: list[str] = []
        self._carry = ""
        self._first_at: float | None = None

    def feed(self, text: str, now: float) -> list[list[str]]:
        """并入新文本，返回此刻可以发出去的批次（每批 ≤ max_lines）。

        即使 `text` 为空也会走一次时间检查：进程安静下来时，攒着的那几行仍要在
        100ms 后发出去，不能等到「下一行到来」才发。
        """
        batches: list[list[str]] = []
        if text:
            self._carry += text
            parts = self._carry.split("\n")
            self._carry = parts.pop()          # 最后一段可能是不完整的行
            if parts:
                # 去掉行尾 \r：Windows 下命令输出是 CRLF，原样推给页面会渲染成多余回车
                self._pending.extend(part.rstrip("\r") for part in parts)
                if self._first_at is None:
                    self._first_at = now
        while len(self._pending) >= self.max_lines:
            batches.append(self._pending[:self.max_lines])
            del self._pending[:self.max_lines]
            # 剩余行重新起算窗口：否则积压时按轮询频率（50ms）不断刷出小批
            self._first_at = now
        if not self._pending:
            self._first_at = None
        elif self._first_at is not None and now - self._first_at >= self.interval_s:
            batches.append(self._pending)
            self._pending = []
            self._first_at = None
        return batches

    def flush(self) -> list[str]:
        """取走全部残余（进程退出时调用）：未完成的半行也一并交出，不丢最后一行。"""
        rest = list(self._pending)
        if self._carry:
            rest.append(self._carry)
        self._pending = []
        self._carry = ""
        self._first_at = None
        return rest


# ───────────────────────── 流式推送（契约 §5）─────────────────────────


class StreamPump:
    """把某个后台命令的输出持续推给页面。

    只做两件事：从注册表 `poll()` 取增量 → 交给 `LineBatcher` 聚批 → 经 `send` 发出
    `microapp.host.push`。进程退出后补发残余日志与 `stream.state exited`（带退出码），
    然后自行结束。这里不管理进程——进程的生命周期完全在 `bg_process_registry`。
    """

    def __init__(self, stream_id: str, uid: str, send: Callable[[dict], None]) -> None:
        self.stream_id = stream_id
        self.uid = uid
        self._send = send
        self._seq = 0
        self._batcher = LineBatcher()
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        try:
            self.task = asyncio.get_running_loop().create_task(self._run())
        except RuntimeError:  # 无事件循环（关停路径）：不推流，进程仍可查
            logger.debug("[microapp-host] 无事件循环，跳过流式推送 stream=%s", self.stream_id)
            return
        # 推流结束后从表里摘掉自己：长跑会话里开几十个进程不该累积成一张永不释放的表
        self.task.add_done_callback(lambda _t, _sid=self.stream_id: _PUMPS.pop(_sid, None))

    def _emit(self, payload: dict[str, Any]) -> None:
        self._seq += 1
        self._send({
            "event": EVENT_PUSH,
            "payload": {"uid": self.uid, "streamId": self.stream_id,
                        "seq": self._seq, **payload},
        })

    def _emit_log(self, lines: list[str]) -> None:
        if lines:
            self._emit({"event": "stream.log", "lines": lines, "stream": "stdout"})

    def _emit_state(self, state: str, code: int | None = None) -> None:
        payload: dict[str, Any] = {"event": "stream.state", "state": state}
        if code is not None:
            payload["code"] = int(code)
        self._emit(payload)

    async def _run(self) -> None:
        offset = 0
        try:
            self._emit_state("running")
            while True:
                snap = bg_process_registry.poll(self.stream_id, offset)
                now = time.monotonic()
                if snap is None:
                    # 条目已被清理（注册表只保留最近 100 条已退出）——按已退出收尾
                    self._emit_log(self._batcher.flush())
                    self._emit_state("exited")
                    return
                offset = int(snap["next_offset"])
                for batch in self._batcher.feed(snap.get("log") or "", now):
                    self._emit_log(batch)
                if not snap["running"]:
                    self._emit_log(self._batcher.flush())
                    self._emit_state("exited", snap.get("returncode"))
                    return
                await asyncio.sleep(_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 推流失败不能影响进程本身
            logger.warning("[microapp-host] 推流异常 stream=%s", self.stream_id, exc_info=True)


_PUMPS: dict[str, StreamPump] = {}


# ───────────────────────── 会话工作区 ─────────────────────────


async def session_workspace(session_id: int) -> str:
    """按会话解析工作区根目录（worktree 优先，其次项目路径，最后全局兜底）。

    与 `routers/turns.py` 建快照时的口径一致：分支会话的 `worktree_path` 才是它真正在改的目录，
    用 `project.path` 会让 `fs.*` 指向另一份代码。
    """
    from app.core.config import resolve_workspace_root
    from app.persistence.database import async_session_factory
    from app.services import project_service, session_service

    async with async_session_factory() as db:
        session = await session_service.get_session(db, session_id)
        if session is None:
            raise HostError(ERR_SESSION_NOT_FOUND, f"会话不存在：{session_id}")
        root = getattr(session, "worktree_path", None) or ""
        project_id = getattr(session, "project_id", None)
        if not root and project_id:
            project = await project_service.get_project(db, project_id)
            root = getattr(project, "path", "") if project is not None else ""
    return resolve_workspace_root(root or None)


# ───────────────────────── op 实现 ─────────────────────────


@dataclass(frozen=True)
class _OpContext:
    """一次 op 调用的横切上下文：请求归属（uid）、会话（sessionId）与下行发送器。

    handler 统一收 `(args, ctx)` 两个参数，才能被登记进 `OP_HANDLERS` 并由同一张表分派；
    换成这个之前是 `**kwargs` 各写一套签名，加 op 就得再改 dispatch 的 if 链。
    """

    uid: str
    session_id: int
    send: Callable[[dict], None]


def _inside_workspace(root: str, raw_path: str) -> Path:
    """把页面给的路径解析到工作区内；越界即 `microapp-host-fs-outside-workspace`。

    复用 `safe_resolve`（fs 工具同一套 realpath + symlink 校验），但**不**用
    `safe_resolve_read`：后者的附件目录兜底是「AI 读用户上传内容」的白名单，
    页面没有这个身份，工作区边界对 fs.read/fs.list 是硬约束（契约 §4）。
    """
    from app.orchestration.tools.safe_path import safe_resolve

    target = safe_resolve(root, raw_path or ".")
    if target is None:
        raise HostError(ERR_FS_OUTSIDE, f"路径不在会话工作区内：{raw_path!r}（工作区 {root}）")
    return target


def fs_read(root: str, args: dict[str, Any]) -> dict[str, Any]:
    """读工作区内文件，返回 `{ content, truncated }`（契约 §4）。"""
    target = _inside_workspace(root, str(args.get("path") or ""))
    if not target.is_file():
        raise HostError(ERR_FS_NOT_FOUND, f"文件不存在或不是普通文件：{args.get('path')!r}")
    try:
        with target.open("rb") as fh:
            data = fh.read(FS_READ_MAX_BYTES + 1)
    except OSError as exc:
        raise HostError(ERR_INTERNAL, f"读取失败：{exc}") from exc
    truncated = len(data) > FS_READ_MAX_BYTES
    # decode_output 复用后台命令那套 UTF-8 → GBK 回退：微应用很可能在中文 Windows 上读 GBK 文件
    return {"content": decode_output(data[:FS_READ_MAX_BYTES]), "truncated": truncated}


def fs_list(root: str, args: dict[str, Any]) -> dict[str, Any]:
    """列工作区内目录，返回 `{ entries: [{ name, type, size }] }`（契约 §4）。"""
    target = _inside_workspace(root, str(args.get("path") or ""))
    if not target.is_dir():
        raise HostError(ERR_FS_NOT_FOUND, f"目录不存在或不是目录：{args.get('path')!r}")
    try:
        # 目录在前、同类按名字排：与右侧文件面板（preview_service.list_children）同序
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (PermissionError, OSError) as exc:
        raise HostError(ERR_INTERNAL, f"列目录失败：{exc}") from exc
    entries: list[dict[str, Any]] = []
    for item in children[:FS_LIST_MAX_ENTRIES]:
        is_dir = item.is_dir()
        try:
            size = 0 if is_dir else item.stat().st_size
        except OSError:
            size = 0  # 单条 stat 失败不该让整次列举失败
        entries.append({"name": item.name, "type": "dir" if is_dir else "file", "size": size})
    return {"entries": entries, "truncated": len(children) > FS_LIST_MAX_ENTRIES}


async def _exec_start(args: dict[str, Any], ctx: _OpContext) -> dict[str, Any]:
    """起进程，不等待完成，立即返回 `{ streamId }`（契约 §4「执行语义」）。

    直接复用 `TerminalExecTool` 的 `waitForCompletion=false` 分支：shell 解析、`cd 前缀`
    处理、`cwd` 归一、后台注册、环形缓冲、整树终止全部沿用，本函数只接管输出去向。

    `stdin_pipe=True` 是本 op 与 AI 侧 `terminal_exec` **唯一**的差别（v3 `exec.stdin`
    的前置条件）：页面要用 `exec.stdin` 往进程里写数据，就得拿到写端句柄。
    AI 侧默认 `False` 不变——等待输入的命令在那里必须「立刻 EOF」而不是永久挂起。
    """
    from app.orchestration.tools.base import ToolContext
    from app.orchestration.tools.terminal import TerminalExecTool

    command = str(args.get("command") or "").strip()
    if not command:
        raise HostError(ERR_BAD_REQUEST, "exec.start 缺少 command")
    cwd = args.get("cwd")
    tool_ctx = ToolContext(
        workspace_root=await session_workspace(ctx.session_id),
        session_id=ctx.session_id, task_id=0, agent_id=0, agent_name="microapp-host",
    )
    result = await TerminalExecTool(stdin_pipe=True).run(
        {"command": command, "cwd": cwd, "waitForCompletion": False}, tool_ctx,
    )
    if not result.ok:
        raise HostError(ERR_EXEC_FAILED, result.error or "起进程失败")
    stream_id = str((result.data or {}).get("shell_id") or "")
    if not stream_id:
        raise HostError(ERR_INTERNAL, "后台命令未返回 shell_id")

    # 页面发起的命令不唤醒 AI 会话：注册表默认会在退出时把「[后台命令完成]」投递进会话
    # （bg_notify），而这条命令不是 AI 起的，凭空叫醒一轮会让用户莫名其妙。
    # mark_observed 是注册表现成的「不必回报」标记（terminal_bg_status 读到终态时也用同一个）。
    bg_process_registry.mark_observed(stream_id)

    pump = StreamPump(stream_id, ctx.uid, ctx.send)
    _PUMPS[stream_id] = pump
    pump.start()
    out: dict[str, Any] = {"streamId": stream_id}
    # v3：stdin 可写就是本 op 的既有能力，把能力点回报给页面，省得它先调一次
    # host.capabilities 才知道这个 stream 能不能 exec.stdin（老宿主没有这个字段）。
    out["stdin"] = True
    if args.get("env") not in (None, {}, ""):
        # 契约列了 env?，但复用的 terminal_exec 通道不注入 env（AI 侧同样没有这个参数）。
        # 明说不生效，好过让页面以为设上了。
        out["warnings"] = ["env 暂不生效：v3 仍复用 terminal_exec 通道，不支持自定义环境变量"]
    return out


async def _exec_stdin(args: dict[str, Any], ctx: _OpContext) -> dict[str, Any]:
    """写入进程 stdin / 关闭 stdin（设计文档 `12-capability-boundaries` §4.1）。

    契约要点：
    - `data` 按 UTF-8 写入，**调用方自己负责换行**（宿主既不加也不猜 `\\n`）；
    - `eof: true` 关闭 stdin（进程读到 EOF 后自行退出，典型是 `cat` / REPL / 控制器进程）；
    - 返回 `{ ok: true, written: <字符数> }`——`written` 是**字符**数而非字节数，
      页面按它做进度提示时不必自己数。
    - 错误只有两种前缀：`microapp-host-stream-unknown`（不存在 / 不属于本会话）、
      `microapp-host-stdin-closed`（进程已退出 / 没有管道 / 管道已断）。
    """
    # 跨会话隔离：与 exec.stop / exec.status 同一套 owner 校验（bg_process_registry
    # .owner_session），只是错误码按 v3 契约统一成 stream-unknown（见 ERR_STREAM_UNKNOWN 注释）。
    stream_id = _checked_stream(args, ctx.session_id, code=ERR_STREAM_UNKNOWN)
    raw = args.get("data")
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    want_eof = bool(args.get("eof"))
    written = 0
    if text:
        try:
            stored = await bg_process_registry.write_stdin(stream_id, text.encode("utf-8"))
        except StdinClosedError as exc:
            # 底层 BrokenPipe / ConnectionReset 一律翻成 stdin-closed，不把原始异常抛给页面
            raise HostError(ERR_STDIN_CLOSED, str(exc)) from exc
        if stored is None:
            # 校验到这里之间进程条目被清理（会话被删 / 注册表裁剪）：按未知处理，不假装写成功
            raise HostError(ERR_STREAM_UNKNOWN, f"未知 streamId：{stream_id}")
        written = len(text)
    if want_eof:
        closed = await bg_process_registry.close_stdin(stream_id)
        if closed is None:
            raise HostError(ERR_STREAM_UNKNOWN, f"未知 streamId：{stream_id}")
        # closed=False（本次之前已 EOF 过 / 该进程本来没有管道）按幂等成功处理：
        # 页面很可能在 stream.state exited 之后才收到用户点的「结束输入」，那不是错误。
    return {"ok": True, "written": written}


async def _exec_stop(args: dict[str, Any], ctx: _OpContext) -> dict[str, Any]:
    """终止进程树（复用注册表 `kill()` → `kill_process_tree`，与 terminal_bg_kill 同一条路）。"""
    stream_id = _checked_stream(args, ctx.session_id)
    # by="user"：这是用户在页面上按的停止，与右上角「后台任务」里手动停止同一语义
    # （页面发起的命令已 mark_observed，因此不会反过来唤醒 AI 会话）
    killed = await bg_process_registry.kill(stream_id, by="user")
    if killed is None:
        raise HostError(ERR_STREAM_NOT_FOUND, f"未知 streamId：{stream_id}")
    return {"stopped": True, "returncode": killed.get("returncode"),
            "running": bool(killed.get("running"))}


async def _exec_status(args: dict[str, Any], ctx: _OpContext) -> dict[str, Any]:
    """兜底轮询：返回 `{ state, code, startedAt }`（契约 §4）。"""
    stream_id = _checked_stream(args, ctx.session_id)
    info = bg_process_registry.status(stream_id, 0)
    if info is None:
        raise HostError(ERR_STREAM_NOT_FOUND, f"未知 streamId：{stream_id}（服务重启后记录会失效）")
    return {
        "state": "running" if info["running"] else "exited",
        "code": info.get("returncode"),
        "startedAt": info.get("started_at"),
    }


async def _fs_read_op(args: dict[str, Any], ctx: _OpContext) -> dict[str, Any]:
    """`fs.read` 的 op 外壳：会话工作区解析 + 复用纯函数 `fs_read`。"""
    return fs_read(await session_workspace(ctx.session_id), args)


async def _fs_list_op(args: dict[str, Any], ctx: _OpContext) -> dict[str, Any]:
    """`fs.list` 的 op 外壳：会话工作区解析 + 复用纯函数 `fs_list`。"""
    return fs_list(await session_workspace(ctx.session_id), args)


async def _workspace_info_op(args: dict[str, Any], ctx: _OpContext) -> dict[str, Any]:
    """`workspace.info`：页面拿工作区根（拼 `fs.*` 相对路径、显示项目名都要用）。"""
    return {"root": await session_workspace(ctx.session_id), "sessionId": ctx.session_id}


async def _host_capabilities_op(args: dict[str, Any], ctx: _OpContext) -> dict[str, Any]:
    """能力自描述（设计文档 `12-capability-boundaries` §4.2）。

    页面启动时读一次并据此决定显示哪些控件：`features.stdin === false` 时把「交互输入」
    置灰并说明原因，而不是给一个点了没反应的按钮。AI 生成微应用前也照它核对边界。
    """
    return {
        "protocol": PROTOCOL_VERSION,
        # ops 直接由分派表生成——手写第二份清单必然会随迭代漂移（加了 op 忘了登记）。
        "ops": list(OP_HANDLERS),
        # net 恒为 False：页面跑在 `app://` 下、CSP `connect-src 'none'`，唯一对外通道是
        # 宿主桥（设计文档 §5「放宽 CSP / socket 转发」属明确不做项）。
        "features": {"stdin": True, "env": False, "net": False},
        "limits": {
            # 并发上限尚无实现（设计文档 §7 backlog）：显式给 null 表示「宿主不限制」，
            # 而不是省略字段让页面以为宿不认识这个键。
            "maxConcurrentExec": None,
            # 与 AI 侧 microapp_call 内联返回值同一口径（tools/microapp.py 的 4000 字符），
            # 页面按它预估「一次调用最多能回多少内容」。fs.read 另有 512KB 单次上限
            # （FS_READ_MAX_BYTES），流式日志不逐条截断（见 bg_process.poll 注释）。
            "maxOutputCharsPerCall": MICROAPP_RESULT_MAX_CHARS,
        },
    }


def _checked_stream(args: dict[str, Any], session_id: int,
                    code: str = ERR_STREAM_NOT_FOUND) -> str:
    """取 streamId 并校验归属：一个会话的页面不能停/查/写另一个会话的进程。

    `code` 供不同 op 选错误码：v2 的 `exec.stop/status` 沿用 `stream-not-found`（已发布的
    契约不改），v3 的 `exec.stdin` 用 `stream-unknown`（设计文档 §4.1 把「不存在」与
    「不属于本会话」合并成同一个码）。
    """
    stream_id = str(args.get("streamId") or "").strip()
    if not stream_id:
        raise HostError(ERR_BAD_REQUEST, "缺少 streamId")
    owner = bg_process_registry.owner_session(stream_id)
    if owner is not None and int(owner) != int(session_id):
        logger.warning("[microapp-host] 拒绝跨会话操作 stream=%s owner=%s 请求会话=%s",
                       stream_id, owner, session_id)
        raise HostError(code, f"未知 streamId：{stream_id}")
    return stream_id


# op 分派表：**唯一事实来源**。`host.capabilities` 的 ops 由它生成，`_dispatch` 由它查表，
# 因此「加了 handler 忘了登记」只会表现为页面调不到（并且 capabilities 里也不会有），
# 不会再出现「清单写了 op、实际没实现」这类漂移。
OP_HANDLERS: dict[str, Callable[[dict[str, Any], _OpContext], Any]] = {
    "exec.start": _exec_start,
    "exec.stdin": _exec_stdin,
    "exec.stop": _exec_stop,
    "exec.status": _exec_status,
    "fs.read": _fs_read_op,
    "fs.list": _fs_list_op,
    "workspace.info": _workspace_info_op,
    "host.capabilities": _host_capabilities_op,
}


async def _dispatch(op: str, args: dict[str, Any], *, uid: str, session_id: int,
                    send: Callable[[dict], None]) -> dict[str, Any]:
    handler = OP_HANDLERS.get(op)
    if handler is None:
        raise HostError(
            ERR_UNKNOWN_OP,
            f"未知 op：{op!r}（已实现：{', '.join(OP_HANDLERS)}；"
            "可先调 host.capabilities 取一份权威清单）",
        )
    return await handler(args, _OpContext(uid=uid, session_id=session_id, send=send))


# ───────────────────────── 入口 ─────────────────────────


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# 在途请求任务的强引用，避免被 GC 提前回收（与 BgProcessRegistry._tasks 同写法）
_REQ_TASKS: set[asyncio.Task] = set()


def spawn_request(payload: dict[str, Any], send: Callable[[dict], None]) -> asyncio.Task | None:
    """在后台跑一条请求，立即把控制权还给 WS 接收循环。

    全局通道的接收循环同时承担浏览器宿主心跳与 `sync` 等既有职责：`exec.start` 要起进程、
    `fs.read` 要读盘，同步等待会把这些一并堵住。页面侧按 `reqId` 配对，乱序回包不影响语义。
    """
    try:
        task = asyncio.get_running_loop().create_task(handle_request(payload, send))
    except RuntimeError:  # 无事件循环（关停路径）：丢弃，事后可从审计日志看出请求到达过
        logger.debug("[microapp-host] 无事件循环，丢弃请求 op=%s", payload.get("op"))
        return None
    _REQ_TASKS.add(task)
    task.add_done_callback(_REQ_TASKS.discard)
    return task


async def handle_request(payload: dict[str, Any], send: Callable[[dict], None]) -> None:
    """处理一条 `microapp.host.request`：执行 → 回 `microapp.host.response` → 写审计。

    `send(event_dict)` 由网关注入（同一条 WS 连接），流式推送也走它——这样本模块不需要
    知道连接管理细节，也便于单测直接收集事件。
    """
    started = time.monotonic()
    req_id = _text(payload.get("reqId"))
    uid = _text(payload.get("uid"))
    app_id = _text(payload.get("appId"))
    version = _text(payload.get("version"))
    op = _text(payload.get("op"))
    raw_args = payload.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    try:
        session_id = int(payload.get("sessionId") or 0)
    except (TypeError, ValueError):
        session_id = 0

    def _error(code: str, detail: str) -> None:
        send({
            "event": EVENT_RESPONSE,
            "payload": {"reqId": req_id, "uid": uid, "ok": False,
                        "reason": code, "message": f"{code}: {detail}"},
        })

    ok = False
    error_text: str | None = None
    try:
        if not req_id:
            raise HostError(ERR_BAD_REQUEST, "缺少 reqId，无法配对回执")
        if not op:
            raise HostError(ERR_BAD_REQUEST, "缺少 op")
        if session_id <= 0:
            raise HostError(ERR_BAD_REQUEST, "缺少 sessionId（渲染层应按标签所属会话桶填入）")
        result = await _dispatch(op, args, uid=uid, session_id=session_id, send=send)
        ok = True
        send({"event": EVENT_RESPONSE,
              "payload": {"reqId": req_id, "uid": uid, "ok": True, "result": result}})
    except HostError as exc:
        error_text = exc.code
        _error(exc.code, exc.detail)
        logger.info("[microapp-host] %s 失败 op=%s app=%s:%s uid=%s session=%s: %s",
                    exc.code, op, app_id, version, uid, session_id, exc.detail)
    except Exception as exc:  # noqa: BLE001 — 任何异常都要变成有配对关系的回执，不能让页面干等
        error_text = ERR_INTERNAL
        _error(ERR_INTERNAL, f"{type(exc).__name__}: {exc}")
        logger.warning("[microapp-host] op=%s 处理异常", op, exc_info=True)
    finally:
        await write_audit(build_audit_record(
            app_id=app_id, version=version, uid=uid, session_id=session_id, op=op,
            args=args, ok=ok, duration_ms=int((time.monotonic() - started) * 1000),
            error=error_text,
        ))

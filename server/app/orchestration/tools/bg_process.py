"""v1.0 (plan-153-705): 后台进程注册表 + terminal_bg_status / terminal_bg_kill 工具。

terminal_exec 以 waitForCompletion=false 启动的长驻进程（dev server、watch、
后端服务）注册到本模块的内存单例注册表：
- 输出持续收集到环形缓冲（64KB/进程），供 AI 增量读取（offset 按字符序号）；
- 进程退出后条目保留（状态=exited + 退出码 + 尾部日志），供事后查询；
- 生命周期独立于 turn/审批（dev server 需跨 turn 存活），不随 cancel_event 终止。

不落库：进程句柄不可序列化，服务重启后自然失效，查询时报「已退出/未知」。
"""
import asyncio
import logging
import secrets
import time
from collections import deque
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# 环形输出缓冲上限（字符/进程）：防长驻进程无限累积日志撑爆内存
_MAX_BUFFER_CHARS = 64 * 1024
# 单次 status 调用返回的日志上限（字符）：与 terminal_exec 输出上限同量级
_MAX_STATUS_LOG_CHARS = 12_000


def decode_output(data: bytes) -> str:
    """优先 UTF-8，失败回退 GBK：cmd/PowerShell 在中文 Windows 上输出 GBK，
    直接按 UTF-8 解码会得到乱码（如 "'Get-ChildItem' 不是内部或外部命令" 变问号）。"""
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


async def kill_process_tree(proc: asyncio.subprocess.Process, timeout: float = 15) -> None:
    """终止进程及其全部子进程，并等待句柄回收。

    Windows 上 proc.kill() 只终止 shell 本身——孤儿子进程（如 pwsh 派生的
    python/npm）持有 stdout 管道，proc.wait() 会一直挂到子进程自然退出
    （实测 sleep 60 的子进程让 wait() 挂 57s）。taskkill /T /F 整树终止后
    wait() 立即返回。POSIX 上进程组语义下 proc.kill() 足够。
    """
    import sys
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=timeout)
        else:
            proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (ProcessLookupError, OSError):
        pass  # 进程已自行退出
    except asyncio.TimeoutError:
        logger.warning("[kill_timeout] pid=%s 整树终止未在 %.0fs 内完成", proc.pid, timeout)


class _BgEntry:
    """单个后台进程的注册条目。"""

    __slots__ = ("shell_id", "proc", "command", "cwd", "session_id",
                 "started_at", "buffer", "total_chars", "returncode", "finished_at")

    def __init__(self, shell_id: str, proc: asyncio.subprocess.Process,
                 command: str, cwd: str | None, session_id: int) -> None:
        self.shell_id = shell_id
        self.proc = proc
        self.command = command
        self.cwd = cwd or ""
        self.session_id = session_id
        self.started_at = time.time()
        # deque 按字符块环形截断：total_chars 记录累计产出量（offset 基准）
        self.buffer: deque[str] = deque()
        self.total_chars = 0
        self.returncode: int | None = None
        self.finished_at: float | None = None

    def append_output(self, text: str) -> None:
        if not text:
            return
        self.buffer.append(text)
        self.total_chars += len(text)
        # 环形截断：超限后从头部丢弃整块，直到回到上限内
        dropped = 0
        while self.total_chars - dropped > _MAX_BUFFER_CHARS and len(self.buffer) > 1:
            dropped += len(self.buffer[0])
            self.buffer.popleft()
        if dropped:
            self.total_chars -= dropped

    def read_from(self, offset: int) -> tuple[str, int]:
        """从累计字符序号 offset 起读取（缓冲已丢弃的部分返回空并校准 offset）。"""
        if offset < 0:
            offset = 0
        # 缓冲头部对应的累计序号起点
        head_start = self.total_chars - sum(len(c) for c in self.buffer)
        if offset < head_start:
            offset = head_start
        if offset >= self.total_chars:
            return "", self.total_chars
        skip = offset - head_start
        chunks: list[str] = []
        for chunk in self.buffer:
            if skip >= len(chunk):
                skip -= len(chunk)
                continue
            chunks.append(chunk[skip:] if skip else chunk)
            skip = 0
        text = "".join(chunks)
        return text, self.total_chars


class BgProcessRegistry:
    """后台进程注册表（模块级单例，见模块 docstring 生命周期说明）。"""

    def __init__(self) -> None:
        self._entries: dict[str, _BgEntry] = {}

    def register(self, proc: asyncio.subprocess.Process, command: str,
                 cwd: str | None, session_id: int) -> str:
        """注册后台进程并启动输出收集任务，返回 shell_id。"""
        shell_id = f"bg_{secrets.token_hex(4)}"
        entry = _BgEntry(shell_id, proc, command, cwd, session_id)
        self._entries[shell_id] = entry
        asyncio.get_running_loop().create_task(self._collect(entry))
        logger.info(
            "[bg.start] shell_id=%s pid=%s cmd=%r cwd=%s session=%s",
            shell_id, proc.pid, command[:200], cwd, session_id,
        )
        return shell_id

    async def _collect(self, entry: _BgEntry) -> None:
        """持续流式读取 stdout/stderr 到环形缓冲，进程退出后记录退出码。

        不能用 communicate()——它会缓冲全部输出直到进程退出，运行中的进程
        查不到增量日志。逐行 readline 读取：换行符不会出现在 UTF-8/GBK
        多字节序列中间，按行解码不会产生乱码。
        """
        proc = entry.proc

        async def _drain(stream, is_stderr: bool) -> None:
            if stream is None:
                return
            first_chunk = is_stderr
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = decode_output(line)
                if not text:
                    continue
                if first_chunk:
                    entry.append_output("\n-- stderr --\n")
                    first_chunk = False
                entry.append_output(text)

        try:
            await asyncio.gather(_drain(proc.stdout, False), _drain(proc.stderr, True))
            entry.returncode = await proc.wait()
        except Exception as exc:  # noqa: BLE001 - 收集任务失败不能影响主流程
            logger.warning("[bg.collect_error] shell_id=%s exc=%s", entry.shell_id, exc)
            entry.append_output(f"\n[输出收集异常] {type(exc).__name__}: {exc}\n")
            try:
                entry.returncode = proc.returncode if proc.returncode is not None else await proc.wait()
            except Exception:  # noqa: BLE001 - 兜底标记异常退出
                entry.returncode = -1
        entry.finished_at = time.time()
        logger.info(
            "[bg.exit] shell_id=%s rc=%s elapsed=%.1fs cmd=%r",
            entry.shell_id, entry.returncode,
            entry.finished_at - entry.started_at, entry.command[:200],
        )

    def status(self, shell_id: str, offset: int = 0) -> dict[str, Any] | None:
        """查询状态与增量日志。返回 None 表示 shell_id 未知。"""
        entry = self._entries.get(shell_id)
        if entry is None:
            return None
        text, next_offset = entry.read_from(offset)
        if len(text) > _MAX_STATUS_LOG_CHARS:
            text = text[:_MAX_STATUS_LOG_CHARS] + "\n...(已截断，可用更大 offset 跳过)"
        return {
            "shell_id": entry.shell_id,
            "running": entry.returncode is None,
            "returncode": entry.returncode,
            "command": entry.command,
            "cwd": entry.cwd,
            "started_at": entry.started_at,
            "finished_at": entry.finished_at,
            "log": text,
            "next_offset": next_offset,
        }

    async def kill(self, shell_id: str) -> dict[str, Any] | None:
        """终止后台进程树。返回 None 表示 shell_id 未知。"""
        entry = self._entries.get(shell_id)
        if entry is None:
            return None
        if entry.returncode is not None:
            return {"shell_id": shell_id, "killed": False,
                    "running": False, "returncode": entry.returncode,
                    "note": "进程已退出，无需终止"}
        proc = entry.proc
        await kill_process_tree(proc)
        killed = entry.returncode is not None or proc.returncode is not None
        logger.info("[bg.kill] shell_id=%s pid=%s killed=%s", shell_id, proc.pid, killed)
        return {"shell_id": shell_id, "killed": killed,
                "running": entry.returncode is None and proc.returncode is None,
                "returncode": entry.returncode if entry.returncode is not None else proc.returncode}


# 模块级单例（对齐 browser.py _BrowserSessionManager 模式）
bg_process_registry = BgProcessRegistry()


class TerminalBgStatusTool(Tool):
    """查询后台进程状态与增量日志（waitForCompletion=false 启动的命令）。"""

    name = "terminal_bg_status"
    risk_level = "low"
    description = (
        "查询后台命令的运行状态与增量日志。shell_id 来自 terminal_exec 以 "
        "waitForCompletion=false 启动命令时的返回。offset 传上次返回的 next_offset "
        "可增量读取新日志；首次查询传 0（或不传）。"
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
                        "shell_id": {"type": "string", "description": "后台命令标识（bg_ 开头）"},
                        "offset": {"type": "integer", "description": "日志起始字符偏移（默认 0，增量读取传上次 next_offset）"},
                    },
                    "required": ["shell_id"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        shell_id = str(args.get("shell_id", "")).strip()
        if not shell_id:
            return ToolResult(ok=False, output="", error="shell_id 为空")
        try:
            offset = int(args.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        info = bg_process_registry.status(shell_id, offset)
        if info is None:
            return ToolResult(ok=False, output="", error=f"未知 shell_id: {shell_id}（服务重启后后台进程记录会失效）")
        lines = [
            f"shell_id: {info['shell_id']}",
            f"状态: {'运行中' if info['running'] else '已退出'}"
            + (f"（退出码 {info['returncode']}）" if not info["running"] else ""),
            f"命令: {info['command']}",
            f"工作目录: {info['cwd'] or '(工作区根)'}",
        ]
        log = info["log"]
        if log:
            lines.append(f"-- 日志(offset {offset} → {info['next_offset']}) --")
            lines.append(log)
        else:
            lines.append(f"-- 暂无新日志（next_offset={info['next_offset']}）--")
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            data=info,
        )


class TerminalBgKillTool(Tool):
    """终止后台命令进程树。"""

    name = "terminal_bg_kill"
    risk_level = "low"
    description = "终止一个后台命令（及其全部子进程）。shell_id 来自 terminal_exec 以 waitForCompletion=false 启动命令时的返回。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "shell_id": {"type": "string", "description": "后台命令标识（bg_ 开头）"},
                    },
                    "required": ["shell_id"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        shell_id = str(args.get("shell_id", "")).strip()
        if not shell_id:
            return ToolResult(ok=False, output="", error="shell_id 为空")
        info = await bg_process_registry.kill(shell_id)
        if info is None:
            return ToolResult(ok=False, output="", error=f"未知 shell_id: {shell_id}（服务重启后后台进程记录会失效）")
        if info.get("killed") or info.get("returncode") is not None:
            return ToolResult(
                ok=True,
                output=f"已终止 {shell_id}（退出码 {info.get('returncode')}）",
                data=info,
            )
        return ToolResult(
            ok=False,
            output="",
            error=f"终止 {shell_id} 失败（进程仍在运行），可重试或检查进程状态",
            data=info,
        )

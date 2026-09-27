"""内置桌面操控内核（desktop-core.exe）的定位与进程管理。

为什么是常驻子进程：本机实测常驻管道往返约 0.01-0.03 ms，而每次新起进程要 30 ms（C# exe）
到 430 ms（PowerShell）。后端是长期运行的 FastAPI 进程，把内核挂成它的子进程即可把这项
固定开销彻底消掉——这正是它相对「每次 spawn 一个 stdio MCP」的关键优势。

定位逻辑与 browser_env.py 同构：
- 源码运行：tools/desktop-core/bin/desktop-core.exe
- 打包运行：<_MEIPASS>/desktop-core/desktop-core.exe（spec 的 datas 目标目录）

环境变量覆盖：CHATCODER_DESKTOP_CORE_EXE 可指定自定义内核路径（便于开发与调试）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 与 chatcoder-server.spec 中 datas 的目标目录保持一致
_BUNDLED_DIR_NAME = "desktop-core"
_EXE_NAME = "desktop-core.exe"
_PIPE_NAME = "chatcoder_desktop_core"


def _pipe_name() -> str:
    """内核管道名（带本进程后缀）。

    为什么要按进程区分：打包版与源码版（或两个开发实例）同时运行时，固定管道名会冲突——
    后启动的实例里内核直接退出并报「cannot create pipe: 所有的管道范例都在使用中」，
    该实例的电脑操控整体不可用（实测遇到的坑）。按进程后缀命名后，两套实例各自持有内核，互不干扰。
    """
    return f"{_PIPE_NAME}_{os.getpid()}"

# 单次调用超时：截图在慢机器上可达数百毫秒，给出足够余量
_CALL_TIMEOUT_SEC = 30.0
# 启动后等待 ready 行的上限
_READY_TIMEOUT_SEC = 10.0

_core_path_cache: str | None = None
_core_path_resolved = False


def _candidate_paths() -> list[Path]:
    """内核可执行文件的候选位置（按优先级）。"""
    candidates: list[Path] = []

    env = os.environ.get("CHATCODER_DESKTOP_CORE_EXE")
    if env:
        candidates.append(Path(env))

    if getattr(sys, "frozen", False):
        # onedir 模式：spec 的 datas 落在 _internal/（sys._MEIPASS 即该目录）
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / _BUNDLED_DIR_NAME / _EXE_NAME)
        # 兜底：exe 同级目录，便于现场手工放置 / 调试
        candidates.append(Path(sys.executable).resolve().parent / _BUNDLED_DIR_NAME / _EXE_NAME)
    else:
        # 源码运行：<repo>/tools/desktop-core/bin/desktop-core.exe
        # app/core/desktop_env.py → parents[2] = server → parent = <repo>
        repo_root = Path(__file__).resolve().parents[2].parent
        candidates.append(repo_root / "tools" / _BUNDLED_DIR_NAME / "bin" / _EXE_NAME)

    return candidates


def resolve_core_path() -> str | None:
    """返回内核 exe 的绝对路径；未找到则 None。"""
    global _core_path_cache, _core_path_resolved
    if _core_path_resolved:
        return _core_path_cache
    _core_path_resolved = True
    for candidate in _candidate_paths():
        try:
            if candidate.is_file():
                _core_path_cache = str(candidate)
                return _core_path_cache
        except OSError:
            continue
    return None


def reset_cache() -> None:
    """清除路径解析缓存（测试用）。"""
    global _core_path_cache, _core_path_resolved
    _core_path_cache = None
    _core_path_resolved = False


class DesktopCoreError(RuntimeError):
    """内核不可用或调用失败。消息直接面向用户展示。"""


class DesktopCore:
    """常驻内核的进程封装：懒启动、持久管道、崩溃自动重建。

    使用方式（并发安全，内部串行化）：
        core = get_desktop_core()
        data = await core.call("windows", {"limit": 10})
    """

    def __init__(self, exe_path: str, pipe_name: str | None = None) -> None:
        self._exe = exe_path
        # 默认按进程生成唯一管道名（见 _pipe_name）：多实例并存时互不干扰
        self._pipe = pipe_name or _pipe_name()
        self._proc: asyncio.subprocess.Process | None = None
        self._transport: asyncio.ReadTransport | None = None
        self._reader: asyncio.StreamReader | None = None
        self._lock = asyncio.Lock()
        self._starting = False

    # ── 生命周期 ──────────────────────────────────────────────

    async def _ensure_started(self) -> None:
        """确保内核进程与连接就绪。"""
        if self._proc is not None and self._proc.returncode is None and self._transport is not None:
            return
        if self._starting:
            return
        self._starting = True
        try:
            await self._spawn()
        finally:
            self._starting = False

    async def _spawn(self) -> None:
        await self._teardown()

        try:
            proc = await asyncio.create_subprocess_exec(
                self._exe, "serve", self._pipe,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise DesktopCoreError(
                f"桌面操控内核不存在：{self._exe}。请先运行 tools/desktop-core/build.ps1 编译。"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise DesktopCoreError(f"桌面操控内核启动失败：{exc}") from exc

        # 内核启动后会在 stdout 回一行 ready JSON，等到它再连管道，避免竞态。
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=_READY_TIMEOUT_SEC)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise DesktopCoreError("桌面操控内核启动超时（未收到就绪信号）。") from exc

        if not raw:
            stderr = b""
            try:
                stderr = await proc.stderr.read()
            except Exception:  # noqa: BLE001
                pass
            raise DesktopCoreError(
                "桌面操控内核启动即退出：" + stderr.decode("utf-8", errors="replace")[:300]
            )

        try:
            ready = json.loads(raw.decode("utf-8", errors="replace").strip())
        except json.JSONDecodeError:
            proc.kill()
            raise DesktopCoreError("桌面操控内核就绪信号无法解析。")

        if not ready.get("ready"):
            proc.kill()
            raise DesktopCoreError("桌面操控内核未报告就绪。")

        # 连接命名管道。
        # 注意：asyncio.open_connection 不支持 Windows 命名管道——它按 socket 语义解析，
        # 会直接报 getaddrinfo failed。必须用事件循环的 create_pipe_connection。
        try:
            transport, reader = await asyncio.wait_for(
                self._open_pipe(), timeout=_READY_TIMEOUT_SEC
            )
        except Exception as exc:  # noqa: BLE001
            proc.kill()
            raise DesktopCoreError(f"无法连接桌面操控内核管道：{exc}") from exc

        self._proc = proc
        self._transport = transport
        self._reader = reader
        logger.info("[desktop] 内核已就绪 pid=%s screen=%s", ready.get("pid"), ready.get("screen"))

    async def _open_pipe(self):  # type: ignore[no-untyped-def]
        """在 Windows 上通过 ProactorEventLoop 连接命名管道，返回 (transport, StreamReader)。"""
        loop = asyncio.get_running_loop()
        pipe_path = rf"\\.\pipe\{self._pipe}"

        if not hasattr(loop, "create_pipe_connection"):
            raise DesktopCoreError(
                "当前事件循环不支持命名管道（Windows 上需要 ProactorEventLoop）。"
            )

        holder: dict = {}

        def factory():  # type: ignore[no-untyped-def]
            reader = asyncio.StreamReader(limit=1 << 20, loop=loop)
            holder["reader"] = reader
            return asyncio.StreamReaderProtocol(reader, loop=loop)

        transport, _protocol = await loop.create_pipe_connection(factory, pipe_path)  # type: ignore[attr-defined]
        return transport, holder["reader"]

    async def _teardown(self) -> None:
        """关闭连接并终止进程（幂等）。"""
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
            self._transport = None
        self._reader = None

        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass

    async def shutdown(self) -> None:
        """优雅停止：先请求内核自杀，再兜底强杀。"""
        if self._transport is not None and self._proc is not None and self._proc.returncode is None:
            try:
                self._transport.write(b'{"op":"shutdown"}\n')
            except Exception:  # noqa: BLE001
                pass
            proc = self._proc
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except Exception:  # noqa: BLE001
                    pass
        await self._teardown()

    # ── 调用 ──────────────────────────────────────────────────

    async def call(self, op: str, params: dict | None = None) -> dict:
        """调用一个 op，返回 data 部分。失败抛 DesktopCoreError。

        串行化：内核管道是一条连接一次请求-响应，并发调用会串行排队。
        正常一次调用在毫秒级，排队影响可忽略；并发写操作本来也不应并行。
        """
        async with self._lock:
            await self._ensure_started()
            assert self._transport is not None and self._reader is not None

            payload = {"op": op}
            if params:
                payload.update(params)
            line = json.dumps(payload, ensure_ascii=False) + "\n"

            try:
                self._transport.write(line.encode("utf-8"))
                raw = await asyncio.wait_for(self._reader.readline(), timeout=_CALL_TIMEOUT_SEC)
            except asyncio.TimeoutError as exc:
                raise DesktopCoreError(f"桌面操控调用超时（{op}）。") from exc
            except (ConnectionError, OSError) as exc:
                # 管道断了：内核可能崩溃，清掉状态让下次调用重建
                await self._teardown()
                raise DesktopCoreError(f"桌面操控内核连接中断（{op}）：{exc}") from exc

            if not raw:
                await self._teardown()
                raise DesktopCoreError(f"桌面操控内核无响应（{op}），已重置连接。")

            try:
                resp = json.loads(raw.decode("utf-8", errors="replace").strip())
            except json.JSONDecodeError as exc:
                raise DesktopCoreError(f"桌面操控内核返回无法解析（{op}）。") from exc

            data = resp.get("data") or {}
            if isinstance(data, dict) and data.get("error"):
                raise DesktopCoreError(str(data["error"]))
            return data if isinstance(data, dict) else {"result": data}

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None


_core: DesktopCore | None = None


def get_desktop_core() -> DesktopCore:
    """取得全局内核单例（懒解析路径）。"""
    global _core
    if _core is None:
        exe = resolve_core_path()
        if not exe:
            raise DesktopCoreError(
                "桌面操控内核未找到。请在 tools/desktop-core 下运行 build.ps1 编译，"
                "或用 CHATCODER_DESKTOP_CORE_EXE 指定路径。"
            )
        _core = DesktopCore(exe)
    return _core


async def shutdown_desktop_core() -> None:
    """停止内核（应用关闭时调用）。"""
    global _core
    if _core is not None:
        await _core.shutdown()
        _core = None


def core_available() -> bool:
    """内核是否已就绪（不触发启动）。"""
    return resolve_core_path() is not None

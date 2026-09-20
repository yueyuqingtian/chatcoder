"""Arthas 会话宿主（内置 MCP「开发调试」的 Java 现场诊断通道）。

为什么需要这一层（沿用 debug_service 的架构约定）
------------------------------------------------
MCP 客户端 `mcp_wrapper._call_stdio` 每调用一个工具就 spawn 一个新进程、调完即 kill，
而 Arthas 是**长连接 + 跨调用状态**：一个 arthas-boot 进程 + 目标 JVM 内的一个
HTTP server（端口固定）+ 增强过的类。所以进程与端口必须由**主服务进程**持有：

    内置调试 MCP（子进程）
        └─ arthas_* 工具 --HTTP--> 主服务 /api/debug/arthas/*
                                        └─ 本模块：arthas-boot 进程 + HTTP 调用 + 作业
                                              └─ POST 127.0.0.1:<httpPort>/api （目标 JVM 内）

为什么是 Arthas 而不是 JDWP（方案 §1 的实测结论）
------------------------------------------------
IDEA 以 Debug 模式启动的 JVM，其 JDWP 通道已被 IDEA 独占，且 HotSpot 禁止运行时
动态加载 jdwp agent——第三方调试器无法并存。而 Arthas 走 **Attach API**（JVMTI），
与 JDWP 是两条独立通道，实测可与 IDEA 调试并存。因此：
- `java_debug_*`（JDWP）：真断点、单步、改变量，独占场景使用；
- `arthas_*`（本模块）：方法级观测 / 调用栈 / 类结构 / 线程，**IDEA 调试中也能用**。

与 IDEA 调试共存的注意点（写进工具描述，避免用户困惑）
------------------------------------------------------
`watch` / `trace` / `tt` 会对目标方法做 retransform，**被增强的方法上 IDEA 断点可能
失效**（需重新下断点）。因此 stop 前必先 `reset` 还原类。

Arthas 启动器获取（方案 §9 待确认项 1 的定稿）
--------------------------------------------
优先本地：配置项 > 缓存目录 > 联网下载到 `~/.chatcoder/arthas/` 并缓存。
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Arthas 端口段（方案 §9 待确认项 3：8563-8599）
_PORT_RANGE = range(8563, 8600)
# telnet 端口段：**必须给一个可用端口**——实测 arthas-boot 4.3.5 传 `--telnet-port -1`
# 直接报 `port out of range:-1`（它用 telnet 端口做“是否已 attach”的进程探测），
# 因此另开一段并同样绑 127.0.0.1（不对外暴露）。
_TELNET_RANGE = range(8660, 8700)
_DEFAULT_IDLE_TIMEOUT_S = 900        # 15 分钟无操作自动回收（方案 §5.3）
_ATTACH_READY_TIMEOUT_S = 60         # 首次 attach 需下载 arthas 全量包，给足时间
_MAX_SESSIONS = 4

_ARTHAS_BOOT_URL = "https://arthas.aliyun.com/arthas-boot.jar"
_BOOT_JAR_MIN_BYTES = 50_000         # 实测 4.3.5 为 148033 字节；过小=代理错误页

# 默认禁用命令（方案 §5.3；ognl 按用户确认放行——它是常用诊断能力，
# 只在 arthas_exec 上层标记 high 需审批）
_DISABLED_COMMANDS = "redefine,retransform,dump,heapdump,classloader"
_DANGEROUS_COMMANDS = {"redefine", "retransform", "dump", "heapdump", "classloader"}

# key = (chatcoder session_id, "arthas")
_sessions: dict[tuple[int, str], "ArthasSession"] = {}
_lock = asyncio.Lock()
_reaper: asyncio.Task | None = None


@dataclass
class ArthasSession:
    """一个 Arthas 会话：一个 arthas-boot 进程 + 目标 JVM 内的一个 HTTP server。"""
    session_id: int
    pid: int
    http_port: int
    java: str
    jar: str
    telnet_port: int = 0
    proc: Any = None                      # asyncio.subprocess.Process
    version: str | None = None
    main_class: str = ""
    attached_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    # 作业表：job_id -> {command, created_at}（watch/trace 等持续输出命令）
    jobs: dict[str, dict] = field(default_factory=dict)
    # arthas-boot 的输出尾部（attach 失败时用于回报关键行）
    output_tail: list[str] = field(default_factory=list)
    # Arthas 会话 id：一次性命令会自动建临时会话（响应顶层回显），
    # 但 async_exec / pull_results **必须**带它——否则 Arthas 返回
    # `state=FAILED, message="'sessionId' is required"`（实测）。
    arthas_session_id: str = ""
    # 服务端分配的 consumerId，同样要用于异步观测与结果拉取
    arthas_consumer_id: str = ""

    def touch(self) -> None:
        self.last_active = time.time()

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "attached": True,
            "pid": self.pid,
            "http_port": self.http_port,
            "version": self.version,
            "java": self.java,
            "jar": self.jar,
            "main_class": self.main_class,
            "attached_at": self.attached_at,
            "idle_seconds": int(time.time() - self.last_active),
            "active_jobs": len(self.jobs),
        }


def _key(session_id: int) -> tuple[int, str]:
    return (int(session_id), "arthas")


# ═══════════════════════════════════════════════════════════════
# 配置 / 环境探测
# ═══════════════════════════════════════════════════════════════

def _user_config() -> dict:
    """读用户配置（~/.chatcoder/config.json）；失败返回空 dict（不阻断功能）。"""
    try:
        path = Path(os.environ.get(
            "CHATCODER_USER_CONFIG", str(Path.home() / ".chatcoder" / "config.json")))
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError, ValueError):
        logger.debug("[arthas] 读取用户配置失败", exc_info=True)
    return {}


def _cfg(key: str) -> str:
    val = _user_config().get(key)
    return str(val).strip() if val else ""


def arthas_dir() -> Path:
    """Arthas 启动器缓存目录（与 ~/.chatcoder 下其他资源同级）。"""
    explicit = os.environ.get("CHATCODER_ARTHAS_DIR") or _cfg("arthas_dir")
    root = Path(explicit) if explicit else (Path.home() / ".chatcoder" / "arthas")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _exe(home: Path, name: str) -> Path | None:
    for cand in (home / "bin" / f"{name}.exe", home / "bin" / name):
        if cand.exists():
            return cand
    return None


def resolve_java_home(explicit: str | None = None) -> tuple[Path | None, str]:
    """解析 JDK 目录。返回 (java_home, 来源说明)。

    优先级（方案 §5.3）：显式参数 > 配置项 arthas_java_home > 环境变量
    CHATCODER_ARTHAS_JAVA_HOME > JAVA_HOME > PATH 中的 java。
    注意 Arthas 需要 **JDK**（要 jps/attach 能力），仅有 JRE 时会提示。
    """
    candidates: list[tuple[str, Path]] = []
    if explicit:
        candidates.append(("参数", Path(explicit)))
    cfg_home = _cfg("arthas_java_home")
    if cfg_home:
        candidates.append(("配置 arthas_java_home", Path(cfg_home)))
    env_home = os.environ.get("CHATCODER_ARTHAS_JAVA_HOME")
    if env_home:
        candidates.append(("环境变量 CHATCODER_ARTHAS_JAVA_HOME", Path(env_home)))
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(("环境变量 JAVA_HOME", Path(java_home)))
    which_java = shutil.which("java")
    if which_java:
        candidates.append(("PATH 中的 java", Path(which_java).resolve().parent.parent))

    for label, home in candidates:
        try:
            if home and home.is_dir() and _exe(home, "java"):
                return home, label
        except OSError:
            continue
    return None, "未找到"


def _jdk_error() -> str:
    return ("未找到可用的 JDK。Arthas 需要 JDK（依赖 jps / Attach API），"
            "请在设置中指定 arthas_java_home，或设置 JAVA_HOME 环境变量。")


def _find_jps(java_home: Path) -> Path | None:
    return _exe(java_home, "jps") or _exe(java_home, "jcmd")


# ═══════════════════════════════════════════════════════════════
# arthas-boot.jar 获取（本地优先 → 缓存 → 下载）
# ═══════════════════════════════════════════════════════════════

def _looks_like_jar(path: Path) -> bool:
    """jar 是 zip（PK 头）。防止把代理返回的错误页当 jar 用。"""
    try:
        with path.open("rb") as f:
            head = f.read(2)
        return head == b"PK" and path.stat().st_size > _BOOT_JAR_MIN_BYTES
    except OSError:
        return False


async def ensure_boot_jar() -> tuple[Path | None, str]:
    """确保拿到 arthas-boot.jar。返回 (路径, 来源说明)；失败时路径为 None。"""
    explicit = os.environ.get("CHATCODER_ARTHAS_JAR") or _cfg("arthas_boot_jar")
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p, "配置指定的本地 jar"
        logger.warning("[arthas] 配置的 arthas_boot_jar 不存在：%s，回退缓存/下载", p)

    cached = arthas_dir() / "arthas-boot.jar"
    if _looks_like_jar(cached):
        return cached, "本地缓存"

    err = await _download_boot_jar(cached)
    if err:
        return None, err
    return cached, "联网下载"


async def _download_boot_jar(target: Path) -> str | None:
    """下载 arthas-boot.jar 到缓存目录；成功返回 None，失败返回可读错误。

    走全局代理（设置面板的 HTTP 代理 → 环境变量），与 web 工具口径一致；
    代理配置在国内网络下往往是能否下载成功的关键。
    """
    from app.core.http_client import build_http_client

    tmp = target.with_suffix(".part")
    try:
        async with build_http_client(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(_ARTHAS_BOOT_URL)
            if resp.status_code >= 400:
                return (f"下载 arthas-boot.jar 失败：HTTP {resp.status_code}。"
                        f"可手动下载 {_ARTHAS_BOOT_URL} 后在设置中指定 arthas_boot_jar。")
            tmp.write_bytes(resp.content)
    except Exception as e:  # noqa: BLE001 —— 网络异常种类多，统一转成可读提示
        return (f"下载 arthas-boot.jar 失败：{e}。"
                f"请检查网络/代理设置，或手动下载 {_ARTHAS_BOOT_URL} 后指定 arthas_boot_jar。")
    finally:
        if tmp.exists() and not _looks_like_jar(tmp):
            tmp.unlink(missing_ok=True)
    if not _looks_like_jar(tmp):
        tmp.unlink(missing_ok=True)
        return ("下载到的 arthas-boot.jar 不是有效的 jar（可能是代理返回的错误页），"
                "请检查网络/代理，或手动指定 arthas_boot_jar。")
    tmp.replace(target)
    logger.info("[arthas] 已下载 arthas-boot.jar → %s", target)
    return None


# ═══════════════════════════════════════════════════════════════
# 进程枚举
# ═══════════════════════════════════════════════════════════════

async def _run_capture(cmd: list[str], timeout_s: float = 20.0) -> tuple[int, str, str]:
    """跑一条命令并捕获输出（超时杀进程）。"""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=_creation_flags(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        with _suppress():
            proc.kill()
        return -1, "", f"命令超时：{' '.join(cmd)}"
    return (proc.returncode or 0,
            (out or b"").decode("utf-8", errors="replace"),
            (err or b"").decode("utf-8", errors="replace"))


def _creation_flags() -> int:
    """Windows 下不弹控制台窗口（与 symbol_index_manager 的 worker 启动口径一致）。"""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _suppress:  # noqa: N801 —— 极简上下文管理器，避免为一次 kill 引入依赖
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True


_JDWP_RE = re.compile(r"address=([^,\s]+)")
_JDWP_LEGACY_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}:\d+")


def _parse_jps_line(line: str) -> dict | None:
    """解析 `jps -lvm` 的一行：`<pid> <mainClass> [args...]`。"""
    parts = line.strip().split()
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    pid = int(parts[0])
    main_class = parts[1]
    # jps 对“无主类”的进程（如 IDE 启动器）会把 JVM 参数当作第二列（-Xxx 开头）
    if main_class.startswith("-"):
        main_class = "(未知主类)"
        args = " ".join(parts[1:])
    else:
        args = " ".join(parts[2:])
    if main_class.endswith("Jps") or main_class.endswith("sun.tools.jps.Jps"):
        return None

    jdwp_addr = ""
    debugging = False
    tokens = parts[1:] if main_class.startswith("(") else parts[2:]
    for token in tokens:
        if token.startswith(("-agentlib:jdwp", "-Xrunjdwp")):
            debugging = True
            m = _JDWP_RE.search(token)
            if m:
                jdwp_addr = m.group(1)
            elif not jdwp_addr:
                m2 = _JDWP_LEGACY_RE.search(token)
                jdwp_addr = m2.group(0) if m2 else ""
        # IDEA 会额外挂 debugger-agent（即使 JDWP 参数被改写也能看出在调试）
        if "debugger-agent.jar" in token:
            debugging = True
    return {
        "pid": pid,
        "main_class": main_class,
        "jvm_args": args,
        "debugging": debugging,
        "jdwp_address": jdwp_addr,
        # 给模型/用户一个"这是不是我要的目标"的粗判信号
        "kind": ("spring-boot" if "spring" in args.lower() or "Application" in main_class
                 else "java"),
    }


async def list_processes(java_home: str | None = None) -> dict:
    """列出本机可 attach 的 JVM（`jps -lvm`），并标注是否处于调试中。"""
    home, source = resolve_java_home(java_home)
    if home is None:
        return {"ok": False, "error": _jdk_error(), "processes": []}
    jps = _find_jps(home)
    if jps is None:
        return {"ok": False, "error": f"在 {home} 下找不到 jps/jcmd（可能需要 JDK 而非 JRE）。",
                "processes": []}

    # jps 的输出列：pid mainClass [mainArgs] [jvmArgs]；-v 附带 JVM 参数
    code, out, err = await _run_capture([str(jps), "-lvm"], timeout_s=25)
    if code != 0 and not out.strip():
        return {"ok": False, "error": f"jps 执行失败：{err.strip() or code}",
                "processes": [], "java_home": str(home)}
    processes = [p for p in (_parse_jps_line(ln) for ln in out.splitlines()) if p]
    return {
        "ok": True,
        "processes": processes,
        "java_home": str(home),
        "java_home_source": source,
        "count": len(processes),
    }


def _port_free(port: int) -> bool:
    """端口是否空闲。

    注意：**不能**设 SO_REUSEADDR —— Windows 上该选项语义等同 SO_REUSEPORT，
    会让"已被占用的端口"探测为可用（多会话会抢同一个 HTTP 端口）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _allocate_port(preferred: int | None = None, ports: range = _PORT_RANGE) -> int | None:
    if preferred and _port_free(int(preferred)):
        return int(preferred)
    for p in ports:
        if _port_free(p):
            return p
    return None


# ═══════════════════════════════════════════════════════════════
# 广播（前端调试面板可见性的落点）
# ═══════════════════════════════════════════════════════════════

async def _broadcast(session_id: int, payload: dict) -> None:
    """把 Arthas 观测事件推给前端（失败不阻塞）。"""
    try:
        from app.orchestration.agent_events import broadcast
        await broadcast(session_id, {
            "event": "arthas.event",
            "payload": {"session_id": session_id, "at": int(time.time() * 1000), **payload},
        })
    except Exception:  # noqa: BLE001
        logger.debug("[arthas] 广播失败(非阻塞)", exc_info=True)


# ═══════════════════════════════════════════════════════════════
# attach / exec / pull / stop
# ═══════════════════════════════════════════════════════════════

async def _drain_output(sess: ArthasSession, stream: Any) -> None:
    """持续读取 arthas-boot 的输出，只留尾部若干行（attach 失败时回报关键行）。

    注意 stdout/stderr 已合并（stderr=STDOUT）：arthas-boot 的关键信息
    （`port out of range:-1`、`Attach process ... success`）是打在 **stdout** 上的，
    早期只接 stderr 会导致失败时“拿不到任何输出”。
    """
    try:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                sess.output_tail.append(text)
                del sess.output_tail[:-40]
    except (asyncio.CancelledError, OSError):
        return


async def _probe_agent(port: int) -> dict | None:
    """探测端口上是否有存活的 Arthas，并读出它服务的目标 JVM（pid / 主类）。

    为什么要读 pid：Arthas agent 常驻在**目标 JVM**里，chatcoder 异常退出时它不会
    随之消失。一旦残留 agent 占着端口，新 attach 会“成功”但 HTTP 起不来
    （目标 JVM 内的 Arthas 是单例，第二次 attach 会沿用旧端口）——实测就会看到一个
    60 秒空等后的失败。发现同 PID 的 agent 时直接复用，避免这个坑。

    pid 从哪来：Arthas 会话建立后 pull_results 的积压流里有一条 `welcome`
    （含 pid / mainClass / version），这是最可靠的关联手段。
    """
    from app.mcp_servers.arthas_client import ArthasClient

    try:
        client = ArthasClient(port, timeout_s=8.0)
        ver = await client.version()
        if not ver:
            return None
        got = await client.init_session()
        info: dict = {"port": port, "version": ver, "pid": None, "main_class": ""}
        sid, cid = got.get("session_id"), got.get("consumer_id")
        if sid:
            sess_client = ArthasClient(port, session_id=sid, consumer_id=cid, timeout_s=8.0)
            data = await sess_client.pull_results(timeout_s=8.0)
            for r in (data.get("results") or []):
                if isinstance(r, dict) and r.get("type") == "welcome":
                    raw_pid = str(r.get("pid") or "")
                    info["pid"] = int(raw_pid) if raw_pid.isdigit() else None
                    info["main_class"] = str(r.get("mainClass") or "")
            with _suppress():
                await sess_client.close_session()
        return info
    except Exception:  # noqa: BLE001 —— 目标端口可能被无关服务占用，任何异常都当“不是 Arthas”
        return None


async def _find_agent_for_pid(pid: int) -> dict | None:
    """在端口段里找**同一 PID** 的残留 Arthas agent（只探测已占用端口，代价可忽）。"""
    for p in _PORT_RANGE:
        if _port_free(p):
            continue
        info = await _probe_agent(p)
        if info and info.get("pid") == int(pid):
            logger.info("[arthas] 发现 PID %s 的已有 agent，复用端口 %s", pid, p)
            return info
    return None


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """公开入口的结构化兑底：任何未预期异常都转成 {"ok": False, "error": ...}。

    方案 §5.3 要求"不要抛裸异常给模型"：路由层拿不到 {"ok": False} 就会回 500，
    而 500 在 MCP 侧只能看到 "HTTP 500"（看不到原因），排查成本极高（实测踩过）。
    """
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.exception("[arthas] %s 执行异常", fn.__name__)
            return {"ok": False,
                    "error": f"Arthas 操作异常（{type(e).__name__}）：{e}"}

    return wrapper


@_safe
async def attach(session_id: int, pid: int, *, http_port: int | None = None,
                 java_home: str | None = None) -> dict:
    """按 **PID** attach 到目标 JVM（与 `java_debug_attach` 按 **JDWP 端口** 区分）。"""
    from app.mcp_servers.arthas_client import ArthasClient

    pid = int(pid or 0)
    if pid <= 0:
        return {"ok": False, "error": "需要有效的 Java 进程号 pid（先用 java_list_processes 获取）。"}

    home, source = resolve_java_home(java_home)
    if home is None:
        return {"ok": False, "error": _jdk_error()}
    java_exe = _exe(home, "java")
    if java_exe is None:
        return {"ok": False, "error": f"{home} 下找不到 java 可执行文件。"}

    jar, jar_source = await ensure_boot_jar()
    if jar is None:
        return {"ok": False, "error": jar_source}

    preferred = int(http_port) if http_port else None
    if preferred is None:
        cfg_port = _cfg("arthas_http_port")
        if cfg_port.isdigit():
            preferred = int(cfg_port)
    port = _allocate_port(preferred)
    if port is None:
        return {"ok": False, "error": f"端口段 {_PORT_RANGE.start}-{_PORT_RANGE.stop - 1} 内没有空闲端口。"}
    telnet_port = _allocate_port(ports=_TELNET_RANGE)
    if telnet_port is None:
        return {"ok": False,
                "error": f"telnet 端口段 {_TELNET_RANGE.start}-{_TELNET_RANGE.stop - 1} 内没有空闲端口。"}

    # 同会话已有 attach：先干净退出（避免多个 Arthas server 抢同一个 HTTP 端口）
    await stop(session_id, reason="reattach")

    # 目标 JVM 上可能有上次残留的 agent（chatcoder 异常退出时它不会随之消失）。
    # 此时新起 arthas-boot 会“attach 成功但 HTTP 端口起不来”，这里先找出来复用。
    adopted = await _find_agent_for_pid(pid)
    if adopted:
        sess = ArthasSession(session_id=int(session_id), pid=pid,
                             http_port=int(adopted["port"]), java=str(java_exe),
                             jar=str(jar), version=adopted.get("version"),
                             main_class=adopted.get("main_class") or "")
        _sessions[_key(session_id)] = sess
        await _broadcast(session_id, {
            "phase": "attached", "pid": pid, "http_port": sess.http_port,
            "version": sess.version,
            "summary": f"已复用既有 Arthas 会话（PID {pid}）· HTTP {sess.http_port}",
        })
        _ensure_reaper()
        return {**sess.to_dict(), "reused": True,
                "note": "该 JVM 上已存在 Arthas agent（多为上次未正常断开），已直接复用其端口"}

    mirror = _cfg("arthas_repo_mirror") or "aliyun"
    args = [
        str(java_exe), "-jar", str(jar), str(pid),
        "--attach-only",
        "--http-port", str(port),
        "--telnet-port", str(telnet_port),
        "--target-ip", "127.0.0.1",   # 两个端口都只绑本机，不对外暴露
        "--repo-mirror", mirror,      # 首次 attach 会拉全量包，国内走阿里云镜像
        "--disabled-commands", _DISABLED_COMMANDS,
    ]
    async with _lock:
        if len(_sessions) >= _MAX_SESSIONS:
            return {"ok": False,
                    "error": f"并发 Arthas 会话已达上限（{_MAX_SESSIONS}），请先 arthas_stop 释放。"}
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,   # 关键信息在 stdout，合并后才能回报
                creationflags=_creation_flags(),
            )
        except OSError as e:
            return {"ok": False, "error": f"启动 arthas-boot 失败：{e}"}

        sess = ArthasSession(session_id=int(session_id), pid=pid, http_port=port,
                             java=str(java_exe), jar=str(jar), telnet_port=telnet_port,
                             proc=proc)
        _sessions[_key(session_id)] = sess
        asyncio.create_task(_drain_output(sess, proc.stdout))

    client = ArthasClient(port, session_id="")
    deadline = time.monotonic() + _ATTACH_READY_TIMEOUT_S
    last_err = ""
    while time.monotonic() < deadline:
        try:
            ver = await client.version()
        except Exception as e:  # noqa: BLE001
            # 就绪探测必须容忍**任何**瞬时异常（连接被拒/重置、半就绪时接受连接但不回响应、
            # 超时……）——早期只捕 ArthasError，一次 RemoteProtocolError 就把 attach 打成 500。
            last_err = f"{type(e).__name__}: {e}"
            ver = None
        if ver:
            sess.version = ver
            sess.touch()
            await _broadcast(session_id, {
                "phase": "attached", "pid": pid, "http_port": port, "version": ver,
                "summary": f"已 attach（PID {pid}）· Arthas {ver} · HTTP {port}",
            })
            _ensure_reaper()
            return {**sess.to_dict(), "java_home": str(home), "java_home_source": source,
                    "jar_source": jar_source, "repo_mirror": mirror}
        if proc.returncode is not None and proc.returncode != 0:
            # `--attach-only` 下 attach 成功时 arthas-boot 会正常退出（returncode=0），
            # 真正常驻的是**目标 JVM 内的 Arthas server**；只有非 0 退出才是真失败。
            tail = "\n".join(sess.output_tail[-12:])
            _sessions.pop(_key(session_id), None)
            return {"ok": False, "error": _attach_failure_hint(pid, proc.returncode, tail)}
        await asyncio.sleep(1.0)

    tail = "\n".join(sess.output_tail[-12:])
    await _kill(sess)
    _sessions.pop(_key(session_id), None)
    return {"ok": False, "error": _attach_failure_hint(pid, None, tail, last_err)}


def _attach_failure_hint(pid: int, returncode: int | None, tail: str,
                         last_err: str = "") -> str:
    """attach 失败时给出**可操作**的排查提示（方案 §8：不要抛裸异常）。"""
    lines = [f"attach 到 PID {pid} 失败。"]
    if returncode is not None:
        lines.append(f"arthas-boot 已退出（returncode={returncode}）。")
    if tail:
        lines.append("arthas-boot 输出：\n" + tail)
    if last_err:
        lines.append(f"就绪探测：{last_err}")
    lower = tail.lower()
    if "not found" in lower or "no such" in lower:
        lines.append("提示：PID 可能已退出，请用 java_list_processes 重新确认。")
    if "disableattachmechanism" in lower or "attach" in lower and "refus" in lower:
        lines.append("提示：目标 JVM 可能带 -XX:+DisableAttachMechanism，或与当前用户权限不一致。")
    if "download" in lower or "connect" in lower or "timeout" in lower:
        lines.append("提示：首次使用需下载 Arthas 全量包，网络不通时可在设置中配置代理"
                     "或 arthas_dir 指向离线 Arthas 目录。")
    if "version" in lower and "unsupported" in lower:
        lines.append("提示：Arthas 4 支持 JDK 8+；JDK 6/7 需使用 Arthas 3。")
    lines.append("提示：若目标 JVM 正停在 IDEA 断点上（线程全部挂起），attach 会阻塞，"
                 "请先在 IDEA 里 resume 再重试。")
    return "\n".join(lines)


def get_session(session_id: int) -> ArthasSession | None:
    return _sessions.get(_key(session_id))


def _require(session_id: int) -> tuple[ArthasSession | None, dict | None]:
    sess = get_session(session_id)
    if sess is None:
        return None, {"ok": False,
                      "error": "尚未建立 Arthas 会话。请先 java_list_processes 找到 PID，"
                               "再 java_attach_process(pid) 建立会话。"}
    sess.touch()
    return sess, None


@_safe
async def exec_command(session_id: int, command: str, *, exec_timeout_ms: int = 10000,
                       timeout_s: float | None = None) -> dict:
    """执行一条 Arthas 命令；持续输出命令可先异步提交再 pull。"""
    from app.mcp_servers.arthas_client import ArthasError

    sess, err = _require(session_id)
    if err:
        return err
    assert sess is not None
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "command 不能为空。"}
    head = cmd.split()[0].lower()
    if head in _DANGEROUS_COMMANDS:
        return {"ok": False,
                "error": f"命令 {head} 被禁用（可能影响目标 JVM 稳定性）："
                         f"禁用清单 {_DISABLED_COMMANDS}。"}
    if head == "stop":
        return {"ok": False, "error": "请用 arthas_stop 断开（它会先 reset 还原被增强的类）。"}

    call_timeout = timeout_s or max(30.0, exec_timeout_ms / 1000 + 15)
    try:
        data = await _exec_with_client(sess, cmd, exec_timeout_ms, call_timeout)
    except ArthasError as e:
        return {"ok": False, "error": f"Arthas 不可用：{e}。可用 arthas_status 确认会话，"
                                      f"或 arthas_stop 后重新 attach。"}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("message") or data.get("state") or "执行失败",
                "command": cmd, "raw": data.get("raw")}
    res = _annotate(sess, cmd, data)
    # 同步观测（如 arthas_watch run_async=false）同样要把命中推给前端面板，
    # 否则用户只能看到"AI 说命中了"而面板一片空白（本次用户反馈的正是这个）。
    await _broadcast_observation(session_id, res)
    return res


async def _broadcast_observation(session_id: int, res: dict,
                                 job_id: str | int | None = None) -> None:
    """把观测结果推给前端面板：命中推 entries，未命中也推一条"窗口内无命中"。

    两种都推的原因：只推命中时，面板在"提交了观测但没命中"期间毫无反应，
    用户无法区分"AI 没在观测"与"观测了但没命中"。
    """
    entries = res.get("entries") or []
    jid = job_id if job_id is not None else res.get("job_id")
    if entries:
        await _broadcast(session_id, {
            "phase": "observed", "job_id": jid, "command": res.get("command"),
            "entries": entries, "time_expired": res.get("time_expired"),
            "summary": f"观测到 {len(entries)} 条命中：{res.get('command')}",
        })
    elif res.get("time_expired"):
        await _broadcast(session_id, {
            "phase": "no_hit", "job_id": jid, "command": res.get("command"),
            "time_expired": True,
            "summary": f"观测窗口内未命中：{res.get('command')}",
        })


async def _exec_with_client(sess: ArthasSession, cmd: str, exec_timeout_ms: int,
                            call_timeout: float) -> dict:
    """同步命令：**不**带会话 id。

    一次性命令即使不带 sessionId 也能跑（服务端自动建临时会话），而且
    不把自己的持久会话“污染”：否则命令结果会混进持久会话的积压流，
    后续 pull_results 会把它们当观测结果一并返回（实测）。
    """
    from app.mcp_servers.arthas_client import ArthasClient

    client = ArthasClient(sess.http_port)
    return await client.exec(cmd, exec_timeout_ms=exec_timeout_ms, timeout_s=call_timeout)


async def _ensure_arthas_session(sess: ArthasSession) -> tuple[str, str]:
    """保证持有 Arthas 会话 id + 服务端分配的 consumerId（异步观测的前置条件）。

    必须是 init_session 回显的那一对：临时会话下一跳失效，自编 consumerId
    会在 pull_results 报 `consumer not found`（均实测）。
    """
    from app.mcp_servers.arthas_client import ArthasClient

    if sess.arthas_session_id and sess.arthas_consumer_id:
        return sess.arthas_session_id, sess.arthas_consumer_id
    got = await ArthasClient(sess.http_port).init_session()
    if got.get("session_id"):
        sess.arthas_session_id = got["session_id"]
    if got.get("consumer_id"):
        sess.arthas_consumer_id = got["consumer_id"]
    return sess.arthas_session_id, sess.arthas_consumer_id


def _annotate(sess: ArthasSession, cmd: str, data: dict) -> dict:
    """把执行结果整理成前端/模型都能直接读的结构。"""
    from app.mcp_servers.arthas_client import render_results

    entries = _extract_entries(data.get("results"))
    job_id = data.get("job_id")
    if job_id:
        sess.jobs[str(job_id)] = {"command": cmd, "created_at": time.time()}
    return {
        "ok": True,
        "command": cmd,
        "text": render_results(data.get("results"),
                              time_expired=bool(data.get("time_expired")),
                              job_status=data.get("job_status")),
        "entries": entries,
        "job_id": job_id,
        "job_status": data.get("job_status"),
        "time_expired": bool(data.get("time_expired")),
        "results": data.get("results"),
    }


def _extract_entries(results: Any) -> list[dict]:
    """从命令结果里抽取 watch/trace 命中条目（供前端调试面板可视化）。"""
    out: list[dict] = []
    if not isinstance(results, list):
        return out

    def _walk(node: dict, kind: str) -> None:
        # 字段名以 HTTP API 实际输出为准：className / methodName / value；
        # 同时兼容命令行形态的 class / method / params / returnObj。
        out.append({
            "type": kind,
            "ts": node.get("ts"),
            "cost": node.get("cost"),
            "class": node.get("className") or node.get("class"),
            "method": node.get("methodName") or node.get("method"),
            "location": node.get("location"),
            "access_point": node.get("accessPoint"),
            # value：Arthas 渲染好的观测表达式结果（多行字符串）——面板直接展示它
            "value": _short(node.get("value"), 1200),
            "params": node.get("params"),
            "return_obj": _short(node.get("returnObj")),
            "throwable": _short(node.get("throwable")),
            "children": node.get("children") or [],
        })

    for res in results:
        if not isinstance(res, dict):
            continue
        kind = str(res.get("type") or "result")
        inner = res.get("results")
        if isinstance(inner, list) and inner:
            # 命令行的作业流形态：命中嵌在元素内部的 results 里
            for node in inner:
                if isinstance(node, dict):
                    _walk(node, kind)
        elif kind in ("watch", "tt", "trace", "stack") and (
                res.get("className") or res.get("methodName")):
            # HTTP API 形态：命中就是顶层元素（带 className/methodName/value）
            _walk(res, kind)
    return out[:80]


def _short(value: Any, limit: int = 600) -> Any:
    """裁剪过长的字段（面板展示用，避免把整个对象塞进事件）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:limit]
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


@_safe
async def async_command(session_id: int, command: str, *, exec_timeout_ms: int = 10000) -> dict:
    """异步提交（watch/trace 等持续输出命令）：立即拿 job_id，随后用 pull 取结果。"""
    from app.mcp_servers.arthas_client import ArthasClient, ArthasError

    sess, err = _require(session_id)
    if err:
        return err
    assert sess is not None
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "command 不能为空。"}
    try:
        sid, cid = await _ensure_arthas_session(sess)
        data = await ArthasClient(sess.http_port, session_id=sid, consumer_id=cid).async_exec(
            cmd, exec_timeout_ms=exec_timeout_ms)
    except ArthasError as e:
        return {"ok": False, "error": f"Arthas 不可用：{e}"}
    job_id = data.get("job_id")
    if job_id:
        sess.jobs[str(job_id)] = {"command": cmd, "created_at": time.time()}
    await _broadcast(session_id, {
        "phase": "job_started", "command": cmd, "job_id": job_id,
        "summary": f"已提交异步观测：{cmd}",
    })
    return {"ok": bool(data.get("ok")), "job_id": job_id, "job_status": data.get("job_status"),
            "command": cmd, "state": data.get("state"),
            "error": None if data.get("ok") else _submit_error(data, sess)}


def _submit_error(data: dict, sess: ArthasSession) -> str:
    """异步提交失败的友好说明（Arthas 的原始消息单独给出去容易让模型误解）。"""
    msg = str(data.get("message") or "提交失败")
    if "Another job is running" in msg:
        pending = "、".join(str(j) for j in sess.jobs) or "（本地已无记录）"
        return (f"已有进行中的观测作业（Arthas 同一会话同时只允许一个作业）："
                f"先调 arthas_pull 取回结果，或对作业 {pending} 调 arthas_interrupt 中止后重试。")
    return msg


@_safe
async def pull(session_id: int, job_id: str | int | None = None, *, timeout_s: float = 30.0) -> dict:
    """拉取异步作业结果（长轮询，单次）。

    注意：Arthas 的结果流是**消费即清空**的（实测）——同一次观测第一次 pull 拿到
    全部积压（含 session 初始化消息与命中记录），之后再 pull 返回空，
    因此调用方要拿“第一次”的结果；本函数把命中条目抽到 entries 一并返回。
    """
    from app.mcp_servers.arthas_client import ArthasClient, ArthasError

    sess, err = _require(session_id)
    if err:
        return err
    assert sess is not None
    # Arthas 的 jobId 是**整数**（如 4），调用方可能传 int 或 str，统一转成字符串
    jid = str(job_id).strip() if job_id is not None else ""
    if not jid:
        if not sess.jobs:
            return {"ok": False, "error": "当前没有进行中的观测作业。"}
        jid = max(sess.jobs, key=lambda k: sess.jobs[k]["created_at"])
    try:
        sid, cid = await _ensure_arthas_session(sess)
        data = await ArthasClient(sess.http_port, session_id=sid, consumer_id=cid).pull_results(
            job_id=jid, timeout_s=timeout_s)
    except ArthasError as e:
        return {"ok": False, "error": f"Arthas 不可用：{e}"}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("message") or data.get("state") or "拉取失败"}
    res = _annotate(sess, sess.jobs.get(jid, {}).get("command", ""), data)
    # 作业结束后不再保留，避免把已完成 job 反复 pull 出重复结果。
    # 注意：一次性命令的 jobStatus 是 **TERMINATED**（实测 arthas 4.3.5），
    # 持久观测命令（watch/trace -n）命中足够次数后也是 TERMINATED。
    if str(res.get("job_status") or "").upper() in ("FINISHED", "TERMINATED",
                                                     "FAILED", "CANCELED"):
        sess.jobs.pop(jid, None)
    await _broadcast_observation(session_id, res, job_id=jid)
    return res


@_safe
async def interrupt(session_id: int, job_id: str | int) -> dict:
    """中断一个进行中的异步观测。"""
    from app.mcp_servers.arthas_client import ArthasClient, ArthasError

    sess, err = _require(session_id)
    if err:
        return err
    assert sess is not None
    try:
        sid, cid = await _ensure_arthas_session(sess)
        data = await ArthasClient(sess.http_port, session_id=sid,
                                  consumer_id=cid).interrupt_job(job_id)
    except ArthasError as e:
        return {"ok": False, "error": f"Arthas 不可用：{e}"}
    sess.jobs.pop(str(job_id), None)
    return {"ok": bool(data.get("ok")), "message": "已请求中断该观测作业。",
            "error": None if data.get("ok") else (data.get("message") or "中断失败")}


async def _kill(sess: ArthasSession) -> None:
    """确保 arthas-boot 进程退出（先 TERM 后 KILL）。"""
    proc = sess.proc
    if proc is None or proc.returncode is not None:
        return
    with _suppress():
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=8)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        with _suppress():
            proc.kill()


@_safe
async def stop(session_id: int, *, reason: str = "user") -> dict:
    """断开 Arthas 会话：先 `reset` 还原被增强的类，再 `stop` 卸载目标 JVM 内的 Arthas。

    注意：不调 close_session——`stop` 本身就会关掉整个 Arthas server（含所有会话），
    而提前关会话会让后面的 reset/stop 因会话失效而被拒绝（实测踩过）。
    """
    k = _key(session_id)
    sess = _sessions.pop(k, None)
    if sess is None:
        return {"ok": True, "message": "当前没有 Arthas 会话。"}

    # reset 必须真正生效：否则 retransform 过的类会一直影响目标 JVM（IDEA 断点会失效）
    reset_note = ""
    try:
        data = await _plain_exec(sess.http_port, "reset", 10000, 20.0)
        reset_note = ("已 reset 还原被增强的类" if data.get("ok")
                      else f"reset 未生效（{data.get('message') or data.get('state')}）")
    except Exception as e:  # noqa: BLE001 —— reset 失败只是提示，不能阻断断开流程
        reset_note = f"reset 未执行（{e}），如目标方法行为异常请在 IDEA 中重启应用"

    shut_ok = await _shutdown_agent(sess.http_port)
    await _kill(sess)
    tail = "" if shut_ok else "⚠️ 未能确认 Arthas 已从目标 JVM 卸载（端口仍监听）——" \
                          "可重试 arthas_stop，或在目标应用里重启释放。"
    summary = f"Arthas 会话已断开（PID {sess.pid}）。{reset_note}{tail}"
    await _broadcast(session_id, {
        "phase": "detached", "pid": sess.pid, "http_port": sess.http_port,
        "reason": reason, "summary": summary,
    })
    return {"ok": True, "pid": sess.pid, "http_port": sess.http_port,
            "closed": shut_ok, "message": summary.strip()}


async def _plain_exec(port: int, command: str, timeout_ms: int, timeout_s: float) -> dict:
    """不带会话 id 的一次性命令。

    断开流程**必须**走这条路径：带了已失效的会话 id 时，Arthas 会返回
    `state=FAILED`（HTTP 层仍是 200），若只看连不连得上就会误以为
    reset/stop 执行了——实测出现过“以为已还原、Arthas 实际仍在目标 JVM 里跑”的情况。
    """
    from app.mcp_servers.arthas_client import ArthasClient

    return await ArthasClient(port).exec(command, exec_timeout_ms=timeout_ms, timeout_s=timeout_s)


async def _shutdown_agent(port: int) -> bool:
    """向目标 JVM 内的 Arthas 发 `stop` 并**确认端口已释放**（返回是否确认卸载）。"""
    try:
        await _plain_exec(port, "stop", 5000, 10.0)
    except Exception:  # noqa: BLE001
        # stop 会让 Arthas server 在响应写完前关掉自己，连接被重置/拒绝是**预期结果**
        pass
    for _ in range(8):
        await asyncio.sleep(0.5)
        if _port_free(port):
            return True
    return False


def status(session_id: int) -> dict:
    sess = get_session(session_id)
    if sess is None:
        return {"ok": True, "attached": False, "pid": None, "http_port": None,
                "version": None, "active_jobs": 0}
    return {**sess.to_dict(),
            "jobs": [{"job_id": j, **v} for j, v in sess.jobs.items()]}


# ═══════════════════════════════════════════════════════════════
# 空闲回收 / 会话清理
# ═══════════════════════════════════════════════════════════════

def _idle_timeout() -> int:
    raw = _cfg("arthas_idle_timeout_sec")
    try:
        val = int(raw) if raw else _DEFAULT_IDLE_TIMEOUT_S
        return max(60, val)
    except ValueError:
        return _DEFAULT_IDLE_TIMEOUT_S


def _ensure_reaper() -> None:
    """惰性启动空闲回收循环（进程自身不占用任何 UI 线程）。"""
    global _reaper
    if _reaper is not None and not _reaper.done():
        return
    _reaper = asyncio.create_task(_reap_loop())


async def _reap_loop() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            await reap_idle()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 —— 回收失败不能拖垮后台循环
            logger.debug("[arthas] 空闲回收异常", exc_info=True)


async def reap_idle() -> list[int]:
    """回收空闲超时的会话（Arthas agent 常驻会占目标 JVM 资源，必须自动释放）。"""
    timeout = _idle_timeout()
    now = time.time()
    stale = [sid for (sid, _t), s in _sessions.items() if now - s.last_active > timeout]
    for sid in stale:
        logger.info("[arthas] 空闲超时回收会话 %s（>%ss）", sid, timeout)
        with _suppress():
            await stop(sid, reason="idle")
    return stale


async def cleanup_session(session_id: int) -> None:
    """chatcoder 会话结束时同步清理 Arthas 资源。"""
    if get_session(session_id) is not None:
        with _suppress():
            await stop(session_id, reason="session_closed")


# ═══════════════════════════════════════════════════════════════
# 配置读写（供设置页 / 右侧调试面板）
# ═══════════════════════════════════════════════════════════════

_CONFIG_KEYS = ("arthas_java_home", "arthas_boot_jar", "arthas_repo_mirror",
                "arthas_idle_timeout_sec", "arthas_http_port", "arthas_dir")


def config_info() -> dict:
    """当前 Arthas 配置 + 环境探测结果（面板能直接告诉用户"为什么还不能用"）。"""
    home, source = resolve_java_home()
    jar_cfg = os.environ.get("CHATCODER_ARTHAS_JAR") or _cfg("arthas_boot_jar")
    cache = arthas_dir() / "arthas-boot.jar"
    if jar_cfg and Path(jar_cfg).is_file():
        jar_path, jar_source = jar_cfg, "配置指定的本地 jar"
    elif _looks_like_jar(cache):
        jar_path, jar_source = str(cache), "本地缓存"
    else:
        jar_path, jar_source = "", "首次 attach 时自动下载"
    return {
        "ok": True,
        "java_home": _cfg("arthas_java_home"),
        "java_home_resolved": str(home) if home else "",
        "java_home_source": source,
        "boot_jar": jar_cfg or "",
        "boot_jar_resolved": jar_path,
        "boot_jar_source": jar_source,
        "cache_dir": str(arthas_dir()),
        "repo_mirror": _cfg("arthas_repo_mirror") or "aliyun",
        "idle_timeout_sec": _idle_timeout(),
        "http_port": _cfg("arthas_http_port"),
        "disabled_commands": _DISABLED_COMMANDS,
    }


def save_config(patch: dict) -> dict:
    """写入 Arthas 相关配置（与其它设置项同一文件，保留其它键）。"""
    path = Path(os.environ.get("CHATCODER_USER_CONFIG",
                               str(Path.home() / ".chatcoder" / "config.json")))
    data = _user_config()
    for key in _CONFIG_KEYS:
        if key in patch and patch[key] is not None:
            data[key] = str(patch[key]).strip()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"写入配置失败：{e}"}
    return {"ok": True}

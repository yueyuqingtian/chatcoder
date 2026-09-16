"""plan-248-1258 M3.1: 符号索引工作区注册与自动增量更新。

背景：symbol_index_service 已能建/增量更新索引，但
1) 无「每工作区开关」（需求：每个工作目录默认关闭，用户开启后自动索引）；
2) 无后台自动更新（改动文件后不会自动重扫，只能等下次工具调用）。

本模块提供：
- 状态表 symbol_index_state（workspace 主键）：enabled/status/files/symbols/updated/progress；
- enable/disable：开启即在后台线程建索引并广播进度；
- 自动增量：后台循环定时对有变更的工作区做增量（sha1 短路，成本可控）；
- 写盘钩子：工具写文件后调用 notify_file_changed，标记该工作区待增量（下一轮或延迟立即处理）。

设计取舍：不引入 watchdog（PyInstaller 采集风险 + 多平台差异），改用
「短间隔增量轮询 + 写盘即时标记」组合——sha1 短路使轮询在无变更时几乎零成本。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

# 自动增量巡检间隔（秒）。
# 仅兑底外部编辑器改动；应用内写盘由 notify_file_changed 标脏后
# 2 秒防抖立即增量，不受此间隔影响。
# 曾为 30：打包环境下 worker 是 onefile exe，每个开启的工作区每 30s
# 都要解压启动一次进程（多工作区时每分钟 6+ 次进程创建），是
# "索引库耗性能"的另一大来源；mtime 短路后单次成本已极低，拉长间隔。
AUTO_SCAN_INTERVAL = 600
# 写盘后延迟立即增量（避免连续写入期间反复扫描）
DIRTY_DEBOUNCE_S = 2.0
# worker 停滞阈值（秒）：进度长期不推进即判定卡死并终止。
# 阈值取 180s：正常大仓库解析单文件是毫秒级、每 32 个文件必出新进度；
# 真有病态输入（如正则灾难性回溯）时，宁可有界失败也不让 UI 无限卡住。
WORKER_STALL_TIMEOUT_S = 180.0
# plan-248-1273: 启动后延迟再开始自动增量，避免与应用启动请求风暴叠加
STARTUP_DELAY_S = 30

_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None
# 内存态：workspace → 状态；持久化到各工作区 .chatcoder/index_state.db
_registry: dict[str, dict] = {}
_dirty: dict[str, float] = {}
_lock = asyncio.Lock()
# plan-248-1273: 启动首轮只处理 dirty 工作区，不做全量巡检（防"打开软件就全量扫描"）
_first_scan_done = False
# legacy symbols.db → index_state.db 迁移只尝试一次（避免每次读状态都打开旧库）
_migrated: set[str] = set()

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> float:
    return time.time()


def _norm(workspace: str | Path) -> str:
    return str(Path(workspace).resolve())


def _connect_state(workspace: str | Path):
    """打开独立的索引状态库。

    不能把 index_state 和 symbols.db 共用：大型项目全量解析时 symbols.db 会持有
    SQLite 写锁，而 API 请求若在事件循环里读取状态会阻塞到 sqlite timeout（截图中
    providers/workspaces/models 一批请求 pending 的根因）。独立文件让状态读写永不等待
    符号解析库；首次打开时把旧 symbols.db 中的 index_state 迁移过来。
    """
    import sqlite3

    from app.services.symbol_index_service import _db_path

    symbols_db = _db_path(workspace)
    db = symbols_db.with_name("index_state.db")
    ws_key = str(db)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db), timeout=0.5)
        conn.executescript(_STATE_SCHEMA)
        # 从旧 symbols.db 一次性迁移状态，兼容此前已经开启过索引的工作区。
        # plan-248-1273: 每个工作区只尝试一次迁移，避免每次读状态都打开旧库
        # （旧库可能被 worker 写锁持有，同步 connect 会阻塞事件循环）。
        if ws_key not in _migrated:
            _migrated.add(ws_key)
            if conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0] == 0 and symbols_db.exists():
                with contextlib.suppress(Exception):
                    with sqlite3.connect(str(symbols_db), timeout=0.5) as legacy:
                        rows = legacy.execute("SELECT key, value FROM index_state").fetchall()
                        if rows:
                            conn.executemany("INSERT OR REPLACE INTO index_state(key, value) VALUES (?, ?)", rows)
                            conn.commit()
        return conn
    except Exception:  # noqa: BLE001
        logger.warning("[symbols] 打开索引状态库失败 %s", db, exc_info=True)
        return None


def _read_state(workspace: str | Path) -> dict:
    conn = _connect_state(workspace)
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT key, value FROM index_state").fetchall()
        return {k: v for k, v in rows}
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()


def _write_state(workspace: str | Path, **kv) -> None:
    conn = _connect_state(workspace)
    if conn is None:
        return
    try:
        for k, v in kv.items():
            conn.execute(
                "INSERT INTO index_state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, "" if v is None else str(v)),
            )
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.debug("[symbols] 写索引状态失败", exc_info=True)
    finally:
        conn.close()


def get_state(workspace: str | Path) -> dict:
    """返回工作区索引状态（默认关闭 / 未索引）。"""
    ws = _norm(workspace)
    s = _read_state(ws)
    enabled = s.get("enabled") == "1"
    status = s.get("status") or ("ready" if enabled else "off")
    # 进行中的实时计数（UI 展示“已扫描 x / 共 y 个文件”）；非索引期间归零，
    # 避免完成/关闭后残留上轮扫描计数。
    in_progress = status in ("queued", "scanning", "parsing")
    return {
        "workspace": ws,
        "enabled": enabled,
        "status": status,
        "files": int(s.get("files") or 0),
        "symbols": int(s.get("symbols") or 0),
        "last_updated": float(s["last_updated"]) if s.get("last_updated") else None,
        "progress": int(s.get("progress") or 0),
        "error": s.get("error") or None,
        "files_scanned": int(s.get("files_scanned") or 0) if in_progress else 0,
        "files_total": int(s.get("files_total") or 0) if in_progress else 0,
    }


async def _broadcast(workspace: str, payload: dict) -> None:
    """通过全局通道广播索引进度（索引是全局能力，不绑定具体会话）。"""
    try:
        from app.gateway.ws import manager as ws_manager
        await ws_manager.broadcast_global({"event": "symbol_index.progress", "payload": payload})
    except Exception:  # noqa: BLE001
        logger.debug("[symbols] 广播索引进度失败", exc_info=True)


def _state_db_path(ws: str) -> Path:
    from app.services.symbol_index_service import _db_path
    return _db_path(ws).with_name("index_state.db")


def _worker_command(ws: str, state_db: Path, job_id: str, cancel_file: Path, force: bool) -> list[str]:
    explicit = os.environ.get("CHATCODER_INDEX_WORKER")
    if explicit:
        cmd = [explicit]
    elif getattr(sys, "frozen", False):
        cmd = [str(Path(sys.executable).with_name("chatcoder-index-worker.exe"))]
    else:
        cmd = [sys.executable, "-m", "app.index_worker"]
    cmd += ["--workspace", ws, "--state-db", str(state_db), "--job-id", job_id,
            "--cancel-file", str(cancel_file)]
    if force:
        cmd.append("--force")
    return cmd


async def _run_index_worker(ws: str, *, rebuild: bool = False) -> dict:
    """在独立 OS 进程运行索引，主服务只轮询状态并广播。"""
    state_db = _state_db_path(ws)
    job_id = uuid.uuid4().hex[:12]
    cancel_file = state_db.with_name(f"index-cancel-{job_id}")
    log_file = state_db.with_name(f"index-worker-{job_id}.log")
    _write_state(ws, status="queued", progress=0, error="", worker_pid="", job_id=job_id,
                 files_scanned=0, files_total=0)
    await _broadcast(ws, {"workspace": ws, "status": "queued", "progress": 0, "job_id": job_id})
    process = None
    log_handle = None
    try:
        cmd = _worker_command(ws, state_db, job_id, cancel_file, rebuild)
        cwd = str(Path(__file__).resolve().parents[2])
        log_handle = open(log_file, "ab", buffering=0)
        creationflags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, stdout=log_handle, stderr=log_handle,
            creationflags=creationflags,
        )
        _workers[ws] = process
        _worker_jobs[ws] = {"pid": process.pid, "job_id": job_id, "log": str(log_file), "cancel_file": str(cancel_file)}
        _write_state(ws, status="scanning", worker_pid=process.pid, job_id=job_id)
        # 停滞看门狗：进度长时间不推进即判定 worker 卡死（如正则灾难性回溯），
        # 终止它并报明确错误，避免 UI 永远停在某个百分比。正常大仓库单文件
        # 解析是毫秒级、每 32 个文件必出新进度，该阈值留足余量。
        last_marker: tuple | None = None
        last_change_at = time.monotonic()
        while process.returncode is None:
            await asyncio.sleep(0.35)
            state = get_state(ws)
            await _broadcast(ws, {"workspace": ws, "status": state["status"], "progress": state["progress"],
                                  "files": state["files"], "symbols": state["symbols"],
                                  "files_scanned": state["files_scanned"],
                                  "files_total": state["files_total"],
                                  "job_id": job_id})
            marker = (state["status"], state["progress"], state["files_scanned"], state["files_total"])
            if marker != last_marker:
                last_marker = marker
                last_change_at = time.monotonic()
            elif time.monotonic() - last_change_at > WORKER_STALL_TIMEOUT_S:
                logger.warning("[symbols] worker 停滞 %ss（status=%s progress=%s scanned=%s），判定卡死并终止 ws=%s",
                               WORKER_STALL_TIMEOUT_S, state["status"], state["progress"],
                               state["files_scanned"], ws)
                _write_state(ws, status="error",
                             error=(f"索引进程无响应（停滞超过 {int(WORKER_STALL_TIMEOUT_S)}s），"
                                    "已自动终止；可重试或排除超大文件"),
                             worker_pid="")
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        process.kill()
                break
        await process.wait()
        state = get_state(ws)
        became_ready = (
            process.returncode == 0 and state["enabled"]
            and state["status"] not in ("error", "cancelled")
        )
        if became_ready:
            _write_state(ws, status="ready", progress=100, worker_pid="")
        elif process.returncode == 2 or not state["enabled"]:
            # 主动取消 / 关闭索引：仍开启→cancelled；已关闭→off。
            # 此前任何非零退出码都会把 off/cancelled 覆盖成 error——
            # 大仓库扫描中点"关闭索引"时 worker 被 terminate（Windows 退出码 1），
            # 页面从此一直显示「异常 worker exited with code 1」。
            _write_state(ws, status="cancelled" if state["enabled"] else "off", worker_pid="")
        elif state["status"] not in ("error", "cancelled", "off"):
            _write_state(ws, status="error", error=f"worker exited with code {process.returncode}", worker_pid="")
        state = get_state(ws)
        await _broadcast(ws, {"workspace": ws, **state, "job_id": job_id})
        return {"ok": state["status"] == "ready", **state}
    except Exception as e:  # noqa: BLE001
        logger.warning("[symbols] worker 启动/运行失败 ws=%s", ws, exc_info=True)
        _write_state(ws, status="error", error=str(e)[:300], worker_pid="", job_id=job_id)
        await _broadcast(ws, {"workspace": ws, "status": "error", "error": str(e)[:200], "job_id": job_id})
        return {"ok": False, "error": str(e)}
    finally:
        _workers.pop(ws, None)
        _worker_jobs.pop(ws, None)
        with contextlib.suppress(OSError):
            cancel_file.unlink()
        with contextlib.suppress(Exception):
            log_handle.close()


async def _run_index(ws: str, *, rebuild: bool = False) -> dict:
    """兼容旧调用名，但实际执行已隔离到独立 worker 进程。"""
    return await _run_index_worker(ws, rebuild=rebuild)


async def enable(workspace: str) -> dict:
    """开启工作区索引（后台执行）；幂等。"""
    ws = _norm(workspace)
    _write_state(ws, enabled="1", status="indexing", progress=0, error="",
                 files_scanned=0, files_total=0)
    _registry[ws] = get_state(ws)
    running = _index_tasks.get(ws)
    if running is None or running.done():
        task = asyncio.create_task(_run_index_guarded(ws))
        _index_tasks[ws] = task
    return get_state(ws)


async def _cancel_worker(ws: str, process: asyncio.subprocess.Process, cancel_file: str) -> None:
    """取消 worker：先发取消文件（worker 遍历/逐文件阶段都会检查，会主动退出），
    宽限 5s 后才 terminate。宽限过短时大目录遍历中的 worker 会被强杀
    （Windows 退出码 1），导致 manager 把状态误报为 error。"""
    with contextlib.suppress(OSError):
        Path(cancel_file).touch()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=1.0)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()


async def disable(workspace: str) -> dict:
    """关闭工作区索引；向独立 worker 发取消信号，不阻塞主服务。"""
    ws = _norm(workspace)
    job = _worker_jobs.get(ws)
    process = _workers.get(ws)
    if job and process is not None:
        asyncio.create_task(_cancel_worker(ws, process, job["cancel_file"]))
    _write_state(ws, enabled="0", status="off", progress=get_state(ws)["progress"])
    return get_state(ws)


_index_tasks: dict[str, asyncio.Task] = {}
_index_locks: dict[str, asyncio.Lock] = {}
_workers: dict[str, asyncio.subprocess.Process] = {}
_worker_jobs: dict[str, dict] = {}



def _index_lock(ws: str) -> asyncio.Lock:
    lock = _index_locks.get(ws)
    if lock is None:
        lock = asyncio.Lock()
        _index_locks[ws] = lock
    return lock


async def _run_index_guarded(ws: str, *, rebuild: bool = False) -> dict:
    """同一工作区只允许一个索引任务，避免全量/增量并发争抢 SQLite 与 CPU。"""
    lock = _index_lock(ws)
    if lock.locked():
        logger.info("[symbols] 跳过重复索引任务 ws=%s", ws)
        return {"ok": False, "skipped": True, "reason": "index already running"}
    async with lock:
        return await _run_index(ws, rebuild=rebuild)


async def rebuild(workspace: str) -> dict:
    """手动全量重建（开启状态下亦可用）。"""
    ws = _norm(workspace)
    running = _index_tasks.get(ws)
    if running is not None and not running.done():
        return get_state(ws)
    _write_state(ws, enabled="1", status="indexing", progress=0, error="",
                 files_scanned=0, files_total=0)
    task = asyncio.create_task(_run_index_guarded(ws, rebuild=True))
    _index_tasks[ws] = task
    return get_state(ws)


def notify_file_changed(workspace: str | Path, file_path: str | None = None) -> None:
    """写盘钩子：标记工作区为「脏」，由自动循环做增量（含失效单文件优化）。

    同步函数（工具写盘路径上调用，不能 await）。真正扫描在 _loop 中完成。
    """
    ws = _norm(workspace)
    st = _read_state(ws)
    if st.get("enabled") != "1":
        return
    if file_path:
        with contextlib.suppress(Exception):
            from app.services import symbol_index_service as sis
            sis.invalidate_file(ws, file_path)
    _dirty[ws] = _now()


async def _enabled_workspaces() -> list[str]:
    """当前已开启索引的工作区：已知项目 + 历史开启过的目录。"""
    from app.persistence.database import async_session_factory
    from app.services import project_service

    found: set[str] = set(_registry.keys())
    try:
        async with async_session_factory() as db:
            projects = await project_service.list_projects(db)
        for p in projects:
            path = getattr(p, "path", None) or getattr(p, "workspace_root", None)
            if path:
                found.add(_norm(path))
    except Exception:  # noqa: BLE001
        logger.debug("[symbols] 读取项目列表失败", exc_info=True)
    out = []
    for ws in found:
        st = _read_state(ws)
        if st.get("enabled") == "1":
            out.append(ws)
    return out


async def _scan_once() -> int:
    """对所有开启的工作区做增量。

    plan-248-1273: 统一提交到独立 worker 进程，主服务只负责调度与状态读取，
    绝不在事件循环/线程池中执行文件扫描与 AST 解析。
    启动首轮只处理 dirty 工作区（写盘钩子标记的），不做全量巡检——
    避免"打开软件就自动全量扫描"拖垮所有 HTTP 请求。
    """
    global _first_scan_done

    n = 0
    now = _now()
    first = not _first_scan_done
    _first_scan_done = True

    for ws in await _enabled_workspaces():
        running = _index_tasks.get(ws)
        if running is not None and not running.done():
            continue
        dirty_at = _dirty.get(ws)
        due = dirty_at is not None and (now - dirty_at) >= DIRTY_DEBOUNCE_S

        # 启动首轮：只处理写盘脏标记触发的工作区，跳过周期巡检
        if first and not due:
            continue

        # 非脏工作区按间隔巡检（外部编辑器改动场景）
        if not due and dirty_at is None:
            last = get_state(ws).get("last_updated") or 0
            if (now - last) < AUTO_SCAN_INTERVAL:
                continue

        # 提交到独立 worker（fire-and-forget），主服务不等待解析完成
        _dirty.pop(ws, None)
        task = asyncio.create_task(_run_index_guarded(ws))
        _index_tasks[ws] = task
        n += 1
        logger.info("[symbols] 自动增量已提交 worker ws=%s dirty=%s", ws, dirty_at is not None)
    return n


async def _loop() -> None:
    assert _stop is not None
    # plan-248-1273: 启动延迟——等应用完全就绪（health 通过、前端加载完成）后
    # 再开始自动增量，避免首轮扫描与启动请求风暴叠加导致全部 pending。
    logger.info("[symbols] 符号索引自动增量循环启动（启动延迟 %ss，间隔 %ss）",
                STARTUP_DELAY_S, AUTO_SCAN_INTERVAL)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_stop.wait(), timeout=STARTUP_DELAY_S)
    if _stop.is_set():
        logger.info("[symbols] 符号索引自动增量循环已停止（启动延迟期间收到停止信号）")
        return
    while not _stop.is_set():
        try:
            await _scan_once()
        except Exception:  # noqa: BLE001
            logger.exception("[symbols] 自动增量 tick 异常")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_stop.wait(), timeout=AUTO_SCAN_INTERVAL)
    logger.info("[symbols] 符号索引自动增量循环已停止")


async def _recover_stale_states() -> None:
    """启动时清理上次会话遗留的 indexing 状态（worker 进程已不在）。

    上次运行中如果有工作区处于 indexing/queued/scanning/parsing，
    本次启动时该 worker 已不存在，必须标记为 error，否则 UI 永远显示"索引中"。
    """
    found: set[str] = set(_registry.keys())
    try:
        from app.persistence.database import async_session_factory
        from app.services import project_service

        async with async_session_factory() as db:
            projects = await project_service.list_projects(db)
        for p in projects:
            path = getattr(p, "path", None) or getattr(p, "workspace_root", None)
            if path:
                found.add(_norm(path))
    except Exception:  # noqa: BLE001
        logger.debug("[symbols] 恢复状态时读取项目列表失败", exc_info=True)

    for ws in found:
        st = get_state(ws)
        if st["status"] in ("indexing", "queued", "scanning", "parsing"):
            _write_state(ws, status="error", error="上次索引未完成（应用重启），请手动重建",
                         worker_pid="", progress=0)
            logger.info("[symbols] 清理遗留索引状态 ws=%s (原状态=%s)", ws, st["status"])


async def start() -> None:
    global _task, _stop
    if _task is not None and not _task.done():
        return
    if not getattr(settings, "symbol_index_auto_update", True):
        logger.info("[symbols] 自动增量已被配置禁用")
        return
    # plan-248-1273: 启动时清理上次会话遗留的 indexing 状态
    try:
        await _recover_stale_states()
    except Exception:
        logger.debug("[symbols] 恢复遗留状态失败(非阻塞)", exc_info=True)
    _stop = asyncio.Event()
    _task = asyncio.create_task(_loop())


def worker_diagnostics() -> dict:
    return {
        "mode": "process",
        "workers": [
            {"workspace": ws, **info}
            for ws, info in _worker_jobs.items()
        ],
    }


async def stop() -> None:
    global _task, _stop
    # plan-248-1273: 强制回收全部 worker——先发取消信号，短暂等待后 terminate。
    # 不允许孤儿 worker 在主服务退出后继续占 CPU 和文件锁。
    for ws, process in list(_workers.items()):
        job = _worker_jobs.get(ws)
        if job:
            with contextlib.suppress(OSError):
                Path(job["cancel_file"]).touch()
    if _workers:
        # 给 worker 1 秒优雅退出窗口
        await asyncio.sleep(1.0)
    for ws, process in list(_workers.items()):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=1.0)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.kill()
    _workers.clear()
    _worker_jobs.clear()
    # 停止调度循环
    if _stop is not None:
        _stop.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
        except Exception:
            logger.debug("[symbols] 停止时异常", exc_info=True)
    _task = None
    _stop = None


async def list_workspaces() -> list[dict]:
    """列出全部已知工作区（项目 + 已开启目录）的索引状态，供索引库页面。"""
    from app.persistence.database import async_session_factory
    from app.services import project_service

    rows: list[dict] = []
    seen: set[str] = set()
    try:
        async with async_session_factory() as db:
            projects = await project_service.list_projects(db)
        for p in projects:
            path = getattr(p, "path", None) or getattr(p, "workspace_root", None)
            if not path:
                continue
            ws = _norm(path)
            if ws in seen:
                continue
            seen.add(ws)
            rows.append({**get_state(ws), "name": getattr(p, "name", None) or Path(ws).name,
                         "exists": Path(ws).is_dir()})
    except Exception:  # noqa: BLE001
        logger.debug("[symbols] 列出工作区失败", exc_info=True)
    # 补上历史开启但已不在项目列表的目录
    for ws in list(_registry.keys()):
        if ws in seen:
            continue
        seen.add(ws)
        rows.append({**get_state(ws), "name": Path(ws).name, "exists": Path(ws).is_dir()})
    return rows


__all__ = [
    "get_state", "enable", "disable", "rebuild", "notify_file_changed",
    "list_workspaces", "start", "stop", "AUTO_SCAN_INTERVAL",
]

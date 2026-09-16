"""chatcoder 独立符号索引 worker。

该进程只负责文件扫描、AST/正则解析和工作区 symbols.db 写入，不导入 FastAPI、
主业务数据库或模型请求路由。主服务通过参数启动它，通过独立 index_state.db
读取状态；取消通过 cancel_file 文件协议传递。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from app.services import symbol_index_service as sis

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_state (key TEXT PRIMARY KEY, value TEXT);
"""


def _state_conn(path: str) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=0.5)
    conn.executescript(_STATE_SCHEMA)
    return conn


def _write_state(path: str, **values: object) -> None:
    conn = _state_conn(path)
    if conn is None:
        return
    try:
        with conn:
            conn.executemany(
                "INSERT INTO index_state(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, "" if v is None else str(v)) for k, v in values.items()],
            )
    except sqlite3.Error:
        logging.getLogger(__name__).debug("worker state write failed", exc_info=True)
    finally:
        conn.close()


def _cancelled(path: str | None) -> bool:
    return bool(path and Path(path).exists())


def _set_low_priority() -> None:
    """降低 worker 调度优先级，避免大型索引抢占前台软件。"""
    if os.name != "nt":
        try:
            os.nice(5)
        except (AttributeError, OSError):
            pass
        return
    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        # BELOW_NORMAL_PRIORITY_CLASS
        ctypes.windll.kernel32.SetPriorityClass(handle, 0x00004000)
    except Exception:
        logging.getLogger(__name__).debug("failed to lower worker priority", exc_info=True)


def run(args: argparse.Namespace) -> int:
    _set_low_priority()
    logger = logging.getLogger("chatcoder.index_worker")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.time()
    # 清零上轮残留计数：scanning 属进行中状态，不清零会让前端瞬时显示上一轮的
    # “已扫描 x / 共 y”（尤其从 ready 再次重建时）。
    _write_state(args.state_db, status="scanning", progress=1, error="",
                 files_scanned=0, files_total=0,
                 worker_pid=os.getpid(), job_id=args.job_id)

    def progress(value: int, phase: str = "parsing", **extra: object) -> None:
        """�v举阶段（phase=scanning）与解析阶段（parsing）分开写入。

        前端据 status 展示“正在扫描/正在解析”，两阶段的百分比各自归一化，
        避免旧实现把扫描阶段也用 parsing 状态与 10% 以下的进度混淆。
        """
        _write_state(args.state_db, status=phase, progress=value, worker_pid=os.getpid(), job_id=args.job_id, **extra)

    try:
        result = sis.index_workspace(
            args.workspace,
            force=args.force,
            cancel_file=args.cancel_file,
            progress_cb=progress,
        )
        if result.get("cancelled"):
            _write_state(args.state_db, status="cancelled", progress=progress_value(result),
                         files_scanned=result.get("files_scanned", 0),
                         files_total=result.get("files_total", 0),
                         worker_pid=os.getpid(), elapsed_ms=int((time.time() - started) * 1000))
            return 2
        if result.get("error"):
            _write_state(args.state_db, status="error", progress=0, error=result["error"], worker_pid=os.getpid(), elapsed_ms=int((time.time() - started) * 1000))
            return 1
        stats = sis.index_stats(args.workspace)
        _write_state(
            args.state_db,
            status="ready",
            progress=100,
            error="",
            files=stats.get("files", 0),
            symbols=stats.get("symbols", 0),
            last_updated=stats.get("last_updated") or time.time(),
            worker_pid=os.getpid(),
            job_id=args.job_id,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        logger.info("index ready workspace=%s files=%s symbols=%s", args.workspace, stats.get("files"), stats.get("symbols"))
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("index worker failed")
        _write_state(args.state_db, status="error", progress=0, error=str(exc)[:500], worker_pid=os.getpid(), job_id=args.job_id)
        return 1


def progress_value(result: dict) -> int:
    try:
        return int(result.get("progress", 0))
    except (TypeError, ValueError):
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="chatcoder symbol index worker")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--cancel-file", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

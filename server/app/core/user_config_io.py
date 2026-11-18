"""plan-20261118-runtime-question-cache：不同设置入口共用用户配置文件的原子更新。"""
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from anyio import CapacityLimiter, to_thread

logger = logging.getLogger(__name__)
_config_io = CapacityLimiter(4)
_snapshots: dict[Path, tuple[float, dict]] = {}
_SNAPSHOT_LIFETIME = 2.0


async def run_config_io(fn, *args):
    return await to_thread.run_sync(fn, *args, limiter=_config_io)


def recent_config(path: Path) -> dict | None:
    saved = _snapshots.get(path)
    if saved is not None and time.monotonic() - saved[0] < _SNAPSHOT_LIFETIME:
        return saved[1]
    return None


def remember_config(path: Path, data: dict) -> dict:
    snapshot = data.copy()
    _snapshots[path] = (time.monotonic(), snapshot)
    return snapshot


def read_config_snapshot(path: Path, read) -> dict:
    with _CFG_LOCK:
        data = recent_config(path)
        return data if data is not None else remember_config(path, read())


_CFG_LOCK = threading.RLock()

# 跨进程互斥：`msvcrt.locking` 只在 Windows 可用、`fcntl` 只在 POSIX 可用，二者都是标准库，
# 不引入新依赖。锁加在**独立的 .lock 文件**上而不是配置文件本身——配置文件会被 os.replace
# 换掉（inode/句柄失效），锁文件本身永不替换，才能长期持有稳定的字节区间锁。
# 拿不到平台实现时（理论上不会）退化为「只有进程内锁」，并在日志里留痕。
_LOCK_SUFFIX = ".lock"
_LOCK_TIMEOUT_S = 5.0
# 写临时文件的后缀：与 feature_map_service.save_map 同思路（临时文件 + os.replace）
_TMP_SUFFIX = ".tmp"


@contextmanager
def _file_lock(config_path: Path):
    """跨进程互斥，超时向调用方报告，防止用旧快照覆盖更新。"""
    path = config_path.with_name(config_path.name + _LOCK_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115

    locked = False
    try:
        # 锁需要落在**已存在的字节区间**上：Windows 的 msvcrt.locking 对空文件的锁定请求
        # 会失败，那样每次写都要白等满 _LOCK_TIMEOUT_S 才退化——比不加锁还慢。
        # 锁文件只用于加锁，内容无意义，写一个占位字节即可。
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
        except OSError:
            logger.debug("[user_config] 初始化锁文件失败（继续尝试加锁）", exc_info=True)

        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    # 非阻塞模式（LK_NBLCK）自旋重试：阻塞模式（LK_LOCK）超时行为在不同
                    # Windows 版本上不一致，且会卡死整个写路径。
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待用户配置写入超时，请重试。") from exc
                time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.debug("[user_config] 释放配置文件锁失败", exc_info=True)
        try:
            handle.close()
        except OSError:
            pass


@contextmanager
def config_transaction(path: Path):
    """配置文件的「读 → 改 → 写」临界区：进程内锁 + 跨进程文件锁。"""
    with _CFG_LOCK, _file_lock(path):
        yield


def write_config(path: Path, data: dict) -> None:
    """整文件写回，改**原子替换**（临时文件 + os.replace）。

    为什么必须原子替换：此前直接 `write_text` 先截断再写，写到一半崩溃/被杀就留下半截 JSON，
    下次 `_read_cfg` 解析失败返回 `{}` —— 用户所有自定义模式一次性消失（且无任何报错）。
    临时文件与目标同目录（os.replace 跨盘会退化成复制，不原子），写完再替换。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        remember_config(path, data)
    except OSError:
        logger.warning("[user_config] 写入用户配置失败 %s", path, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise



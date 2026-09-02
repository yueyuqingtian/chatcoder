"""统一日志配置（v2）：console + 文件轮转，启动时调用一次。"""
import logging
import logging.handlers
import os
import sys
from pathlib import Path

_configured = False

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_LOG_DIR = "logs"


def _utf8_stream(stream):
    """把标准流包装为 UTF-8 写出，避免 Windows GBK 控制台把中文写成 ?????。

    Python 3.7+ 可用 stream.reconfigure(encoding="utf-8")；不可用时回退
    为 errors="replace" 的写入包装，至少不抛异常。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
            return stream
        except (ValueError, OSError):
            pass
    try:
        import io

        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            return io.TextIOWrapper(buffer, encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass
    return stream


def _writable_data_dir() -> Path:
    """可写数据目录，规则与 run_server._resolve_data_dir 一致。

    打包后 exe 位于 Program Files，同目录不可写；必须落到用户目录。
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        elif sys.platform == "darwin":
            base = str(Path.home() / "Library" / "Application Support")
        else:
            base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "chatcoder"
    # 开发模式：项目根（server/ 的上一级）
    return Path(__file__).resolve().parents[2]


def resolve_log_dir(explicit: str | None = None) -> Path:
    """解析可写日志目录（已创建并通过写入校验）。

    优先级：显式参数 > CHATCODER_LOG_DIR > 可写数据目录/logs。
    mkdir 成功不代表可写（只读卷/权限不足），故做真实写探针。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_dir = os.environ.get("CHATCODER_LOG_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(_writable_data_dir() / "logs")

    last_error: Exception | None = None
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return cand
        except OSError as exc:
            last_error = exc
            continue

    fallback = Path(os.getcwd()) / "logs"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if last_error is not None:
        print(f"[logging] 警告：无可用日志目录，最后错误 {last_error}，回退 {fallback}",
              file=sys.stderr)
    return fallback


_DIAG_NAME = "app.diagnostics"
_diag_configured = False


def ensure_diagnostics_logger(log_dir: Path | None = None) -> Path | None:
    """确保诊断日志 handler 已挂载，返回日志文件路径（失败返回 None）。

    与 setup_logging 解耦、单独幂等：打包入口先配置 logging 时，
    app.main 里的 setup_logging 会被 _configured 跳过，但本函数仍可
    独立补齐 diagnostics handler，避免诊断日志静默丢失。
    """
    global _diag_configured
    diag = logging.getLogger(_DIAG_NAME)
    if _diag_configured:
        return _existing_diag_path(diag)

    try:
        target_dir = log_dir if log_dir is not None else resolve_log_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target_dir / "diagnostics.log", maxBytes=20 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        diag.setLevel(logging.DEBUG)
        diag.propagate = True  # 同时进入根日志器，控制台与主日志也可见
        diag.addHandler(handler)
        _diag_configured = True
        path = target_dir / "diagnostics.log"
        print(f"[logging] diagnostics.log = {path}", file=sys.stderr)
        return path
    except OSError as exc:
        print(f"[logging] diagnostics.log 挂载失败: {exc}", file=sys.stderr)
        return None


def _existing_diag_path(diag: logging.Logger) -> Path | None:
    for h in diag.handlers:
        base = getattr(h, "baseFilename", None)
        if base:
            return Path(base)
    return None


def setup_logging(debug: bool = False, log_dir: str | None = None) -> None:
    """初始化根日志器：控制台 + 文件（RotatingFileHandler）。

    幂等：重复调用不会重复添加 handler。
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    fmt = logging.Formatter(_LOG_FORMAT)

    # 控制台：v36 强制 UTF-8。Windows 控制台默认 GBK，
    # 中文日志经 errors=replace 会退化为 ?????，导致 backend.log 不可读。
    console = logging.StreamHandler(_utf8_stream(sys.stdout))
    console.setFormatter(fmt)
    root.addHandler(console)

    # 文件轮转（10MB × 5）
    try:
        dir_path = resolve_log_dir(log_dir)
        file_handler = logging.handlers.RotatingFileHandler(
            dir_path / "server.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        print(f"[logging] server.log = {dir_path / 'server.log'}", file=sys.stderr)
    except OSError as exc:
        print(f"[logging] server.log 挂载失败: {exc}", file=sys.stderr)

    # v36: 独立诊断日志——工具异常与 traceback 与业务日志分离，
    # 出问题时直接查 diagnostics.log，检索 [tool.error] 即可拿到完整现场。
    # 单独幂等，不受 setup_logging 调用时序影响（打包入口会先配置一次）。
    ensure_diagnostics_logger()

    _configured = True

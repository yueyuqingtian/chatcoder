"""统一日志配置（v2）：console + 文件轮转，启动时调用一次。"""
import logging
import logging.handlers
import sys
from pathlib import Path

_configured = False

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_LOG_DIR = "logs"


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

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 文件轮转（10MB × 5）
    try:
        dir_path = Path(log_dir or _LOG_DIR)
        dir_path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            dir_path / "server.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass  # 无法写日志文件时仅控制台

    _configured = True

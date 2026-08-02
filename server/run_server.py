"""打包专用启动入口：PyInstaller 产物从这里启动。

关键修复:
- 数据库 / 工作区 / 配置写入 %LOCALAPPDATA%/chatcoder (用户可写目录)
  安装在 Program Files 时 exe 同目录不可写,会导致后端启动崩溃
- 强制 CORS_ALLOW_ALL=true (Electron file:// origin 为 null)
- 强制 SQLite + 关闭 SQL echo
- 崩溃时写错误日志到数据目录,便于诊断
"""
import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path


def _resolve_data_dir() -> Path:
    """解析可写数据目录。

    Windows: %LOCALAPPDATA%/chatcoder  (如 C:/Users/xxx/AppData/Local/chatcoder)
    macOS:   ~/Library/Application Support/chatcoder
    Linux:   ~/.local/share/chatcoder
    开发模式: exe 同目录(已有写权限)
    """
    frozen = getattr(sys, "frozen", False)
    if not frozen:
        return Path(__file__).resolve().parent

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")

    data_dir = Path(base) / "chatcoder"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _setup_logging(data_dir: Path) -> None:
    """配置 logging：stderr(被 Electron 接进 backend.log) + 轮转文件 server.log。

    打包后 root logger 默认无 handler，logger.* 全部静默；在此单点补齐，
    让 scheduler/provider/agent_runtime 里现成的 logger.error/exception 生效。
    """
    try:
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(fmt)

        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "server.log", maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        )
        file_handler.setFormatter(fmt)

        root = logging.getLogger()
        root.setLevel(logging.INFO)
        for h in (stderr_handler, file_handler):
            root.addHandler(h)
        # uvicorn 框架日志也走同一批 handler
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            lg.propagate = True
    except Exception:
        pass  # 日志配置失败不影响启动


def _write_crash_log(exc: BaseException) -> None:
    """把崩溃堆栈写到数据目录下的 crash.log,便于排查。"""
    try:
        data_dir = _resolve_data_dir()
        log_path = data_dir / "crash.log"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"chatcoder-server crash at {os.path.basename(sys.executable)}\n")
            f.write(f"Python: {sys.version}\n")
            f.write(f"Frozen: {getattr(sys, 'frozen', False)}\n")
            f.write(f"Data dir: {data_dir}\n")
            f.write(f"Exec dir: {Path(sys.executable).parent}\n")
            f.write("=" * 60 + "\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass  # 不让日志写失败导致二次崩溃


def _load_env_from_exe_dir() -> None:
    """从 exe 所在目录加载 .env 文件到环境变量中。

    打包后 pydantic-settings 依赖 os.getcwd() 找 .env 文件，
    但 _setup_env 会 chdir 到 %LOCALAPPDATA%/chatcoder，
    导致 Settings 实例化时找不到 .env → default_llm_* 全为空。
    因此在 chdir 之前，先把 .env 中的关键变量注入 os.environ。
    """
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    env_file = exe_dir / ".env"
    if not env_file.exists():
        print(f"[chatcoder-server] WARNING: .env not found at {env_file}")
        return
    print(f"[chatcoder-server] loading env from {env_file}")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # 去掉值的引号(如果有)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        # 仅设置未定义的环境变量，避免覆盖 Electron 传入的显式值
        os.environ.setdefault(key, value)


def _setup_env() -> None:
    """设置环境变量,确保所有写操作落到可写目录。"""
    data_dir = _resolve_data_dir()
    _setup_logging(data_dir)

    # 关键: 在 chdir 之前加载 .env 到环境变量,确保 Settings 能读到配置
    _load_env_from_exe_dir()

    # ── 数据库: SQLite,放在可写数据目录 ──
    db_path = data_dir / "chatcoder.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    # ── 工作区: 默认在数据目录下 ──
    ws_path = data_dir / "workspace"
    ws_path.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("WORKSPACE_ROOT", str(ws_path))

    # ── CORS: 桌面版必须允许所有源 ──
    os.environ["CORS_ALLOW_ALL"] = "true"

    # ── 用户配置: 写入数据目录 ──
    os.environ.setdefault("CHATCODER_USER_CONFIG", str(data_dir / "config.json"))

    # ── 其他默认 ──
    os.environ.setdefault("DEBUG", "false")
    os.environ.setdefault("SERVER_HOST", "127.0.0.1")
    os.environ.setdefault("SERVER_PORT", "8000")

    # 切换到数据目录(确保相对路径写操作都落到可写位置)
    os.chdir(data_dir)

    print(f"[chatcoder-server] data_dir = {data_dir}")
    print(f"[chatcoder-server] database  = {db_path}")
    print(f"[chatcoder-server] workspace = {ws_path}")


def main() -> None:
    try:
        _setup_env()
        import asyncio

        import uvicorn

        from app.core.config import settings
        # 必须在 import app.main(触发 database engine 创建)前关闭 debug
        settings.debug = False

        # 关键:打包后必须直接导入 app 对象,不能用字符串
        from app.main import app as fastapi_app
        from app.gateway.routers.settings import load_persisted_workspace
        from app.persistence.database import init_db, async_session_factory
        from app.persistence.migrations import run_migrations
        from app.persistence.seed import seed

        load_persisted_workspace()
        asyncio.run(init_db())
        # v0.9 迁移:幂等补列
        async def _migrate() -> None:
            async with async_session_factory() as db:
                await run_migrations(db)
        asyncio.run(_migrate())
        asyncio.run(seed())

        config = uvicorn.Config(
            fastapi_app,
            host=settings.server_host,
            port=settings.server_port,
            reload=False,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        print(f"[chatcoder-server] listening on {settings.server_host}:{settings.server_port}")
        asyncio.run(server.serve())
    except Exception as exc:
        _write_crash_log(exc)
        # 也打印到 stderr(Electron 可以捕获)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

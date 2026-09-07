"""pytest 全局配置与隔离数据库初始化。"""
import os
import sys
import tempfile
from pathlib import Path

# 把 server/ 加入 sys.path,使测试能 import app.*
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

# 测试必须强制使用隔离配置，不能被本机 .env/打包运行环境污染。
# 文件库（临时目录）而非 :memory:——WriteEngine（同步 pysqlite 单写线程）与异步读
# 引擎必须连同一数据库文件（plan-206-975 无锁单写者；:memory: 下两个 engine 各是
# 独立内存库，写引擎会写错库）。测试间由 _reset_shared_test_db 重建表隔离。
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(
    tempfile.gettempdir(), "chatcoder_pytest.db")
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["QDRANT_URL"] = "http://localhost:6333"
os.environ["WORKSPACE_ROOT"] = "./workspace_test"
os.environ["AUTO_CONFIRM_PLAN"] = "false"
os.environ["AUTO_APPROVE_TOOLS"] = "false"
os.environ["APPROVAL_TIMEOUT_SEC"] = "2"
os.environ["AGENT_MAX_STEPS"] = "3"
os.environ["JWT_SECRET"] = "test-secret"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
async def _reset_shared_test_db():
    """为使用全局 async_session_factory 的旧测试重建内存库表。"""
    from app.persistence.database import Base, engine
    from app.persistence import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """提供一个干净的工作区目录(测试结束自动清理)。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws

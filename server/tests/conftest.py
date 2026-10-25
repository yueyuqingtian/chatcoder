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
# 独立内存库，写引擎会写错库）。
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
    """为使用全局 async_session_factory 的测试提供干净的库。

    plan-549-2281 测试提速：此前的实现是每个用例 `drop_all + create_all`，实测在 45 张表上
    分别要 0.20s + 0.10s（DDL 的代价随表数线性增长且必须重写 sqlite_master）。改为
    「保留表结构 + 清空数据」：同连接内按外键逆序 DELETE 全部表只要 0.09s，语义上对测试
    等价——用例看到的是空库，主键也会因 SQLite 的 rowid 行为从 1 重新开始（本库不使用
    AUTOINCREMENT，见 persistence/database.py 的同一说明）。

    仍然保留 create_all：`Base.metadata.create_all` 是 checkfirst 语义（幂等），表都在时
    实测单次 0.09s，却能兜住「上个用例或并发进程把表 drop 掉」的情况——此前共用文件库时
    出现过 `no such table` 随机报错，多这一步不会让隔离变慢多少，但能显著减少假失败。
    """
    from app.persistence import models  # noqa: F401  （注册全部模型）
    from app.persistence.database import Base, engine

    async with engine.begin() as conn:
        # 幂等建表：表已存在时只查一次 sqlite_master，不重写任何 DDL
        await conn.run_sync(Base.metadata.create_all)
        # 关外键后按 sorted_tables 逆序删数据：外键开启时删除会被约束挡下
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for table in reversed(Base.metadata.sorted_tables):
            await conn.exec_driver_sql(f'DELETE FROM "{table.name}"')
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    yield


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """提供一个干净的工作区目录(测试结束自动清理)。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws

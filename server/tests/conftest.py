"""pytest 全局配置。

测试范围:
- 纯逻辑层单测(无需 DB):DAG、scheduler、tools、approval、artifacts、context。
- 配置隔离:通过 monkeypatch settings,避免读到 .env 真实配置。

不包含:
- 真实 DB 集成测试(留 v0.5 加 testcontainers/PG)。
- 真实 LLM 调用(用 mock provider)。
"""
import os
import sys
from pathlib import Path

# 把 server/ 加入 sys.path,使测试能 import app.*
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

# 测试用环境变量(在 settings 加载前设置)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")  # 测试库
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("WORKSPACE_ROOT", "./workspace_test")
os.environ.setdefault("AUTO_CONFIRM_PLAN", "false")
os.environ.setdefault("AUTO_APPROVE_TOOLS", "false")  # 测试不走自动批准
os.environ.setdefault("APPROVAL_TIMEOUT_SEC", "2")  # 测试用短超时
os.environ.setdefault("AGENT_MAX_STEPS", "3")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """提供一个干净的工作区目录(测试结束自动清理)。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws

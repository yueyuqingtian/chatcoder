"""v1.0: 文件级 write-intent 冲突检测。

并行 agent 写同一文件时，通过 session 级注册表检测冲突：
- agent 执行 fs_write 前声明 intent
- 若路径已被其他 agent 持有 → 阻塞等待（超时返回冲突错误）
- 工具执行完毕释放 intent
"""
import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 等待锁释放的超时时间（秒）
_LOCK_TIMEOUT = 30.0


@dataclass
class _WriteIntent:
    agent_id: int
    agent_name: str
    path: str


class WriteIntentRegistry:
    """session 级文件写入意图注册表。"""

    def __init__(self) -> None:
        # path -> _WriteIntent（当前持有者）
        self._intents: dict[str, _WriteIntent] = {}
        self._lock = asyncio.Lock()
        # path -> asyncio.Event（释放信号）
        self._release_events: dict[str, asyncio.Event] = {}

    async def acquire(self, path: str, agent_id: int, agent_name: str) -> bool:
        """尝试获取文件写入权。若被其他 agent 持有则等待。

        返回 True 表示获取成功，False 表示超时/冲突。
        """
        # 规范化路径
        norm_path = path.replace("\\", "/").rstrip("/")

        async with self._lock:
            existing = self._intents.get(norm_path)
            if existing is None or existing.agent_id == agent_id:
                # 无冲突或同一 agent 重复写入
                self._intents[norm_path] = _WriteIntent(
                    agent_id=agent_id, agent_name=agent_name, path=norm_path
                )
                return True

            # 被其他 agent 持有 → 需要等待
            logger.warning(
                "[FileLock] 文件冲突: %s 被 agent %s(%d) 持有，agent %s(%d) 等待",
                norm_path, existing.agent_name, existing.agent_id, agent_name, agent_id,
            )
            event = self._release_events.setdefault(norm_path, asyncio.Event())

        # 在锁外等待释放
        try:
            await asyncio.wait_for(event.wait(), timeout=_LOCK_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("[FileLock] 等待超时: %s (agent=%d)", norm_path, agent_id)
            return False

        # 等待成功后获取
        async with self._lock:
            self._intents[norm_path] = _WriteIntent(
                agent_id=agent_id, agent_name=agent_name, path=norm_path
            )
            return True

    async def release(self, path: str, agent_id: int) -> None:
        """释放文件写入权。"""
        norm_path = path.replace("\\", "/").rstrip("/")
        async with self._lock:
            existing = self._intents.get(norm_path)
            if existing and existing.agent_id == agent_id:
                del self._intents[norm_path]
                # 通知等待者
                event = self._release_events.pop(norm_path, None)
                if event:
                    event.set()

    def get_holder(self, path: str) -> _WriteIntent | None:
        """查询当前持有者（调试用）。"""
        norm_path = path.replace("\\", "/").rstrip("/")
        return self._intents.get(norm_path)


# ── 全局注册表: session_id -> WriteIntentRegistry ──
_registries: dict[int, WriteIntentRegistry] = {}


def get_write_registry(session_id: int) -> WriteIntentRegistry:
    """获取 session 级的写入意图注册表。"""
    if session_id not in _registries:
        _registries[session_id] = WriteIntentRegistry()
    return _registries[session_id]


def cleanup_session(session_id: int) -> None:
    """会话结束时清理注册表。"""
    _registries.pop(session_id, None)

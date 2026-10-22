"""fs_read 未变化短路（按会话）。

同一文件、同一行范围在未修改时，不必把相同内容再发一遍。但存根只能在“较早的读取结果仍完整在上下文中”时
返回：若那条结果已被微压缩折叠为占位符，或已随压缩摘要移出上下文，存根就会指向一个模型已经看不到的内容，
此时必须返回全文。重启后按库中历史重建上下文，判断口径相同（历史里仍有完整结果即命中）。
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def norm_path(path: object) -> str:
    """读取记录与写入校验统一用的路径口径（fs_read 与 fs_write 解析路径的方式不同，需先规范化）。"""
    return os.path.normcase(os.path.abspath(str(path)))


# 存根文案：告诉模型去看上文那条工具结果，而不是让它以为文件内容丢了
UNCHANGED_STUB = (
    "（文件自上次读取后未变化，同一路径与行范围的内容仍在上文的工具结果中，请直接参考它。"
    "如需重新查看，请换一个行范围，或先修改文件。）"
)

# 微压缩折叠占位符的开头（见 compaction.py 的 _micro_compact）
_FOLDED_PREFIX = "[内容已折叠"

_MAX_ENTRIES = 2000  # 每会话的记录上限，超出时丢弃最早的记录


class ReadWindowCache:
    def __init__(self) -> None:
        self._messages: Callable[[], list[Any]] = lambda: []
        self._seen: dict[tuple, tuple[tuple[int, int], str]] = {}
        # 本会话里由模型自己写入 / 修改过的文件：内容它已经知道，再次整体重写不需要先读
        self._known: set[str] = set()

    def bind(self, messages_getter: Callable[[], list[Any]]) -> None:
        """每轮绑定当前的消息列表（列表可能被压缩替换，故用 getter 而非引用）。"""
        self._messages = messages_getter

    def lookup(self, key: tuple, fingerprint: tuple[int, int]) -> bool:
        """同一键、同一指纹，且较早的结果仍完整可见时返回 True。"""
        prev = self._seen.get(key)
        if prev is None or prev[0] != fingerprint:
            return False
        call_id = prev[1]
        for m in self._messages() or []:
            if getattr(m, "role", None) == "tool" and getattr(m, "tool_call_id", None) == call_id:
                content = getattr(m, "content", "") or ""
                return bool(content) and not content.startswith(_FOLDED_PREFIX)
        return False

    def record(self, key: tuple, fingerprint: tuple[int, int], call_id: str | None) -> None:
        if not call_id:
            return
        if len(self._seen) >= _MAX_ENTRIES and key not in self._seen:
            self._seen.pop(next(iter(self._seen)))
        self._seen[key] = (fingerprint, call_id)

    def has_any(self) -> bool:
        """本进程内本会话是否登记过任何读取（重启后为空，此时不做“先读后写”强制）。"""
        return bool(self._seen)

    def mark_known(self, path: object) -> None:
        """登记模型自己写入 / 修改过的文件（之后整体重写不算「未读覆盖」）。"""
        self._known.add(norm_path(path))

    def has_read(self, path: object) -> bool:
        """本会话内是否读取过（或自己写入过）该文件。"""
        target = norm_path(path)
        return target in self._known or any(k[0] == target for k in self._seen)


_CACHES: dict[int, ReadWindowCache] = {}


def read_cache_for(session_id: int, messages_getter: Callable[[], list[Any]]) -> ReadWindowCache:
    """取得（或新建）会话的读取缓存，并绑定本轮的消息列表。"""
    cache = _CACHES.get(session_id)
    if cache is None:
        cache = ReadWindowCache()
        _CACHES[session_id] = cache
    cache.bind(messages_getter)
    return cache

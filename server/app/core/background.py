"""后台任务的统一启动入口（plan-156-620）。

`loop.create_task` 返回的任务只被事件循环弱引用：调用方不保存引用时，
任务可能在执行中途被 GC 回收（Python 官方文档明确警告）。
各模块此前各自 `create_task(...)` 后丢弃返回值——子会话的首轮执行、标题归纳都受影响。
这里集中持有强引用，任务结束后自动释放。
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_BACKGROUND_TASKS: set[asyncio.Task] = set()


def spawn_background(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """在当前事件循环启动后台任务并持有强引用。无运行中的事件循环时抛 RuntimeError。"""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task

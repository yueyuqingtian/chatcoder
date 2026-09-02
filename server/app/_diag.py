"""工具错误诊断日志（v36）：把 traceback 与调用上下文一并落盘。

背景：agent_loop 此前用 f"[工具执行异常] {exc}" 把异常转成字符串，
traceback 随之丢失，事后只能看到一行消息，无法定位抛错的文件与行号。
本模块提供异常格式化与结构化诊断日志，供 agent_loop / executor 共用。

输出目标：独立诊断日志文件（diagnostics.log），与业务日志分离，
便于出问题时直接检索 [tool.error] 拿到完整现场。
"""
from __future__ import annotations

import logging
import traceback
from typing import Any

logger = logging.getLogger("app.diagnostics")


def format_exc_chain(exc: BaseException) -> str:
    """格式化异常链（含 cause / context），保留完整 traceback。

    单个 traceback.format_exc() 只覆盖最外层异常；
    __cause__ / __context__ 常常才是真正的根因（如 TypeError 由解包失败引起）。
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(
            "".join(
                traceback.format_exception(
                    type(current), current, current.__traceback__
                )
            ).rstrip()
        )
        current = current.__cause__ or current.__context__
    return "\n\n--- caused by ---\n\n".join(parts) if parts else "<无 traceback>"


def summarize_args(args: Any, limit: int = 500) -> str:
    """参数摘要：写盘工具重点关注 path / old_text / new_text。

    不做 json.dumps（避免非序列化对象抛错打断日志），用 repr 截断，
    保证任何参数都能安全记录。
    """
    if not isinstance(args, dict):
        text = repr(args)
        return text if len(text) <= limit else text[:limit] + "...(已截断)"
    keys = ("path", "paths", "edits", "old_text", "new_text", "command", "cwd")
    chunks: list[str] = []
    for key in keys:
        if key not in args:
            continue
        value = repr(args[key])
        if len(value) > 200:
            value = value[:200] + "...(已截断)"
        chunks.append(f"{key}={value}")
    if not chunks:
        for key, value in list(args.items())[:10]:
            chunks.append(f"{key}={repr(value)[:120]}")
    text = " ".join(chunks)
    return text if len(text) <= limit else text[:limit] + "...(已截断)"


def log_tool_error(
    *,
    turn_id: Any,
    step: Any,
    tool_name: str,
    call_key: str,
    exc: BaseException,
    args: Any = None,
    phase: str = "execute",
) -> None:
    """记录工具失败的完整现场：异常类型、消息、参数摘要、完整 traceback。

    phase 区分失败发生的阶段（execute=执行 / precheck=审批前置检查 /
    record=写盘记录），用于判断异常发生在工具内部还是外围包装逻辑。
    """
    logger.error(
        "[tool.error] phase=%s turn=%s step=%s tool=%s call_key=%s "
        "exc_type=%s exc=%s\nargs=%s\n--- traceback ---\n%s",
        phase, turn_id, step, tool_name, call_key,
        type(exc).__name__, exc, summarize_args(args),
        format_exc_chain(exc),
    )

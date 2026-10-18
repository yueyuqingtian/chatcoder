"""子会话 / 子代理 / 工作流结论的分段传递（plan-156-620）。

结论原文此前有多处硬截断：子会话结束回报截到 1200 字、进度查询只给 600 字的步骤标题、
子代理结论截到 4000 字、工作流节点产出落库时就截断。主代理拿到的是半截结论。

现在的口径：**结论原文不做静默截断**，单次放不下时按固定页长分段，
每段末尾写明「共多少字、已给到哪里、下一段用什么调用取」（通用实现见 output_paging）。
页长与工具结果上限（agent_loop.MAX_TOOL_OUTPUT_CHARS = 16000）对齐并留出格式余量。
"""
from __future__ import annotations

from app.orchestration.output_paging import TextPage, page_of, paging_hint, tool_ref

# 兼容既有引用名
ReportPage = TextPage

# 主代理空闲时落库的子会话汇报消息的内容标记（context_manager 据此把它作为独立输入交给主代理）
SUBSESSION_REPORT_FLAG = "subsession_report"

# 单段结论的最大字符数（工具结果上限 16000，扣掉标题 / 状态 / 续读提示的余量）
REPORT_PAGE_CHARS = 12000
# 汇总多个子会话 / 子代理时，每个结论先给出的开头长度（总量可控，续读提示指向单个对象）
REPORT_PREVIEW_CHARS = 1500

__all__ = [
    "REPORT_PAGE_CHARS", "REPORT_PREVIEW_CHARS", "ReportPage", "SUBSESSION_REPORT_FLAG",
    "continuation_hint", "page_of", "paging_hint", "tool_ref",
]


def continuation_hint(page: TextPage, subsession_id: int) -> str:
    """子会话结论的续读提示。"""
    return paging_hint(
        page, tool_ref("get_subsession_progress", subsession_id=subsession_id, report_offset="{offset}"),
    )

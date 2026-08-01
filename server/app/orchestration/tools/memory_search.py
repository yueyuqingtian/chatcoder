"""v0.8: memory.search 工具(low risk,免审批)。

让 AI 能按关键词检索会话更早的历史消息(已被摘要压缩掉的部分)。
返回匹配的消息列表，供 AI 补全上下文。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.persistence.database import async_session_factory


class MemorySearchTool(Tool):
    name = "memory_search"
    risk_level = "low"
    description = (
        "在当前会话的历史消息中按关键词检索(包括已被摘要压缩的早期消息)。"
        "当需要回忆之前讨论过的细节、决策、任务安排时使用。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "搜索关键词(如任务名、人名、技术名词)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回条数(默认10)",
                        },
                    },
                    "required": ["keyword"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        keyword = str(args.get("keyword", "")).strip()
        limit = int(args.get("limit", 10))
        if not keyword:
            return ToolResult(ok=False, output="", error="keyword 不能为空")

        from app.orchestration.context_memory import search_session_memory

        async with async_session_factory() as db:
            msgs = await search_session_memory(db, ctx.session_id, keyword, limit)

        if not msgs:
            return ToolResult(
                ok=True,
                output=f"未找到包含 '{keyword}' 的历史消息。",
                data={"keyword": keyword, "count": 0},
            )

        lines = [f"找到 {len(msgs)} 条相关历史消息:"]
        for m in msgs:
            speaker = m.sender_type
            if m.sender_type == "agent":
                speaker = m.content.get("agent_name") or f"agent#{m.sender_id}"
            text = m.content.get("text") or m.content.get("note") or "(非文本)"
            # 单条截断
            if len(text) > 300:
                text = text[:300] + "..."
            thread_tag = f"[子会话#{m.thread_id}]" if m.thread_id else "[主群]"
            lines.append(f"{thread_tag}[{speaker}] {text}")

        return ToolResult(
            ok=True,
            output="\n".join(lines),
            data={"keyword": keyword, "count": len(msgs)},
        )

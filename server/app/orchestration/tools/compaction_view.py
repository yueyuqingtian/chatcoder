"""v30.1: 压缩索引查看工具（compaction_index / compaction_view）。

上下文压缩后，被压缩的早期会话内容以"压缩块"形式存在（软阴影，物理保留在
messages 表）。两个工具让 AI 按需查看压缩前的会话信息：

1. compaction_index —— 列出会话内全部压缩块索引（序号/覆盖范围/节省 token/
   摘要预览），AI 需要回忆被压缩内容时先定位索引；
2. compaction_view —— 按索引（序号或 compaction_id）取某个压缩块遮蔽的
   原始消息完整原文，供 AI 按需补全上下文。

均 low risk 免审批（纯读操作，无副作用）。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.persistence.database import async_session_factory


def _message_to_text(m) -> str:
    """把 DB Message 转成 AI 可读的文本行。"""
    from app.core.enums import MsgType

    c = m.content if isinstance(m.content, dict) else {}
    if m.msg_type == MsgType.TOOL_CALL.value:
        tool = str(c.get("tool") or "unknown")
        args = c.get("args") or {}
        import json
        try:
            args_str = json.dumps(args, ensure_ascii=False)[:200]
        except (TypeError, ValueError):
            args_str = str(args)[:200]
        return f"[工具调用] {tool}({args_str})"
    if m.msg_type == MsgType.TOOL_RESULT.value:
        out = str(c.get("output") or c.get("error") or "(无输出)")
        return f"[工具结果] {out[:300]}"
    if m.msg_type == MsgType.THINKING.value:
        return f"[思考] {(str(c.get('text') or ''))[:200]}"
    speaker = "用户" if m.sender_type == "user" else (
        str(c.get("agent_name") or f"agent#{m.sender_id}")
        if m.sender_type == "agent" else "系统"
    )
    text = str(c.get("text") or c.get("note") or "(非文本)")
    return f"[{speaker}] {text[:400]}"


class CompactionIndexTool(Tool):
    name = "compaction_index"
    risk_level = "low"
    description = (
        "列出当前会话内全部上下文压缩块的索引（序号/覆盖消息范围/节省 token/"
        "摘要预览）。上下文被压缩后，需要回忆早期会话细节时先调用本工具定位索引，"
        "再调用 compaction_view 按索引查看压缩前的原始消息。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from app.services import compression_service

        async with async_session_factory() as db:
            entries = await compression_service.list_compaction_index(db, ctx.session_id)
        if not entries:
            return ToolResult(ok=True, output="当前会话没有上下文压缩块。", data={"count": 0})

        lines = [f"当前会话共 {len(entries)} 个上下文压缩块（按压缩时间排序，可使用 compaction_view 查看原文）:"]
        for e in entries:
            idx = e.get("index") or "-"
            cid = e.get("compaction_id") or "-"
            n = len(e.get("shadowed_ids") or [])
            saved = e.get("saved_tokens", 0)
            trigger = "溢出恢复" if e.get("trigger") == "context-overflow" else "压力"
            preview = (e.get("summary_preview") or "").replace("\n", " ")[:120]
            lines.append(
                f"- #{idx} [{trigger}] 遮蔽 {n} 条消息, 节省 {saved} tokens, "
                f"compaction_id={cid}\n  摘要: {preview}"
            )
        return ToolResult(ok=True, output="\n".join(lines), data={"count": len(entries)})


class CompactionViewTool(Tool):
    name = "compaction_view"
    risk_level = "low"
    description = (
        "按索引查看某个上下文压缩块遮蔽的压缩前完整会话消息（原文）。"
        "参数二选一：index=压缩块序号（compaction_index 返回的 #序号，从 1 起）；"
        "或 compaction_id=压缩块 id。当需要回忆被压缩早期会话的具体内容时使用。"
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
                        "index": {
                            "type": "integer",
                            "description": "压缩块序号（compaction_index 的 #序号，从 1 起）",
                        },
                        "compaction_id": {
                            "type": "string",
                            "description": "压缩块 id（compaction_index 返回的 compaction_id）",
                        },
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from app.services import compression_service

        index = args.get("index")
        compaction_id = str(args.get("compaction_id") or "").strip()
        if not compaction_id and index is None:
            return ToolResult(ok=False, output="", error="必须提供 index 或 compaction_id 之一")

        async with async_session_factory() as db:
            if not compaction_id:
                entries = await compression_service.list_compaction_index(db, ctx.session_id)
                try:
                    target = entries[int(index) - 1]
                except (ValueError, IndexError):
                    return ToolResult(
                        ok=False, output="", error=f"压缩块序号 {index} 不存在（共 {len(entries)} 个）",
                    )
                compaction_id = target.get("compaction_id") or ""
            try:
                msgs = await compression_service.get_compacted_messages(db, ctx.session_id, compaction_id)
            except KeyError as e:
                return ToolResult(ok=False, output="", error=str(e))

        if not msgs:
            return ToolResult(ok=True, output=f"压缩块 {compaction_id} 没有遮蔽消息。", data={"count": 0})

        lines = [f"压缩块 {compaction_id} 的压缩前会话消息（共 {len(msgs)} 条）:"]
        for m in msgs:
            lines.append(f"[#{m.id}] {_message_to_text(m)}")
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            data={"compaction_id": compaction_id, "count": len(msgs)},
        )

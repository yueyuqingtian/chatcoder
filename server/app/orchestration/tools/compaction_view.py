"""v30.1 / plan-230-1144 M4.1: 压缩索引查看工具（compaction_index / compaction_view）。

上下文压缩后，被压缩的早期会话内容以"压缩块"形式存在（软阴影，物理保留在
messages 表）。两个工具让 AI 按需查看压缩前的会话信息：

1. compaction_index —— 列出会话内全部压缩块索引（序号/覆盖范围/节省 token/
   摘要预览），AI 需要回忆被压缩内容时先定位索引；
2. compaction_view —— 按索引（序号或 compaction_id）取某个压缩块遮蔽的
   原始消息，支持 offset/limit 分页与 keyword 过滤；默认单条截断防炸上下文，
   显式要求 full=true 时可取单条全文（M4.1 改造：解除此前 300/400 字硬截断）。

均 low risk 免审批（纯读操作，无副作用）；四种权限模式全部可用。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.persistence.database import async_session_factory

# 默认单条截断（防一次拉爆上下文）；full=True 时放开
_DEFAULT_SNIPPET = 400


def _message_to_text(m, *, snippet: int | None = _DEFAULT_SNIPPET) -> str:
    """把 DB Message 转成 AI 可读的文本行。snippet=None 时返回全文。"""
    from app.core.enums import MsgType

    c = m.content if isinstance(m.content, dict) else {}

    def _cut(s: str) -> str:
        if snippet is None or len(s) <= snippet:
            return s
        return s[:snippet] + f"…(截断，共 {len(s)} 字符，可 full=true 取全文)"

    if m.msg_type == MsgType.TOOL_CALL.value:
        tool = str(c.get("tool") or "unknown")
        args = c.get("args") or {}
        import json
        try:
            args_str = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = str(args)
        return f"[工具调用] {tool}({_cut(args_str)})"
    if m.msg_type == MsgType.TOOL_RESULT.value:
        out = str(c.get("output") or c.get("error") or "(无输出)")
        return f"[工具结果] {_cut(out)}"
    if m.msg_type == MsgType.THINKING.value:
        return f"[思考] {_cut(str(c.get('text') or ''))}"
    speaker = "用户" if m.sender_type == "user" else (
        str(c.get("agent_name") or f"agent#{m.sender_id}")
        if m.sender_type == "agent" else "系统"
    )
    text = str(c.get("text") or c.get("note") or "(非文本)")
    return f"[{speaker}] {_cut(text)}"


class CompactionIndexTool(Tool):
    name = "compaction_index"
    risk_level = "low"
    description = (
        "【按需回看第 1 步】列出当前会话内全部上下文压缩块的索引（序号/覆盖消息范围/节省 token/"
        "摘要预览）。上下文被压缩后，需要回忆早期会话细节时先调用本工具定位索引，"
        "再调用 compaction_view 按索引查看压缩前的原始消息（只取需要的部分，**不要全量拉取**）。"
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
        "【按需回看第 2 步】按索引查看某个上下文压缩块遮蔽的压缩前会话消息。参数二选一："
        "index=压缩块序号（compaction_index 返回的 #序号，从 1 起）；"
        "或 compaction_id=压缩块 id。支持 offset/limit 分页与 keyword 过滤；"
        "单条消息默认截断 400 字符，需要某条全文时用 full=true。"
        "**按需取用**：先用 compaction_index 定位块，再用 keyword / offset / limit 只取需要的几条；"
        "**严禁一次性全量拉取**压缩前历史（会重新撑爆上下文、抵消压缩效果）。"
        "当需要回忆被压缩早期会话的具体内容时使用。"
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
                        "keyword": {
                            "type": "string",
                            "description": "只返回包含该关键词的消息（不区分大小写）",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "分页起始偏移（默认 0）",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回条数（默认 30，最大 200）",
                        },
                        "full": {
                            "type": "boolean",
                            "description": "true=单条消息不截断返回全文（默认 false，单条截 400 字符）",
                        },
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from app.services import compression_service

        index = args.get("index")
        compaction_id = str(args.get("compaction_id") or "").strip()
        keyword = str(args.get("keyword") or "").strip().casefold()
        try:
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = max(1, min(int(args.get("limit") or 30), 200))
        except (TypeError, ValueError):
            limit = 30
        full = bool(args.get("full"))

        if not compaction_id and index is None:
            return ToolResult(ok=False, output="", error="必须提供 index 或 compaction_id 之一")

        async with async_session_factory() as db:
            if not compaction_id:
                entries = await compression_service.list_compaction_index(db, ctx.session_id)
                try:
                    target = entries[int(index) - 1]
                except (ValueError, IndexError, TypeError):
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

        msgs = list(msgs)
        # keyword 过滤（M4.1：此前只能顺序浏览，无法定位具体消息）
        if keyword:
            msgs = [m for m in msgs if keyword in _message_to_text(m, snippet=None).casefold()]
        total = len(msgs)
        page = msgs[offset:offset + limit]

        if not page:
            return ToolResult(
                ok=True,
                output=(
                    f"压缩块 {compaction_id} 命中 {total} 条消息，但当前分页（offset={offset}, limit={limit}）为空。"
                    if total else f"压缩块 {compaction_id} 中未找到包含 '{keyword}' 的消息。"
                ),
                data={"compaction_id": compaction_id, "total": total, "returned": 0},
            )

        snippet = None if full else _DEFAULT_SNIPPET
        lines = [
            f"压缩块 {compaction_id} 的压缩前会话消息"
            f"（命中 {total} 条，本页 {len(page)} 条，offset={offset}）:"
        ]
        for m in page:
            lines.append(f"[#{m.id}] {_message_to_text(m, snippet=snippet)}")
        if offset + len(page) < total:
            lines.append(f"…还有 {total - offset - len(page)} 条，可增加 offset 继续翻页。")
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            data={"compaction_id": compaction_id, "total": total, "returned": len(page), "offset": offset},
        )
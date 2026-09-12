"""v0.8 / plan-230-1144 M4.1: memory.search 工具(low risk,免审批)。

三源联合检索（此前只查会话历史消息，命名与语义错位——与 MemoryEntry 记忆库无关）：
1. **记忆库**（MemoryEntry）：AI 记录/抽取的长期记忆（session/project/global 三层），
   含候选区（低置信未注入 prompt 的记忆）；
2. **会话历史消息**：按关键词检索主群 + 子会话的全部消息（含已被摘要压缩的）；
3. **压缩块**：被落库式压缩遮蔽的历史消息（标注所属压缩块）。

结果按来源分组并注明出处层，AI 据此判断可信度与时效。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.persistence.database import async_session_factory


class MemorySearchTool(Tool):
    name = "memory_search"
    risk_level = "low"
    description = (
        "跨来源检索记忆：同时搜索①记忆库（长期记忆条目，含项目/全局层）、"
        "②当前会话全部历史消息（含被摘要/压缩的早期消息）、③被压缩遮蔽的原始消息。"
        "当需要回忆之前讨论过的细节、决策、任务安排、项目约定时使用。"
        "结果会标注来源（记忆库/历史消息/压缩块）。"
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
                            "description": "每类来源最多返回条数(默认10)",
                        },
                        "sources": {
                            "type": "string",
                            "description": "逗号分隔的来源过滤：memory,history,compaction（默认全部）",
                        },
                    },
                    "required": ["keyword"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        keyword = str(args.get("keyword", "")).strip()
        try:
            limit = int(args.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 50))
        raw_sources = str(args.get("sources") or "memory,history,compaction").lower()
        sources = {s.strip() for s in raw_sources.split(",") if s.strip()}

        if not keyword:
            return ToolResult(ok=False, output="", error="keyword 不能为空")

        sections: list[str] = []
        data: dict = {"keyword": keyword, "memory": 0, "history": 0, "compaction": 0}
        kw_lower = keyword.casefold()

        async with async_session_factory() as db:
            # ── 源 1: 记忆库（三层 + 候选区）──
            if "memory" in sources:
                try:
                    from sqlalchemy import select

                    from app.persistence.models.memory import MemoryEntry
                    res = await db.execute(
                        select(MemoryEntry)
                        .where(MemoryEntry.text.ilike(f"%{keyword}%"))
                        .order_by(MemoryEntry.usage_count.desc())
                        .limit(limit)
                    )
                    mems = list(res.scalars().all())
                    if mems:
                        _scope_tag = {"global": "全局", "project": "项目", "session": "会话"}
                        lines = ["【记忆库】"]
                        for e in mems:
                            tag = _scope_tag.get(e.scope or "session", e.scope or "session")
                            cand = "（候选·未注入）" if e.candidate else ""
                            lines.append(f"- [{tag}/{e.kind}{cand}] {e.text}")
                        sections.append("\n".join(lines))
                        data["memory"] = len(mems)
                except Exception:
                    pass

            # ── 源 2: 会话历史消息 ──
            if "history" in sources:
                try:
                    from app.orchestration.context_memory import search_session_memory
                    msgs = await search_session_memory(db, ctx.session_id, keyword, limit)
                    if msgs:
                        lines = ["【历史消息】"]
                        for m in msgs:
                            speaker = m.sender_type
                            if m.sender_type == "agent":
                                speaker = m.content.get("agent_name") or f"agent#{m.sender_id}"
                            text = m.content.get("text") or m.content.get("note") or "(非文本)"
                            if len(text) > 300:
                                text = text[:300] + "..."
                            thread_tag = f"[子会话#{m.thread_id}]" if m.thread_id else "[主群]"
                            lines.append(f"- {thread_tag}[{speaker}] {text}")
                        sections.append("\n".join(lines))
                        data["history"] = len(msgs)
                except Exception:
                    pass

            # ── 源 3: 压缩块（被遮蔽的原始消息）──
            if "compaction" in sources:
                try:
                    from app.services import compression_service
                    entries = await compression_service.list_compaction_index(db, ctx.session_id)
                    hits: list[str] = []
                    for e in entries:
                        cid = e.get("compaction_id") or ""
                        idx = e.get("index") or "-"
                        summary_hit = kw_lower in str(e.get("summary_preview") or "").casefold()
                        # 摘要未命中时深入被遮蔽消息检索（限量防炸）
                        found: list[str] = []
                        if not summary_hit and cid:
                            try:
                                hidden = await compression_service.get_compacted_messages(
                                    db, ctx.session_id, cid,
                                )
                            except Exception:
                                hidden = []
                            for m in _flatten(hidden)[:400]:
                                text = _msg_text(m)
                                if kw_lower in text.casefold():
                                    found.append(f"    · [#{getattr(m, 'id', '?')}] {text[:200]}")
                                    if len(found) >= limit:
                                        break
                        if summary_hit or found:
                            head = f"- 压缩块 #{idx}（compaction_id={cid}）："
                            if summary_hit:
                                head += f"摘要命中「{str(e.get('summary_preview') or '')[:120]}」"
                            hits.append(head)
                            hits.extend(found)
                    if hits:
                        sections.append("【压缩块】\n" + "\n".join(hits))
                        data["compaction"] = len(hits)
                except Exception:
                    pass

        total = data["memory"] + data["history"] + data["compaction"]
        if total == 0:
            return ToolResult(
                ok=True,
                output=f"未找到与 '{keyword}' 相关的记忆/历史/压缩内容。",
                data=data,
            )

        header = (
            f"关键词 '{keyword}' 检索结果："
            f"记忆库 {data['memory']} 条 · 历史消息 {data['history']} 条 · 压缩块 {data['compaction']} 条"
        )
        return ToolResult(ok=True, output=header + "\n\n" + "\n\n".join(sections), data=data)


def _flatten(items) -> list:
    """get_compacted_messages 返回值可能是消息列表或含列表的 dict，统一为列表。"""
    if isinstance(items, dict):
        for key in ("messages", "items", "shadowed"):
            v = items.get(key)
            if isinstance(v, list):
                return v
        return []
    if isinstance(items, list):
        return items
    return []


def _msg_text(m) -> str:
    c = m.content if isinstance(getattr(m, "content", None), dict) else {}
    return str(c.get("text") or c.get("output") or c.get("error") or c.get("note") or "")

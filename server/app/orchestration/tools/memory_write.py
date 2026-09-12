"""memory_write 工具（plan-230-1144 M4.1）。

改造前问题：记忆只有 turn 结束后由 LLM 抽取的被动路径（agent_loop 的
memory consolidation），AI 无法在对话中途主动记录"这个事实很重要，记下来"。
本工具补齐主动写入能力：

- scope=session：本会话事实（默认，30 天过期）
- scope=project：项目约定/坑点（需会话绑定项目，永不过期）
- scope=global：跨项目规范/偏好（永不过期）

low risk：写入的是记忆库（非用户文件系统），去重 + 长度门槛（8 字符）兜底；
scope=project 的词条挂 project_id（由 save_memories 直接落库，无需事后回填）。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

_KINDS = ("fact", "convention", "pitfall", "decision")


class MemoryWriteTool(Tool):
    name = "memory_write"
    risk_level = "low"
    description = (
        "主动记录一条长期有价值的记忆（事实/约定/坑点/决策），跨 turn 生效。"
        "适用：用户明确要求记住某事；发现了重要项目约定、易踩的坑、关键决策。"
        "scope 选择：session=本会话（默认）；project=本项目长期有效；global=跨项目全局规范。"
        "不要记录临时性内容（如单次命令输出、当前任务进度）。"
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
                        "text": {"type": "string", "description": "记忆内容（一句话，8 字符以上）"},
                        "kind": {
                            "type": "string",
                            "description": "分类：fact 事实 / convention 约定 / pitfall 坑点 / decision 决策",
                        },
                        "scope": {
                            "type": "string",
                            "description": "作用域：session（默认）/ project / global",
                        },
                    },
                    "required": ["text"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        text = str(args.get("text") or "").strip()
        if len(text) < 8:
            return ToolResult(ok=False, output="", error="记忆内容过短（至少 8 字符）")
        kind = str(args.get("kind") or "fact")
        if kind not in _KINDS:
            kind = "fact"
        scope = str(args.get("scope") or "session")
        if scope not in ("session", "project", "global"):
            scope = "session"

        from app.persistence.database import async_session_factory

        # scope=project 需要当前项目 id：从会话反查
        project_id = None
        if scope == "project":
            try:
                from app.persistence.models.session import Session
                async with async_session_factory() as db:
                    sess = await db.get(Session, ctx.session_id)
                    project_id = getattr(sess, "project_id", None) if sess else None
            except Exception:
                project_id = None
            if project_id is None:
                return ToolResult(
                    ok=False, output="",
                    error="当前会话未绑定项目，无法写入项目级记忆（可改用 scope=session 或 global）",
                )

        from app.services import memory_service

        async with async_session_factory() as db:
            count = await memory_service.save_memories(
                db, session_id=ctx.session_id, turn_id=None,
                memories=[{"text": text, "kind": kind, "scope": scope, "importance": 1.0}],
                project_id=project_id,
            )

        if count:
            return ToolResult(
                ok=True,
                output=f"已记录记忆（scope={scope}, kind={kind}）：{text}",
                data={"count": count, "scope": scope},
            )
        return ToolResult(ok=True, output="记忆已存在（去重跳过），未重复写入。", data={"count": 0})

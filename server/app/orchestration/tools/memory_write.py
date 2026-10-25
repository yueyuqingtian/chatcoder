"""memory_write 工具（plan-230-1144 M4.1）。

改造前问题：记忆只有 turn 结束后由 LLM 抽取的被动路径（agent_loop 的
memory consolidation），AI 无法在对话中途主动记录"这个事实很重要，记下来"。
本工具补齐主动写入能力：

- scope=session：本会话事实（默认，30 天过期）
- scope=project：项目约定/坑点（需会话绑定项目，永不过期）
- scope=global：跨项目规范/偏好（永不过期）

low risk：写入的是记忆库（非用户文件系统），去重 + 长度门槛（8 字符）兜底；
scope=project 的词条挂 project_id（由 save_memories 直接落库，无需事后回填）。

plan-548-2276 阶段 1：新增可选 `keywords`（决定后续能否被检索到），落库时带
`origin="manual"`；会话→项目的反查改走 `memory_service.resolve_project_id`，
与检索侧共用同一口径。
"""
import re
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

_KINDS = ("fact", "convention", "pitfall", "decision")


class MemoryWriteTool(Tool):
    name = "memory_write"
    risk_level = "low"
    description = (
        "主动记录一条长期有价值的记忆（事实/约定/坑点/决策），跨 turn 生效。"
        "适用：用户明确要求记住某事；用户纠正你的做法或明确肯定某个非显然做法；"
        "发现了重要项目约定、易踩的坑、关键决策。"
        "写入前先用 memory_search 查同主题是否已有记忆——能更新（edit/promote）就不要新增重复条目。"
        "scope 选择口径：session=本会话事实（默认，30 天后过期）；"
        "project=本项目长期有效（需会话已绑定项目，能从代码 30 秒查到的不要记）；"
        "global=跨项目通用的规范/偏好（写前自问「换一个项目，这句话还成立吗」，成立才写 global）。"
        "keywords 请给 3-8 个检索词（中英混合可以），它决定以后能不能被搜到；不填则由正文自动派生。"
        "不要记录：代码结构、文件路径、依赖选择；git 历史与「谁改了什么」；"
        "调试修复步骤；AGENTS.md/CLAUDE.md 已写明的规则；当前任务进度与临时状态"
        "（如单次命令输出、进行中的待办）。"
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
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "检索关键词 3-8 个（如 [\"上下文压缩\",\"compaction\",\"摘要\"]）；"
                                "后续 memory_search 靠它命中，留空则由正文自动派生"
                            ),
                        },
                    },
                    "required": ["text"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # plan-537-2256：开关必须同时挡在工具入口与 save_memories（后端唯一写入口），
        # 否则用户关掉记忆后模型仍能从工具侧写入。
        from app.core.config import settings

        if not getattr(settings, "auto_memory_enabled", True):
            return ToolResult(ok=False, output="", error="记忆功能已在「设置 → 记忆」中关闭。")

        text = str(args.get("text") or "").strip()
        if len(text) < 8:
            return ToolResult(ok=False, output="", error="记忆内容过短（至少 8 字符）")
        kind = str(args.get("kind") or "fact")
        if kind not in _KINDS:
            kind = "fact"
        scope = str(args.get("scope") or "session")
        if scope not in ("session", "project", "global"):
            scope = "session"
        keywords = _parse_keywords_arg(args.get("keywords"))

        from app.persistence.database import async_session_factory

        # scope=project 需要当前项目 id：反查逻辑收敛在 memory_service.resolve_project_id
        # （plan-548-2276 阶段 0；此前写在本工具里并吞掉异常，检索侧需要同一口径）
        project_id = None
        scope_note = ""
        from app.services import memory_service

        if scope == "project":
            async with async_session_factory() as db:
                project_id = await memory_service.resolve_project_id(db, ctx.session_id)
            if project_id is None:
                # 会话确实没有绑定项目：改存会话级而不是让整次调用失败（内容不丢，模型无需重试）
                scope = "session"
                scope_note = "（当前会话未绑定项目，已改存为会话级记忆）"

        async with async_session_factory() as db:
            count = await memory_service.save_memories(
                db, session_id=ctx.session_id, turn_id=None,
                memories=[{
                    "text": text, "kind": kind, "scope": scope, "importance": 1.0,
                    "keywords": keywords or None,
                }],
                project_id=project_id,
                origin="manual",
            )

        if count:
            return ToolResult(
                ok=True,
                output=f"已记录记忆（scope={scope}, kind={kind}）{scope_note}：{text}",
                data={"count": count, "scope": scope, "keywords": keywords or []},
            )
        return ToolResult(ok=True, output="记忆已存在（去重跳过），未重复写入。", data={"count": 0})


def _parse_keywords_arg(raw) -> list[str]:
    """keywords 参数兼容两种写法：字符串数组，或逗号/空白分隔的一串词。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = re.split(r"[,，;；\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        return []
    out: list[str] = []
    for item in items:
        word = item.strip()
        if word and word not in out:
            out.append(word)
    return out[:8]

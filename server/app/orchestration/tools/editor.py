"""v0.3: editor.apply_diff 工具(medium risk,走审批)。

简化版:仅支持对单个文件应用 unified diff(基于 Python 标准库 difflib 不易反向,
故采用 "整文件替换 + 旧片段定位" 策略;若失败提示 agent 改用 fs.write)。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve


class EditorApplyDiffTool(Tool):
    name = "editor_apply_diff"
    risk_level = "medium"
    description = "对工作区内文件应用 diff:用 new_text 替换文件中出现的 old_text(默认必须唯一匹配,replace_all=true 时替换所有匹配)。需用户审批。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "工作区内文件相对路径(如 'clinic/src/Main.java';也可用绝对路径)"},
                        "old_text": {"type": "string", "description": "要被替换的原文(默认必须唯一匹配)"},
                        "new_text": {"type": "string", "description": "替换后的文本"},
                        "replace_all": {"type": "boolean", "description": "true=替换文件中所有匹配的 old_text；false=仅唯一匹配时替换，多匹配报错(默认)"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        replace_all = bool(args.get("replace_all", False))
        target = safe_resolve(ctx.workspace_root, path)
        if target is None:
            return ToolResult(ok=False, output="", error=f"路径越界或非法: {path}")
        if not target.exists():
            return ToolResult(ok=False, output="", error=f"文件不存在: {path}")
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"读取失败: {e}")
        count = content.count(old_text)
        if count == 0:
            return ToolResult(ok=False, output="", error="old_text 在文件中未匹配")
        if count > 1 and not replace_all:
            return ToolResult(
                ok=False, output="", error=f"old_text 匹配 {count} 处,需更唯一的上下文(或设置 replace_all=true)"
            )
        new_content = content.replace(old_text, new_text, -1 if replace_all else 1)
        try:
            target.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"写入失败: {e}")
        return ToolResult(
            ok=True,
            output=f"已应用 diff 到 {path}",
            data={"path": path, "delta": len(new_text) - len(old_text)},
        )

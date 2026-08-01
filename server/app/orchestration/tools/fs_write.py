"""v0.3: fs.write 工具(high risk,走审批)。

v2.5: 使用 safe_resolve_parent 检查路径合法性(新文件可能尚不存在)。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve_parent


class FsWriteTool(Tool):
    name = "fs_write"
    risk_level = "high"
    description = (
        "在工作区内写入文本文件(覆盖已有文件或创建新文件)。需用户审批。\n"
        "path 使用相对路径(如 'clinic/docs/report.md'),也可用绝对路径。\n"
        "如果目录不存在会自动创建。"
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
                        "path": {
                            "type": "string",
                            "description": (
                                "文件路径(相对工作根,如 'clinic/docs/report.md';"
                                "或绝对路径)"
                            ),
                        },
                        "content": {"type": "string", "description": "写入的文本内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return ToolResult(ok=False, output="", error="path 不能为空")

        target = safe_resolve_parent(ctx.workspace_root, path)
        if target is None:
            return ToolResult(
                ok=False, output="",
                error=(
                    f"路径越界或非法: {path}\n"
                    f"请确认路径在工作目录内: {ctx.workspace_root}"
                ),
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"写入失败: {e}")

        # 计算相对路径用于输出显示
        try:
            display = str(target.relative_to(ctx.workspace_root))
        except ValueError:
            display = str(target)

        return ToolResult(
            ok=True,
            output=f"已写入 {len(content)} 字符到 {display}",
            data={"path": display, "bytes": len(content.encode("utf-8"))},
        )

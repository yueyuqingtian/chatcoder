"""fs_read 工具 — 读取文件内容(支持行号范围)。

参照 CodeBuddy/Codex 的 read_file 行为:
- 自动显示行号
- 支持 offset/limit 参数限定读取范围
- 大文件自动提示行号范围
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve

_MAX_CHARS = 8000
_MAX_LINES = 200


class FsReadTool(Tool):
    name = "fs_read"
    risk_level = "low"
    description = (
        "读取工作区内指定文件的文本内容，自动显示行号。\n"
        "支持通过 offset 和 limit 参数限定读取的行号范围（类似 Codex/CodeBuddy 的行为），"
        "适用于大文件分段阅读。\n"
        "- path: 文件路径（相对工作根，如 'clinic/pom.xml'，也可用绝对路径）\n"
        "- offset: 从第几行开始读（1-based，默认 1）\n"
        "- limit: 最多读取多少行（默认 200）"
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
                                "文件路径(相对工作根,如 'clinic/pom.xml';"
                                "也可用绝对路径)"
                            ),
                        },
                        "offset": {
                            "type": "integer",
                            "description": "从第几行开始读取(1-based, 默认1)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多读取多少行(默认200, 大文件可分段读取)",
                        },
                    },
                    "required": ["path"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = args.get("path", "")
        offset = max(1, args.get("offset", 1))
        limit = min(_MAX_LINES, args.get("limit", _MAX_LINES))

        target = safe_resolve(ctx.workspace_root, path)
        if target is None:
            return ToolResult(
                ok=False, output="",
                error=f"路径越界或非法: {path}\n工作根目录: {ctx.workspace_root}",
            )
        if not target.exists():
            return ToolResult(ok=False, output="", error=f"文件不存在: {path}")
        if not target.is_file():
            return ToolResult(ok=False, output="", error=f"不是文件: {path}")

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"读取失败: {e}")

        lines = text.splitlines()
        total_lines = len(lines)

        # 行号范围
        start = offset
        end = min(offset + limit, total_lines + 1)

        # 切片
        selected = lines[start - 1 : end - 1]

        # 格式化: 每行带行号 (格式: "  123: code...")
        max_num_width = len(str(end - 1)) if end > 1 else 1
        formatted_lines = []
        for i, line in enumerate(selected):
            line_no = start + i
            formatted_lines.append(f"{line_no:>{max_num_width}}: {line}")

        result = "\n".join(formatted_lines)

        # 截断超长输出
        if len(result) > _MAX_CHARS:
            result = result[:_MAX_CHARS] + "\n...(已截断,可用更大 offset 继续读取)"

        # 头部信息
        header = f"文件: {path} (共 {total_lines} 行)"
        if start > 1 or end <= total_lines:
            header += f" | 显示第 {start}-{min(end - 1, total_lines)} 行"

        return ToolResult(
            ok=True,
            output=f"{header}\n{result}",
            data={"path": path, "total_lines": total_lines, "shown_lines": f"{start}-{end - 1}"},
        )

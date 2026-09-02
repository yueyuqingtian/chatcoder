"""fs_read 工具 — 读取文件内容(支持行号范围)。

参照 CodeBuddy/Codex 的 read_file 行为:
- 自动显示行号
- 支持 offset/limit 参数限定读取范围
- 大文件自动提示行号范围
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve_read

_MAX_LINES = 400  # v15: 200→400，配合字符上限(tool_output_chars_read=16000)提升单次读取量，减少分页重读


def _max_chars() -> int:
    """fs_read 单次输出字符上限 —— 读取分级配置，避免写死常量与其它层不一致。"""
    from app.core.config import settings
    return settings.tool_output_chars_read


class FsReadTool(Tool):
    name = "fs_read"
    risk_level = "low"
    description = (
        "读取工作区内指定文件的文本内容，自动显示行号。\n"
        "支持通过 offset 和 limit 参数限定读取的行号范围（类似 Codex/CodeBuddy 的行为），"
        "适用于大文件分段阅读。\n"
        "- path: 文件路径（相对工作根，如 'clinic/pom.xml'，也可用绝对路径）\n"
        "- offset: 从第几行开始读（1-based，默认 1）\n"
        "- limit: 最多读取多少行（默认 200，单次最多 400 行）"
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
                            "description": "最多读取多少行(默认200, 单次最多400行, 大文件可分段读取)",
                        },
                    },
                    "required": ["path"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = args.get("path", "")
        # offset/limit 安全解析：模型偶发输出字符串数字或越界值，
        # 直接 max/min 会在 str vs int 比较时抛 TypeError（plan-645）
        try:
            offset = max(1, int(args.get("offset") or 1))
        except (TypeError, ValueError):
            offset = 1
        try:
            # limit=0 是非法值（读 0 行），钳到 1；缺失/None 才回退默认——
            # 不能写 int(args.get("limit") or _MAX_LINES)，0 会被 or 短路成默认
            raw_limit = args.get("limit")
            limit = max(1, min(_MAX_LINES, int(raw_limit) if raw_limit is not None else _MAX_LINES))
        except (TypeError, ValueError):
            limit = _MAX_LINES

        # 附件目录兜底：用户消息附件在工作区外的 uploads 目录，允许只读
        target = safe_resolve_read(ctx.workspace_root, path)
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
        if len(result) > _max_chars():
            result = result[:_max_chars()] + "\n...(已截断,可用更大 offset 继续读取)"

        # 头部信息
        header = f"文件: {path} (共 {total_lines} 行)"
        if start > 1 or end <= total_lines:
            header += f" | 显示第 {start}-{min(end - 1, total_lines)} 行"

        return ToolResult(
            ok=True,
            output=f"{header}\n{result}",
            data={"path": path, "total_lines": total_lines, "shown_lines": f"{start}-{end - 1}"},
        )

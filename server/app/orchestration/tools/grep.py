"""grep 搜索工具 — 在工作区内搜索文件内容。

支持正则表达式和纯文本搜索，可按文件扩展名过滤。
这是 Agent 最核心的代码导航能力之一，没有它 Agent 只能靠 fs.list + fs.read
逐个猜测文件，效率极低。
"""
import logging
import os
import re
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE = 512 * 1024  # 跳过大于 512KB 的文件
_MAX_MATCHES = 50            # 最多返回 50 个匹配
_MAX_OUTPUT = 10000          # 输出上限（字符）
_MAX_FILES_SCAN = 2000       # 最多扫描 2000 个文件
_DEFAULT_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".vscode", "release", "target",
    ".gradle", ".mvn", "coverage", ".next", ".nuxt",
}
_BINARY_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp",
    ".zip", ".tar", ".gz", ".rar", ".7z", ".jar", ".war",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".pptx",
    ".mp3", ".mp4", ".avi", ".mov", ".flv",
    ".ttf", ".woff", ".woff2", ".eot", ".otf",
    ".lib", ".a", ".o", ".class",
    ".lock", ".min.js", ".min.css",
    ".chunk.js", ".map",
}


class GrepTool(Tool):
    name = "fs_grep"
    risk_level = "low"
    description = (
        "在工作区内搜索文件内容（支持正则表达式）。"
        "用于快速定位函数定义、类名、变量引用、配置项、字符串等。"
        "可按文件扩展名过滤搜索范围。强烈建议优先使用此工具定位代码。"
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
                        "pattern": {
                            "type": "string",
                            "description": "搜索模式（正则表达式或纯文本）",
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "搜索的起始目录（相对路径，如 'src' 或 'server/app'）。"
                                "默认搜索整个工作区"
                            ),
                        },
                        "include": {
                            "type": "string",
                            "description": (
                                "只搜索匹配此 glob 模式的文件"
                                "（如 '*.py' 或 '*.java' 或 '*.ts'）。"
                                "默认搜索所有文本文件"
                            ),
                        },
                        "case_sensitive": {
                            "type": "boolean",
                            "description": "是否区分大小写。默认 false",
                        },
                        "context_lines": {
                            "type": "integer",
                            "description": "每个匹配项上下文行数（前后各显示多少行）。默认 0，最大 5",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern_str = args.get("pattern", "").strip()
        if not pattern_str:
            return ToolResult(ok=False, output="", error="pattern 参数不能为空")

        search_path = args.get("path", "")
        include_glob = args.get("include", "")
        case_sensitive = args.get("case_sensitive", False)
        context_lines = min(args.get("context_lines", 0), 5)

        # 解析搜索根目录
        if search_path:
            root = safe_resolve(ctx.workspace_root, search_path)
            if root is None:
                return ToolResult(
                    ok=False, output="",
                    error=f"路径越界或非法: {search_path}",
                )
            if not root.exists():
                return ToolResult(ok=False, output="", error=f"目录不存在: {search_path}")
            if not root.is_dir():
                return ToolResult(ok=False, output="", error=f"不是目录: {search_path}")
        else:
            root = Path(ctx.workspace_root)

        # 编译正则
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern_str, flags)
        except re.error as e:
            return ToolResult(ok=False, output="", error=f"正则表达式错误: {e}")

        # 编译 include glob
        include_re = None
        if include_glob:
            glob_pattern = include_glob.replace(".", r"\.").replace("*", ".*")
            include_re = re.compile(f"^{glob_pattern}$", re.IGNORECASE)

        # 遍历搜索
        results: list[str] = []
        total_matches = 0
        files_searched = 0
        ws_root = Path(ctx.workspace_root)

        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过排除目录
            dirnames[:] = [d for d in dirnames if d not in _DEFAULT_EXCLUDE_DIRS]

            for filename in filenames:
                if files_searched >= _MAX_FILES_SCAN:
                    break
                if total_matches >= _MAX_MATCHES:
                    break

                file_path = Path(dirpath) / filename

                # 扩展名过滤
                if include_re and not include_re.match(filename):
                    continue
                if not include_re and file_path.suffix.lower() in _BINARY_EXTENSIONS:
                    continue

                # 大小过滤
                try:
                    if file_path.stat().st_size > _MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue

                files_searched += 1

                try:
                    text = file_path.read_text(encoding="utf-8", errors="replace")
                    lines = text.splitlines()
                except OSError:
                    continue

                try:
                    rel_path = str(file_path.relative_to(ws_root))
                except ValueError:
                    rel_path = str(file_path)

                for line_no, line in enumerate(lines, 1):
                    if total_matches >= _MAX_MATCHES:
                        break
                    if regex.search(line):
                        total_matches += 1
                        if context_lines > 0:
                            start = max(0, line_no - 1 - context_lines)
                            end = min(len(lines), line_no + context_lines)
                            snippet_lines = []
                            for j in range(start, end):
                                marker = ">>" if j == line_no - 1 else "  "
                                snippet_lines.append(f"  {marker} {j+1}: {lines[j]}")
                            results.append(f"\n{rel_path}:\n" + "\n".join(snippet_lines))
                        else:
                            results.append(f"{rel_path}:{line_no}: {line.strip()[:200]}")

            if files_searched >= _MAX_FILES_SCAN or total_matches >= _MAX_MATCHES:
                break

        if not results:
            output = f"未找到匹配项 (搜索了 {files_searched} 个文件)"
        else:
            header = f"找到 {total_matches} 个匹配 (搜索了 {files_searched} 个文件)"
            if total_matches >= _MAX_MATCHES:
                header += f" (已达上限 {_MAX_MATCHES}，可能还有更多)"
            if files_searched >= _MAX_FILES_SCAN:
                header += f" (已扫描上限 {_MAX_FILES_SCAN} 个文件)"
            body = "\n".join(results)
            output = f"{header}\n\n{body}"

        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + "\n...(已截断)"

        logger.debug(
            "grep pattern=%s path=%s matches=%d files=%d",
            pattern_str, search_path, total_matches, files_searched,
        )

        return ToolResult(
            ok=True,
            output=output,
            data={
                "matches": total_matches,
                "files_searched": files_searched,
                "pattern": pattern_str,
            },
        )

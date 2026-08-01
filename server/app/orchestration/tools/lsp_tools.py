"""v1.0: LSP 集成工具 — 定义跳转、引用查找、诊断信息。

通过 subprocess 与 language server 进程通信（JSON-RPC）。
当前支持 Python (pylsp) 和 TypeScript (typescript-language-server)。
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT = 10


class LspDefinitionTool(Tool):
    name = "lsp_definition"
    risk_level = "low"
    description = "查找符号定义位置（Go to Definition）。返回文件路径和行号。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "文件路径(相对工作根)"},
                        "line": {"type": "integer", "description": "行号(0-based)"},
                        "character": {"type": "integer", "description": "列号(0-based)"},
                    },
                    "required": ["file", "line", "character"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args.get("file", "")
        line = args.get("line", 0)
        character = args.get("character", 0)

        if not file_path:
            return ToolResult(ok=False, output="", error="file 参数为空")

        from app.orchestration.tools.safe_path import safe_resolve
        resolved = safe_resolve(ctx.workspace_root, file_path)
        if resolved is None or not resolved.exists():
            return ToolResult(ok=False, output="", error=f"文件不存在: {file_path}")

        # 使用简单的 grep 回退方案（完整 LSP 需要 language server 进程管理）
        # 读取目标行的符号名
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
            if line >= len(lines):
                return ToolResult(ok=False, output="", error=f"行号超出范围: {line}")
            target_line = lines[line]
            # 提取光标处的单词
            word = _extract_word_at(target_line, character)
            if not word:
                return ToolResult(ok=False, output="", error="无法提取光标处的符号")
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"读取文件失败: {e}")

        # 在工作区中搜索定义（简化版：grep 搜索 def/class/function/const 等）
        import subprocess
        patterns = [
            f"def {word}",
            f"class {word}",
            f"function {word}",
            f"const {word}",
            f"let {word}",
            f"var {word}",
            f"interface {word}",
            f"type {word}",
        ]
        results = []
        for pattern in patterns:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "grep", "-rn", "--include=*.py", "--include=*.ts", "--include=*.tsx",
                    "--include=*.js", "--include=*.jsx",
                    pattern, ctx.workspace_root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
                for match_line in (stdout or b"").decode("utf-8", errors="replace").splitlines()[:5]:
                    results.append(match_line)
            except (asyncio.TimeoutError, OSError):
                continue

        if not results:
            return ToolResult(ok=True, output=f"未找到 '{word}' 的定义", data={"symbol": word})

        output = f"符号 '{word}' 的定义位置:\n" + "\n".join(results[:10])
        return ToolResult(ok=True, output=output, data={"symbol": word, "matches": len(results)})


class LspDiagnosticsTool(Tool):
    name = "lsp_diagnostics"
    risk_level = "low"
    description = "获取文件的诊断信息（语法错误、类型错误等）。当前使用 Python AST 检查。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "文件路径(相对工作根)"},
                    },
                    "required": ["file"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args.get("file", "")
        if not file_path:
            return ToolResult(ok=False, output="", error="file 参数为空")

        from app.orchestration.tools.safe_path import safe_resolve
        resolved = safe_resolve(ctx.workspace_root, file_path)
        if resolved is None or not resolved.exists():
            return ToolResult(ok=False, output="", error=f"文件不存在: {file_path}")

        suffix = resolved.suffix.lower()

        if suffix == ".py":
            # Python: 用 ast 模块检查语法
            try:
                import ast
                source = resolved.read_text(encoding="utf-8")
                ast.parse(source)
                return ToolResult(ok=True, output="无语法错误", data={"file": file_path, "errors": 0})
            except SyntaxError as e:
                return ToolResult(
                    ok=True,
                    output=f"语法错误: 行 {e.lineno}, 列 {e.offset}: {e.msg}",
                    data={"file": file_path, "errors": 1, "line": e.lineno},
                )
        elif suffix in (".ts", ".tsx", ".js", ".jsx"):
            # TypeScript/JS: 尝试用 npx tsc --noEmit
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npx", "tsc", "--noEmit", "--pretty", str(resolved),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=ctx.workspace_root,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                out = (stdout or b"").decode("utf-8", errors="replace")
                if proc.returncode == 0:
                    return ToolResult(ok=True, output="无类型错误", data={"file": file_path, "errors": 0})
                return ToolResult(ok=True, output=out[:4000], data={"file": file_path})
            except (asyncio.TimeoutError, OSError) as e:
                return ToolResult(ok=False, output="", error=f"tsc 检查失败: {e}")
        else:
            return ToolResult(ok=True, output=f"暂不支持 {suffix} 文件的诊断", data={"file": file_path})


def _extract_word_at(line: str, col: int) -> str:
    """提取行中光标位置的单词。"""
    if col >= len(line):
        col = len(line) - 1
    if col < 0:
        return ""
    # 向左扩展
    start = col
    while start > 0 and (line[start - 1].isalnum() or line[start - 1] == "_"):
        start -= 1
    # 向右扩展
    end = col
    while end < len(line) and (line[end].isalnum() or line[end] == "_"):
        end += 1
    return line[start:end]

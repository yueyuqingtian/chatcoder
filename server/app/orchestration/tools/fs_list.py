"""fs.list 工具 — 列出目录内容(支持递归深度)。

参照 CodeBuddy 的 list_dir 行为:
- 支持递归深度控制
- 自动排除常见忽略目录(.git, node_modules 等)
- 支持忽略模式
"""
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve

_MAX_ENTRIES = 300
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".vscode", "release", "target",
    ".gradle", ".mvn", "coverage", ".next", ".nuxt", ".codebuddy",
    "logs", ".cache",
}


class FsListTool(Tool):
    name = "fs_list"
    risk_level = "low"
    description = (
        "列出工作区内指定目录的文件与子目录。\n"
        "- path: 目录路径(默认根目录)\n"
        "- recursive: 是否递归子目录(默认 false, 设为 true 则列出所有子目录的文件)\n"
        "- max_depth: 递归深度(默认 2)"
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
                            "description": "工作区内相对目录,默认根目录(可用绝对路径)",
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "是否递归列出子目录内容(默认 false)",
                        },
                        "max_depth": {
                            "type": "integer",
                            "description": "递归最大深度(默认2, 仅 recursive=true 时有效)",
                        },
                    },
                    "required": [],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = args.get("path", "") or ""
        recursive = args.get("recursive", False)
        max_depth = min(5, args.get("max_depth", 2))

        # v4.8.2: 同步 I/O 移出事件循环，防止 Windows 网络盘/特殊目录挂起
        import asyncio
        try:
            return await asyncio.to_thread(self._list_sync, path, recursive, max_depth, ctx.workspace_root)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"列目录失败: {e}")

    def _list_sync(self, path: str, recursive: bool, max_depth: int, workspace_root: str) -> ToolResult:
        target = safe_resolve(workspace_root, path) if path else Path(workspace_root).resolve()
        if target is None:
            return ToolResult(ok=False, output="", error=f"路径越界或非法: {path}")
        if not target.exists():
            return ToolResult(ok=False, output="", error=f"目录不存在: {path}")
        if not target.is_dir():
            return ToolResult(ok=False, output="", error=f"非目录: {path}")

        try:
            ws_root = Path(workspace_root)
            entries: list[str] = []

            if recursive:
                self._walk_recursive(target, ws_root, entries, depth=0, max_depth=max_depth)
            else:
                for e in sorted(target.iterdir()):
                    if e.name in _IGNORE_DIRS:
                        continue
                    kind = "dir" if e.is_dir() else "file"
                    entries.append(f"[{kind}] {e.name}")

            result = "\n".join(entries[:_MAX_ENTRIES]) or "(空目录)"
            if len(entries) > _MAX_ENTRIES:
                result += f"\n...(仅显示前 {_MAX_ENTRIES} 条,共 {len(entries)} 条)"
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"列目录失败: {e}")

        header = f"目录: {path or '.'} ({len(entries[:_MAX_ENTRIES])} 条)"
        if recursive:
            header += f" [递归, 深度={max_depth}]"

        return ToolResult(
            ok=True,
            output=f"{header}\n{result}",
            data={"path": path, "count": len(entries)},
        )

    def _walk_recursive(
        self, dir_path: Path, ws_root: Path,
        entries: list[str], depth: int, max_depth: int,
    ) -> None:
        """递归遍历目录。"""
        if depth > max_depth or len(entries) >= _MAX_ENTRIES:
            return
        try:
            for e in sorted(dir_path.iterdir()):
                if len(entries) >= _MAX_ENTRIES:
                    return
                if e.name in _IGNORE_DIRS:
                    continue
                # 计算相对路径
                try:
                    rel = e.relative_to(ws_root)
                except ValueError:
                    rel = e
                kind = "dir" if e.is_dir() else "file"
                indent = "  " * depth
                entries.append(f"[{kind}] {indent}{rel}")
                if e.is_dir() and depth < max_depth:
                    self._walk_recursive(e, ws_root, entries, depth + 1, max_depth)
        except (OSError, PermissionError):
            pass

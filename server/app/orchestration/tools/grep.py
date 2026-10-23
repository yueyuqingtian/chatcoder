"""grep 搜索工具 — 在工作区内搜索文件内容。

支持正则表达式和纯文本搜索，可按文件扩展名过滤。
这是 Agent 最核心的代码导航能力之一，没有它 Agent 只能靠 fs.list + fs.read
逐个猜测文件，效率极低。
"""
import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.ripgrep_env import locate_ripgrep
from app.orchestration.output_paging import cap_raw
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE = 512 * 1024  # 跳过大于 512KB 的文件
_MAX_MATCHES = 50            # 最多返回 50 个匹配

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
    concurrency_safe = True  # plan-522-2209：纯读，不写文件、不改库
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
                                "搜索范围：目录（相对路径，如 'src' 或 'server/app'）或单个文件。"
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

        # 解析搜索根目录（支持目录或单个文件；v1.2: path 指向文件时只搜该文件）
        ws_root = Path(ctx.workspace_root)
        if search_path:
            root = safe_resolve(ctx.workspace_root, search_path)
            if root is None:
                # plan-75-332: 工作区外搜索根直接放行（是否询问由 approval_policy 裁决）
                from app.orchestration.tools import outside_access

                _outside, _err = outside_access.resolve_outside_target(ctx, search_path)
                if _outside is None:
                    return ToolResult(ok=False, output="", error=_err)
                root = _outside
            if not root.exists():
                # plan-20261020-pts：附上同名文件 / 目录的实际位置与最近存在的上级目录
                from app.orchestration.tools.path_hints import missing_path_hint

                hint = await asyncio.to_thread(missing_path_hint, ctx.workspace_root, str(search_path))
                return ToolResult(ok=False, output="", error=f"路径不存在: {search_path}{hint}", error_code="NOT_FOUND")
        else:
            root = ws_root

        # 编译正则
        # plan-20261020-pts：正则不合法（最常见是 `foo(` 这类未转义的括号）时按普通文本搜索并说明，
        # 不再直接失败——模型要找的几乎总是这段字面文本。
        flags = 0 if case_sensitive else re.IGNORECASE
        literal_note = ""
        try:
            regex = re.compile(pattern_str, flags)
        except re.error as e:
            regex = re.compile(re.escape(pattern_str), flags)
            literal_note = (
                f"（「{pattern_str}」不是合法正则（{e}），已按普通文本搜索；"
                "要用正则请把括号等特殊字符转义）\n"
            )

        # 编译 include glob
        include_re = None
        if include_glob:
            glob_pattern = include_glob.replace(".", r"\.").replace("*", ".*")
            include_re = re.compile(f"^{glob_pattern}$", re.IGNORECASE)

        # v1.1: 增强搜索 — enhanced_search 开启且能定位到 ripgrep（内置或 PATH）时走 rg 快路径；
        # rg 不可用/超时/失败返回 None，回退下方纯 Python 逐行匹配。
        # v1.2: rg 快路径仅对目录生效（rg 的 cwd 必须是目录），单文件走纯 Python 匹配。
        rg = locate_ripgrep() if (root.is_dir() and settings.enhanced_search) else None
        if rg is not None:
            rg_result = await self._rg_search(
                root=root, ws_root=ws_root, pattern=pattern_str,
                include_glob=include_glob, case_sensitive=case_sensitive,
                context_lines=context_lines, fixed=bool(literal_note), rg_path=rg.path,
            )
            if rg_result is not None:
                if literal_note:
                    rg_result.output = literal_note + rg_result.output
                return rg_result
            logger.debug("rg 不可用或执行失败，回退纯 Python 逐行匹配")

        # 遍历搜索
        results: list[str] = []
        total_matches = 0
        files_searched = 0

        def iter_files():
            """产出待搜索文件：单文件模式产出该文件，目录模式递归产出文本文件。"""
            if root.is_file():
                yield root
                return
            for dirpath, dirnames, filenames in os.walk(root):
                # 跳过排除目录
                dirnames[:] = [d for d in dirnames if d not in _DEFAULT_EXCLUDE_DIRS]
                for filename in filenames:
                    yield Path(dirpath) / filename

        for file_path in iter_files():
            if files_searched >= _MAX_FILES_SCAN:
                break
            if total_matches >= _MAX_MATCHES:
                break

            # 扩展名过滤
            if include_re and not include_re.match(file_path.name):
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

        # plan-156-620：不再按 _MAX_OUTPUT 硬截断（截掉的匹配无法取回）；超出上下文预算由执行层落盘续读
        output = cap_raw(literal_note + output)

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

    async def _rg_search(
        self,
        root: Path,
        ws_root: Path,
        pattern: str,
        include_glob: str,
        case_sensitive: bool,
        context_lines: int,
        fixed: bool = False,
        rg_path: str = "rg",
    ) -> ToolResult | None:
        """v1.1: ripgrep 快路径。任何异常/不可用返回 None，由调用方回退纯 Python。

        fixed=True：按字面文本搜索（-F），用于模型给的正则不合法时的降级。
        注意：--max-filesize 只接受纯字节数或 K/M/G 后缀，不能带 B（否则 rg 报错，快路径形同虚设）。"""
        cmd = [
            rg_path, "--no-heading", "-n", "--color", "never",
            # -0：路径后接 NUL 而不是 ":"，匹配行为 "路径\0行号:内容"、上下文行为 "路径\0行号-内容"，
            # 解析时不会被路径或内容里的 ":数字:" 误导
            "-0",
            "--max-filesize", str(_MAX_FILE_SIZE),
            "--max-count", str(_MAX_MATCHES),
        ]
        if fixed:
            cmd.append("-F")
        if not case_sensitive:
            cmd.append("-i")
        if context_lines > 0:
            cmd.append(f"-C{context_lines}")
        if include_glob:
            cmd += ["-g", include_glob]
        for d in _DEFAULT_EXCLUDE_DIRS:
            cmd += ["-g", f"!{d}/**"]
        for ext in _BINARY_EXTENSIONS:
            cmd += ["-g", f"!*{ext}"]
        cmd += ["-e", pattern, "."]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out_b, _err_b = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except (OSError, asyncio.TimeoutError):
            return None
        if proc.returncode not in (0, 1):
            return None  # rc=2: 参数/正则错误 → 回退 Python 分支以复用错误提示

        # rg 输出路径相对 cwd=root；root 在 ws_root 下时补相对前缀
        rel_prefix = ""
        if root != ws_root:
            try:
                rel_prefix = str(root.relative_to(ws_root)).replace("\\", "/") + "/"
            except ValueError:
                rel_prefix = ""

        text = out_b.decode("utf-8", errors="replace")
        lines: list[str] = []
        total_matches = 0
        file_set: set[str] = set()
        for line in text.splitlines():
            if total_matches >= _MAX_MATCHES:
                break
            if not line or line == "--":
                continue
            path_part, nul, rest = line.partition("\0")
            if not nul:
                continue
            # rg 输出的路径形如 ".\\src\\a.py"（cwd=root 时带 ./ 前缀），统一成与纯 Python 分支一致的相对路径
            path = path_part[2:] if path_part[:2] in ("./", ".\\") else path_part
            m = re.match(r"^\d+([:-])", rest)
            if not m:
                continue
            is_match = m.group(1) == ":"
            if is_match:
                total_matches += 1
                file_set.add(path)
            elif context_lines <= 0:
                continue
            lines.append(f"{rel_prefix}{path}{m.group(1)}{rest}")

        if not lines:
            output = f"未找到匹配项 (rg)"
        else:
            header = f"找到 {total_matches} 个匹配 (rg)"
            if total_matches >= _MAX_MATCHES:
                header += f" (已达上限 {_MAX_MATCHES}，可能还有更多)"
            output = header + "\n\n" + "\n".join(lines)

        # plan-156-620：同上，超出上下文预算由执行层落盘续读
        output = cap_raw(output)

        return ToolResult(
            ok=True,
            output=output,
            data={
                "matches": total_matches,
                "files_searched": len(file_set),
                "pattern": pattern,
            },
        )

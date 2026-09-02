"""v1.0: Git 完整集成工具 — commit/branch/checkout/stash/log/blame。

扩展原有 git_diff，提供完整的 Git 操作能力。
写操作（commit/branch/checkout/stash）标记 risk_level="medium"，需审批。
"""
import asyncio
import logging
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# v1.0 (plan-153-705): 15s → 60s——大仓库 git status/log/blame 会超 15s
_TIMEOUT = 60
_MAX_OUTPUT = 6000


async def _run_git(args: list[str], cwd: str) -> tuple[int, str]:
    """执行 git 命令，返回 (returncode, output)。"""
    cmd = ["git"] + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass
        return -1, f"git 命令超时(>{_TIMEOUT}s): {' '.join(cmd)}"
    except OSError as e:
        return -1, f"执行失败: {e}"

    out = (stdout or b"").decode("utf-8", errors="replace")
    err = (stderr or b"").decode("utf-8", errors="replace")
    combined = out + (("\n-- stderr --\n" + err) if err else "")
    if len(combined) > _MAX_OUTPUT:
        combined = combined[:_MAX_OUTPUT] + "\n...(截断)"
    return proc.returncode or 0, combined


class GitTool(Tool):
    """v1.0: 完整 Git 工具（替代原 git_diff）。"""

    name = "git"
    risk_level = "medium"  # 写操作需审批
    description = (
        "Git 版本控制操作。支持子命令:\n"
        "- diff: 查看变更 (stat_only=true 仅统计)\n"
        "- commit: 提交变更 (message 必填, files 可选)\n"
        "- branch: 创建/列出分支 (name 可选)\n"
        "- checkout: 切换分支/恢复文件 (ref 必填)\n"
        "- stash: 暂存操作 (action: push/pop/list)\n"
        "- log: 查看提交历史 (n 条数, 默认 10)\n"
        "- blame: 查看文件逐行归属 (file 必填)\n"
        "- status: 查看工作区状态"
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
                        "command": {
                            "type": "string",
                            "enum": ["diff", "commit", "branch", "checkout", "stash", "log", "blame", "status"],
                            "description": "Git 子命令",
                        },
                        "cwd": {"type": "string", "description": "工作目录(相对工作根)"},
                        "message": {"type": "string", "description": "commit 消息"},
                        "files": {"type": "array", "items": {"type": "string"}, "description": "commit 指定文件"},
                        "name": {"type": "string", "description": "branch 名称"},
                        "ref": {"type": "string", "description": "checkout 目标"},
                        "action": {"type": "string", "enum": ["push", "pop", "list"], "description": "stash 操作"},
                        "n": {"type": "integer", "description": "log 条数"},
                        "file": {"type": "string", "description": "blame 文件路径"},
                        "stat_only": {"type": "boolean", "description": "diff 仅统计"},
                    },
                    "required": ["command"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command", "")
        cwd = args.get("cwd", "")

        # 解析工作目录
        from app.orchestration.tools.safe_path import safe_resolve
        if cwd:
            resolved = safe_resolve(ctx.workspace_root, cwd)
            work_dir = str(resolved) if resolved else ctx.workspace_root
        else:
            work_dir = ctx.workspace_root

        if command == "diff":
            git_args = ["diff"]
            if args.get("stat_only"):
                git_args.append("--stat")
            rc, out = await _run_git(git_args, work_dir)

        elif command == "commit":
            msg = args.get("message", "")
            if not msg:
                return ToolResult(ok=False, output="", error="commit 需要 message 参数")
            files = args.get("files") or []
            if files:
                await _run_git(["add"] + files, work_dir)
            else:
                await _run_git(["add", "-A"], work_dir)
            rc, out = await _run_git(["commit", "-m", msg], work_dir)

        elif command == "branch":
            name = args.get("name")
            if name:
                rc, out = await _run_git(["branch", name], work_dir)
            else:
                rc, out = await _run_git(["branch", "-a"], work_dir)

        elif command == "checkout":
            ref = args.get("ref", "")
            if not ref:
                return ToolResult(ok=False, output="", error="checkout 需要 ref 参数")
            rc, out = await _run_git(["checkout", ref], work_dir)

        elif command == "stash":
            action = args.get("action", "push")
            rc, out = await _run_git(["stash", action], work_dir)

        elif command == "log":
            n = args.get("n", 10)
            rc, out = await _run_git(
                ["log", f"--oneline", f"-{n}", "--decorate"], work_dir
            )

        elif command == "blame":
            file_path = args.get("file", "")
            if not file_path:
                return ToolResult(ok=False, output="", error="blame 需要 file 参数")
            rc, out = await _run_git(["blame", file_path], work_dir)

        elif command == "status":
            rc, out = await _run_git(["status", "--short"], work_dir)

        else:
            return ToolResult(ok=False, output="", error=f"不支持的 git 子命令: {command}")

        return ToolResult(
            ok=rc == 0,
            output=out or "(无输出)",
            data={"command": command, "returncode": rc, "cwd": work_dir},
            error="" if rc == 0 else f"退出码 {rc}",
        )

"""v0.9: git.diff 工具 — 查看工作目录未提交的代码变更。

低风险(只读)。让 reviewer/审查任务准确拿到变更范围,避免误审无关文件。

v2.5: 支持 repo 参数指定多仓库场景下的具体仓库。
"""
import asyncio
import logging
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.git_root import resolve_repo_for_cwd, list_git_repos
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_OUTPUT = 6000


class GitDiffTool(Tool):
    name = "git_diff"
    risk_level = "low"
    description = (
        "查看 git 仓库中未提交的代码变更。\n"
        "默认只返回变更文件统计(文件名+增删行数),不返回完整 diff,节省上下文。\n"
        "需要看具体变更内容时设置 stat_only=false。\n"
        "- repo: git 仓库目录名(多仓库场景必须指定,如 'clinic')\n"
        "- stat_only: true=仅统计(默认); false=返回完整 diff\n"
        "- tracked_only: True=仅跟踪文件; False=包含未跟踪新文件"
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
                        "repo": {
                            "type": "string",
                            "description": (
                                "git 仓库目录名(相对工作根,如 'clinic' 或 'clinicFrontEnd')。"
                                "多仓库场景必须指定!"
                            ),
                        },
                        "tracked_only": {
                            "type": "boolean",
                            "description": "True=仅已跟踪文件的变更(默认);False=包含未跟踪新文件",
                        },
                        "stat_only": {
                            "type": "boolean",
                            "description": "True=只返回文件统计(文件名+增删行数),不返回完整 diff",
                        },
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        repo_hint = args.get("repo", None)
        # v2.0: 默认 stat_only=True(先看概况再决定是否查看完整 diff)
        tracked_only = args.get("tracked_only", True)
        stat_only = args.get("stat_only", True)

        # v2.5: 根据 repo 参数匹配仓库
        repo_root = resolve_repo_for_cwd(ctx.workspace_root, cwd_hint=repo_hint)

        # 如果回退到了 workspace 根(多仓库未指定),提示 agent
        if repo_root == ctx.workspace_root:
            repos = list_git_repos(ctx.workspace_root)
            if repos:
                repo_names = [Path(r).name for r in repos]
                return ToolResult(
                    ok=False, output="",
                    error=(
                        f"工作根目录不是 git 仓库。检测到子目录仓库: {', '.join(repo_names)}。\n"
                        f"请添加 repo 参数,如: repo='{repo_names[0]}'"
                    ),
                    data={"hint": "specify_repo", "repos": repo_names},
                )

        # 构造 git 命令
        if stat_only:
            cmd = "git diff --stat HEAD"
            if not tracked_only:
                cmd += " && git status --short"
        else:
            cmd = "git diff HEAD"
            if not tracked_only:
                cmd = "git status --short && echo '=== DIFF ===' && git diff HEAD"

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_root,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            return ToolResult(ok=False, output="", error="git diff 超时(>30s)")
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"执行失败: {e}")

        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")

        if "not a git repository" in err.lower() or "fatal: not a git" in err.lower():
            return ToolResult(
                ok=False, output="",
                error=f"目录不是 git 仓库(已查找: {repo_root}),无法获取变更",
                data={"repo_root": repo_root},
            )

        if proc.returncode != 0 and not out:
            return ToolResult(
                ok=False, output="", error=f"git 错误: {err}",
                data={"repo_root": repo_root},
            )

        changed_files: list[str] = []
        for line in out.splitlines():
            if "|" in line and not line.startswith(" "):
                parts = line.split("|", 1)
                fname = parts[0].strip()
                if fname and not fname.endswith(("files changed", "file changed")):
                    changed_files.append(fname)

        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT] + f"\n...(已截断,共 {len(out)} 字符)"

        logger.debug("git.diff repo=%s files=%s", repo_root, changed_files)
        return ToolResult(
            ok=True,
            output=out or "(无未提交变更,工作区干净)",
            data={
                "changed_files": changed_files,
                "returncode": proc.returncode,
                "repo_root": repo_root,
            },
        )

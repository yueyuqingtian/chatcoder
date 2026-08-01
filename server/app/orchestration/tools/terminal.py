"""v0.3: terminal.exec 工具(high risk,走审批)。不做命令白名单(仅审批门决策)。

v2.5: 修复多 git 仓库 cwd 探测冲突 + cd && 链重复执行问题。
v1.0: 超时后 kill 子进程 + cwd 路径穿越防护。
"""
import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.git_root import resolve_repo_for_cwd, list_git_repos

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 30
_MAX_OUTPUT = 8000


class TerminalExecTool(Tool):
    name = "terminal_exec"
    risk_level = "high"
    description = (
        "在工作区目录内执行 shell 命令(超时 30s)。需用户审批。不做命令白名单。\n"
        "重要: 每次调用是全新的 shell 进程,cd 不会跨调用持久化。\n"
        "如需在特定子目录执行,请用 cwd 参数指定,而不是 cd 命令。\n"
        '例: {"command": "git diff", "cwd": "clinic"}'
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
                        "command": {"type": "string", "description": "shell 命令(不含 cd 前缀)"},
                        "cwd": {
                            "type": "string",
                            "description": (
                                "执行命令的工作目录,相对工作根的路径(如 'clinic' 或 'clinicFrontEnd')。"
                                "多 git 仓库时必须指定此参数!"
                            ),
                        },
                    },
                    "required": ["command"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command", "").strip()
        if not command:
            return ToolResult(ok=False, output="", error="command 为空")

        # 1. 检测裸 cd 命令 → 给提示但不执行(避免无效调用)
        bare_cd = re.match(r"^(?:cd|chdir)\s+(.+)$", command, re.IGNORECASE)
        if bare_cd and "&&" not in command and "&" not in command and "|" not in command:
            return ToolResult(
                ok=True,
                output=(
                    f"提示: 'cd {bare_cd.group(1).strip()}' 不会持久化(每次调用是新 shell)。\n"
                    f"请改用 cwd 参数: cwd='{bare_cd.group(1).strip()}', command='你的命令'"
                ),
                data={"hint": "use_cwd_param"},
            )

        # 2. 如果命令含 'cd xxx && yyy',提取 xxx 作为 cwd,并从命令中移除 cd 部分
        #    (否则 subprocess 的 cwd=xxx 已切到该目录,cd xxx 再执行一次会失败)
        cd_chain_match = re.search(
            r"(?:^|\s)(?:cd|chdir)\s+([^\s&;|]+)\s*(?:&&|;|&)",
            command, re.IGNORECASE,
        )
        explicit_cwd = args.get("cwd", None)
        resolved_cwd: str | None = None

        if explicit_cwd:
            resolved_cwd = self._resolve_path(ctx.workspace_root, explicit_cwd)
        elif cd_chain_match:
            cd_dir = cd_chain_match.group(1).strip().strip('"').strip("'")
            resolved_cwd = self._resolve_path(ctx.workspace_root, cd_dir)
            # 从命令中移除 'cd xxx && ' 部分
            command = re.sub(
                r"(?:cd|chdir)\s+[^\s&;|]+\s*(?:&&|;|&)\s*",
                "", command, flags=re.IGNORECASE,
            ).strip()

        # 3. 若仍未确定 cwd 且命令含 git,尝试自动探测
        if not resolved_cwd:
            if "git" in command.lower():
                resolved_cwd = resolve_repo_for_cwd(ctx.workspace_root)
            else:
                resolved_cwd = ctx.workspace_root

        # 4. 如果 cwd 是 workspace 根(非 git 仓库)但命令是 git → 报错提示 agent 用 cwd 参数
        if "git" in command.lower() and resolved_cwd == ctx.workspace_root:
            repos = list_git_repos(ctx.workspace_root)
            if repos:
                repo_names = [Path(r).name for r in repos]
                return ToolResult(
                    ok=False, output="",
                    error=(
                        f"工作根目录不是 git 仓库。检测到子目录仓库: {', '.join(repo_names)}。\n"
                        f"请添加 cwd 参数指定仓库目录,如: cwd='{repo_names[0]}', command='{command}'"
                    ),
                    data={"hint": "specify_cwd", "repos": repo_names},
                )

        # 5. 执行
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=resolved_cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            # v1.0: 超时后强制 kill 子进程，避免资源耗尽/后台恶意进程
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
            logger.warning("terminal.exec 超时已 kill: cmd=%r cwd=%s", command, resolved_cwd)
            return ToolResult(ok=False, output="", error=f"超时(>{_TIMEOUT_SEC}s)，进程已强制终止")
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"执行失败: {e}")

        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        combined = out
        if err:
            combined += ("\n-- stderr --\n" + err) if combined else err
        if len(combined) > _MAX_OUTPUT:
            combined = combined[:_MAX_OUTPUT] + "\n...(已截断)"

        logger.debug("terminal.exec cmd=%r cwd=%s rc=%s", command, resolved_cwd, proc.returncode)
        return ToolResult(
            ok=proc.returncode == 0,
            output=combined or "(无输出)",
            data={"returncode": proc.returncode, "cwd": resolved_cwd, "cmd": command},
            error="" if proc.returncode == 0 else f"退出码 {proc.returncode}",
        )

    @staticmethod
    def _resolve_path(workspace_root: str, rel: str) -> str:
        """v1.0: 把相对路径解析为绝对路径，增加路径穿越防护。"""
        from app.orchestration.tools.safe_path import safe_resolve
        # 优先用 safe_resolve 校验路径安全性
        resolved = safe_resolve(workspace_root, rel)
        if resolved is not None:
            return str(resolved)
        # safe_resolve 返回 None 表示越界，回退到 workspace_root
        p = Path(rel)
        if p.is_absolute():
            # 绝对路径但不在 workspace 内 → 拒绝，回退 workspace
            logger.warning("terminal cwd 绝对路径越界: %s，回退 workspace", rel)
            return workspace_root
        # 相对路径但解析失败（不存在等）→ 回退 workspace
        return workspace_root

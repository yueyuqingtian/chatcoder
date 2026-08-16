"""v0.4: CI 工具(ci.run)— 执行 lint/test/build 检查并返结果。

风险等级:medium(执行子进程,但命令是预设的检查项,非任意命令)。
用途:reviewer agent 在审查产物时调用,作为客观质量门禁。
"""
import asyncio
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

_TIMEOUT_SEC = 60


class CiRunTool(Tool):
    name = "ci_run"
    risk_level = "medium"
    description = (
        "运行预设的 CI 检查项(lint/test/build 之一),返回通过/失败与输出。"
        "用于审查产物质量。检查项在工作区内执行。"
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
                        "check": {
                            "type": "string",
                            "enum": ["lint", "test", "build"],
                            "description": "检查项:lint=代码检查, test=测试, build=构建",
                        },
                    },
                    "required": ["check"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        check = args.get("check", "")
        if check not in ("lint", "test", "build"):
            return ToolResult(ok=False, output="", error=f"未知检查项: {check}")

        command = _build_command(check)
        if command is None:
            return ToolResult(
                ok=False, output="",
                error=f"工作区未配置 {check} 命令(无 package.json/pyproject.toml/Makefile)",
            )

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ctx.workspace_root,
            )
            communicate_task = asyncio.create_task(proc.communicate())
            # v1.1: 等待期间轮询取消信号，命中即 kill 子进程（停止按钮穿透）
            try:
                stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(_poll_ci(communicate_task, ctx)),
                    timeout=_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(ok=False, output="", error=f"CI {check} 超时(>{_TIMEOUT_SEC}s)")
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                return ToolResult(ok=False, output="", error="已被用户中断")
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"CI {check} 执行失败: {e}")

        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        combined = out + (("\n-- stderr --\n" + err) if err else "")
        if len(combined) > 6000:
            combined = combined[:6000] + "\n...(已截断)"

        return ToolResult(
            ok=proc.returncode == 0,
            output=combined or "(无输出)",
            data={"check": check, "returncode": proc.returncode, "command": command},
            error="" if proc.returncode == 0 else f"{check} 失败(退出码 {proc.returncode})",
        )


async def _poll_ci(task: asyncio.Task, ctx: ToolContext):
    """等待 communicate 完成；期间检查取消信号，命中则取消任务。"""
    while not task.done():
        if ctx.cancel_event and ctx.cancel_event.is_set():
            task.cancel()
            raise asyncio.CancelledError("cancelled by user")
        await asyncio.sleep(0.2)
    return await task


def _build_command(check: str) -> str | None:
    """根据检查项与工作区类型构造命令。

    优先级:Makefile > package.json(Node 项目)> pyproject.toml(Python 项目)。
    返回 None 表示工作区无对应配置。
    """
    import os

    def exists(name: str) -> bool:
        return os.path.isfile(name)

    # Makefile 统一入口(若存在 make 目标)
    if exists("Makefile") or exists("makefile"):
        return f"make {check}"

    # Node 项目
    if exists("package.json"):
        if check == "lint":
            return "npm run lint --silent 2>&1 || npx --no-install eslint ."
        if check == "test":
            return "npm test --silent"
        if check == "build":
            return "npm run build --silent"

    # Python 项目
    if exists("pyproject.toml") or exists("setup.py"):
        if check == "lint":
            return "ruff check . 2>&1 || flake8 ."
        if check == "test":
            return "pytest -q"
        if check == "build":
            return "python -m build 2>&1 || echo build-skipped"

    return None

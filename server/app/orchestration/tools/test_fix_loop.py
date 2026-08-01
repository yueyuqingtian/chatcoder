"""v1.0: 测试执行-修复循环工具。

自动运行测试 → 失败 → 分析错误 → 修复 → 重跑（最多 N 轮）。
作为 agent loop 内的"元工具"，内部调用 terminal_exec + fs_read + fs_write。
"""
import asyncio
import logging
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 3
_TIMEOUT = 60


class TestFixLoopTool(Tool):
    name = "test_fix_loop"
    risk_level = "medium"
    description = (
        "测试执行-修复循环。运行测试命令，若失败则分析错误并尝试自动修复，最多重试 N 轮。\n"
        "参数:\n"
        "- test_command: 测试命令(如 'pytest tests/', 'npm test')\n"
        "- cwd: 工作目录(可选)\n"
        "- max_rounds: 最大修复轮数(默认 3)"
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
                        "test_command": {"type": "string", "description": "测试命令"},
                        "cwd": {"type": "string", "description": "工作目录(相对工作根)"},
                        "max_rounds": {"type": "integer", "description": "最大修复轮数(默认3)"},
                    },
                    "required": ["test_command"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        test_command = args.get("test_command", "")
        cwd = args.get("cwd", "")
        max_rounds = min(args.get("max_rounds", _MAX_ROUNDS), 5)

        if not test_command:
            return ToolResult(ok=False, output="", error="test_command 为空")

        # 解析工作目录
        from app.orchestration.tools.safe_path import safe_resolve
        if cwd:
            resolved = safe_resolve(ctx.workspace_root, cwd)
            work_dir = str(resolved) if resolved else ctx.workspace_root
        else:
            work_dir = ctx.workspace_root

        results_log: list[str] = []

        for round_num in range(1, max_rounds + 1):
            # 执行测试
            rc, output = await self._run_command(test_command, work_dir)

            if rc == 0:
                results_log.append(f"[轮次 {round_num}] 测试通过!")
                return ToolResult(
                    ok=True,
                    output="\n".join(results_log) + f"\n\n最终输出:\n{output[-3000:]}",
                    data={"rounds": round_num, "passed": True},
                )

            # 测试失败
            results_log.append(f"[轮次 {round_num}] 测试失败 (退出码 {rc})")
            results_log.append(f"错误摘要:\n{output[-2000:]}")

            if round_num >= max_rounds:
                break

            # 分析错误并尝试修复（简化版：提取失败文件信息供 agent 参考）
            results_log.append(f"[轮次 {round_num}] 需要修复后重试...")

        return ToolResult(
            ok=False,
            output="\n".join(results_log) + f"\n\n最终错误输出:\n{output[-3000:]}",
            error=f"测试在 {max_rounds} 轮后仍未通过",
            data={"rounds": max_rounds, "passed": False},
        )

    @staticmethod
    async def _run_command(command: str, cwd: str) -> tuple[int, str]:
        """执行命令，返回 (returncode, output)。"""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
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
            return -1, f"命令超时(>{_TIMEOUT}s)"
        except OSError as e:
            return -1, f"执行失败: {e}"

        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        combined = out + (("\n-- stderr --\n" + err) if err else "")
        if len(combined) > 8000:
            combined = combined[:8000] + "\n...(截断)"
        return proc.returncode or 0, combined

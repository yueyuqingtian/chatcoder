"""确定性检查命令执行（设计 §4.5 / §5A.1 第 8 点）。

只运行白名单命令（graph.command_allowed）；输出截断保存，供评估角色与时间线查看。
"""

from __future__ import annotations

import asyncio

from app.orchestration.workflow.graph import command_allowed

OUTPUT_TAIL_CHARS = 2000


async def run_check_command(
    command: str, cwd: str, timeout_sec: float = 600.0
) -> dict[str, object]:
    if not command_allowed(command):
        return {
            "cmd": command,
            "ok": False,
            "exit_code": None,
            "output": "命令不在白名单内，未执行",
        }
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "cmd": command,
            "ok": False,
            "exit_code": None,
            "output": f"超时（{int(timeout_sec)} 秒）",
        }
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        raise
    text = (out or b"").decode("utf-8", "replace")
    return {
        "cmd": command,
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": text[-OUTPUT_TAIL_CHARS:],
    }

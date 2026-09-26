"""v0.3: terminal.exec 工具(high risk,走审批)。不做命令白名单(仅审批门决策)。

v2.5: 修复多 git 仓库 cwd 探测冲突 + cd && 链重复执行问题。
v1.0: 超时后 kill 子进程 + cwd 路径穿越防护。
v1.2: 按 terminal_shell 设置解析执行 shell（PowerShell/Git Bash/cmd），
      与交互终端保持一致；输出解码兼容 GBK（中文 Windows 下 cmd/PowerShell 输出 GBK）。
v1.0 (plan-153-705): waitForCompletion=false 后台执行（注册到 bg_process，
      立即返回 shell_id，用 terminal_bg_status/terminal_bg_kill 管理生命周期）
      + timeout 参数（默认 120s，替代 30s 硬编码，上限钳制 tool_exec_timeout_sec）
      + 输出上限改读 settings.tool_output_chars_terminal。
"""
import asyncio
import logging
import os
import time
import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.bg_process import (
    bg_process_registry, decode_output, kill_process_tree,
)
from app.orchestration.tools.git_root import resolve_repo_for_cwd, list_git_repos
from app.orchestration.tools.shell_env import resolve_shell, shell_kind

logger = logging.getLogger(__name__)

# v1.0 (plan-153-705): 同步等待默认超时（秒），可被 timeout 参数覆盖；
# 上限钳制到 settings.tool_exec_timeout_sec（executor/agent_loop 外层同源）。
_DEFAULT_TIMEOUT_SEC = 120
# 兼容历史诊断测试/插件引用；实际解析统一走 _parse_timeout。
_TIMEOUT_SEC = _DEFAULT_TIMEOUT_SEC
_MIN_TIMEOUT_SEC = 5


def _decode_output(data: bytes) -> str:
    """兼容旧引用：解码逻辑已提取到 bg_process.decode_output（后台收集共用）。"""
    return decode_output(data)


def _parse_wait_for_completion(raw: Any) -> bool:
    """解析 waitForCompletion 参数；容错模型输出的字符串 "false"/"true"。"""
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "0", "no")
    return bool(raw) if raw is not None else True


def _parse_timeout(raw: Any) -> int:
    """解析 timeout 参数（秒），钳制 [5, settings.tool_exec_timeout_sec]。"""
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = _TIMEOUT_SEC
    upper = int(settings.tool_exec_timeout_sec)
    return max(_MIN_TIMEOUT_SEC, min(val, upper))


async def _await_returncode(proc: Any, timeout: float = 5.0) -> int | None:
    """兜底回收进程退出码（结果组装前调用）。

    竞态现场（2026-09-15 全量回归偶发 1 例）：两个输出泵都已读到 EOF、收敛循环
    退出时，asyncio 的子进程 watcher 可能还没来得及回填 returncode，于是
    `returncode == 0` 判等失败——命令输出明明正常（echo 有输出）却被判为
    「退出码 None」失败。这里在组装结果前显式等待子进程被回收；
    输出已 EOF 但进程异常残留时最多等 timeout，不做无限等待。
    """
    if getattr(proc, "returncode", None) is not None:
        return proc.returncode
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "[term] 退出码回收超时(输出已 EOF 但进程未回收) pid=%s",
            getattr(proc, "pid", None),
        )
    except Exception:
        logger.debug("[term] 退出码回收失败(非阻塞)", exc_info=True)
    return getattr(proc, "returncode", None)


class TerminalExecTool(Tool):
    name = "terminal_exec"
    risk_level = "high"
    description = (
        "在工作区目录内执行 shell 命令(默认超时 120s,可用 timeout 参数延长)。\n"
        "当前 shell 见系统提示的「Shell 环境」——Windows 上通常是 PowerShell 或 cmd.exe，不是 bash，\n"
        "请按该 shell 的语法写命令（如 Get-ChildItem 仅 PowerShell 可用，grep/find 通常不存在）。\n"
        "只读安全命令(如 git status/findstr/dir)免审批直接执行；危险命令被安全策略直接拒绝。\n"
        "重要: 每次调用是全新的 shell 进程,cd 不会跨调用持久化。\n"
        "如需在特定子目录执行,请用 cwd 参数指定,而不是 cd 命令。\n"
        "启动 dev server、watch、后端服务等长驻进程时,将 waitForCompletion 设为 false,\n"
        "命令进入后台运行并立即返回 shell_id,用 terminal_bg_status 查日志、terminal_bg_kill 终止。\n"
        "重要: 等待后台命令完成时,禁止用 Start-Sleep / sleep 固定等待(无法感知真正完成,\n"
        "且会让界面长时间无输出)。正确做法: 用 terminal_bg_status 轮询直到 running=false,\n"
        "或直接传 wait_until_done=true 一次等待完成(后台进程结束即返回,非固定秒数)。\n"
        '例: {"command": "git diff", "cwd": "clinic"}；'
        '后台例: {"command": "npm run dev", "waitForCompletion": false}'
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
                        "waitForCompletion": {
                            "type": "boolean",
                            "description": (
                                "是否等待命令完成。默认 true；启动 dev server、watch、后端服务等"
                                "长驻进程时必须设为 false，命令会进入后台运行并返回 shell_id。"
                            ),
                        },
                        "timeout": {
                            "type": "integer",
                            "description": (
                                "同步等待超时(秒),默认 120,范围 5~"
                                f"{settings.tool_exec_timeout_sec}。长命令(安装/构建/测试)按需调大。"
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

        # plan-75-332: 工具不再拦截危险命令——分类与风险标注由 shell_policy 给出，
        # 最终是否询问/放行由 approval_policy 按权限模式裁决（完全访问下可直接执行）。

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
        # plan-75-332: 取消了 cwd 越界限制——工具不再阻止在工作区外目录执行命令
        # （是否询问用户由 approval_policy 按权限模式裁决）。
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

        # 5. 执行（按当前 shell 解析：PowerShell/Git Bash 显式传 -Command/-lc 参数，
        #    cmd 及其他走 create_subprocess_shell 默认解析）
        shell = resolve_shell()
        kind = shell_kind()
        wait_for_completion = _parse_wait_for_completion(args.get("waitForCompletion"))
        timeout_sec = _parse_timeout(args.get("timeout"))
        _t0 = time.monotonic()
        # v36: 执行前留痕。打包后 shell 解析依赖 PATH/SystemRoot，
        # 环境缺失会静默退化成错误 shell，导致命令全部失败且难以查证。
        logger.info(
            "[term.start] cmd=%r cwd=%s shell=%s kind=%s workspace=%s bg=%s timeout=%s",
            command[:300], resolved_cwd, shell, kind, ctx.workspace_root,
            not wait_for_completion, timeout_sec,
        )
        try:
            if kind in ("pwsh", "powershell"):
                proc = await asyncio.create_subprocess_exec(
                    shell, "-NoProfile", "-NonInteractive", "-Command", command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=resolved_cwd,
                )
            elif kind == "git-bash":
                proc = await asyncio.create_subprocess_exec(
                    shell, "-lc", command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=resolved_cwd,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=resolved_cwd,
                )
        except OSError as e:
            # v36: 启动失败多因 shell 路径不存在或打包环境缺依赖，
            # 记录 shell 与 PATH 片段，便于判断是环境问题还是命令问题。
            logger.error(
                "[term.spawn_error] cmd=%r cwd=%s shell=%s kind=%s "
                "exc_type=%s exc=%s path_prefix=%s",
                command[:300], resolved_cwd, shell, kind,
                type(e).__name__, e, (os.environ.get("PATH") or "")[:300],
            )
            return ToolResult(ok=False, output="", error=f"执行失败: {type(e).__name__}: {e}")

        # v1.0 (plan-153-705): 后台模式——注册到 bg_process 立即返回 shell_id。
        # 后台进程不随 cancel_event 终止（dev server 需跨 turn 存活）。
        if not wait_for_completion:
            shell_id = bg_process_registry.register(
                proc, command, resolved_cwd, ctx.session_id,
            )
            return ToolResult(
                ok=True,
                output=(
                    f"命令已进入后台运行。\n"
                    f"shell_id: {shell_id}\n"
                    f"命令: {command}\n"
                    f"工作目录: {resolved_cwd}\n"
                    f"用 terminal_bg_status 查询状态与日志(offset 增量读取),"
                    f"terminal_bg_kill 终止。"
                ),
                data={"shell_id": shell_id, "background": True,
                      "command": command, "cwd": resolved_cwd},
            )

        # 同步模式：增量读取 stdout/stderr 并逐帧上报（v7(B)），保持 cancel/timeout 响应。
        # 不再用 proc.communicate() 一次性取——长命令执行期间前端可实时看到输出。
        # 两个常驻 reader task 并发泵出（避免一端写满阻塞），主循环只负责 cancel/timeout/退出观测。
        max_output = int(settings.tool_output_chars_terminal)
        deadline = asyncio.get_running_loop().time() + timeout_sec
        out_parts: list[str] = []
        err_parts: list[str] = []

        async def _pump(stream, sink: list[str]) -> None:
            try:
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    text = decode_output(chunk)
                    if not text:
                        continue
                    sink.append(text)
                    if ctx.on_tool_output is not None:
                        try:
                            await ctx.on_tool_output(text)
                        except Exception:
                            logger.debug("[term] on_tool_output 回调失败(非阻塞)", exc_info=True)
                    # 防内存无限膨胀：仅保留尾部若干字符（与最终截断口径一致）
                    if len("".join(sink)) > max_output * 2:
                        sink[:] = ["".join(sink)[-max_output:]]
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[term] 输出泵异常(非阻塞)", exc_info=True)

        out_task = asyncio.create_task(_pump(proc.stdout, out_parts))
        err_task = asyncio.create_task(_pump(proc.stderr, err_parts))
        try:
            # 收敛条件：进程退出（returncode 非 None）或两个 reader 均读到 EOF
            while proc.returncode is None and not (out_task.done() and err_task.done()):
                if ctx.cancel_event and ctx.cancel_event.is_set():
                    await kill_process_tree(proc)
                    return ToolResult(ok=False, output="", error="已被用户中断")
                if asyncio.get_running_loop().time() >= deadline:
                    raise asyncio.TimeoutError
                # 主循环只轮询，不等待输出（reader task 常驻）
                await asyncio.sleep(0.1)
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            logger.error(
                "[term.timeout] cmd=%r cwd=%s shell=%s kind=%s timeout=%ss "
                "elapsed=%.1fs pid=%s",
                command[:300], resolved_cwd, shell, kind, timeout_sec,
                time.monotonic() - _t0, getattr(proc, "pid", None),
            )
            return ToolResult(
                ok=False, output="",
                error=(
                    f"超时(>{timeout_sec}s)，进程已强制终止。"
                    f"长命令可用 timeout 参数延长等待,或 waitForCompletion=false 转后台运行。"
                ),
            )
        finally:
            # 兜底：若仍有余留 reader（如进程退出但管道未 EOF），取消并回收
            for t in (out_task, err_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(out_task, err_task, return_exceptions=True)

        # 输出泵 EOF 不等于 asyncio 已回收到退出码：此处兜底回收，避免 returncode
        # 停在 None 把成功命令误判为失败（rc=None 的偶发回归现场见 _await_returncode）。
        rc = await _await_returncode(proc)

        out = "".join(out_parts)
        err = "".join(err_parts)
        combined = out
        if err:
            combined += ("\n-- stderr --\n" + err) if combined else err
        if len(combined) > max_output:
            combined = combined[:max_output] + "\n...(已截断)"

        # v36: 从 debug 提到 info——生产默认 INFO，出问题时才有据可查；
        # 输出长度与退出码是判断「命令是否真的生效」的关键。
        logger.info(
            "[term.done] rc=%s elapsed=%.1fs cmd=%r cwd=%s out_len=%s err_len=%s "
            "err_prefix=%s",
            rc, time.monotonic() - _t0, command[:300],
            resolved_cwd, len(out), len(err), err[:200],
        )
        data: dict[str, Any] = {"returncode": rc, "cwd": resolved_cwd, "cmd": command}
        # plan-75-332: 越界 cwd 仅作审计标记（不再有"放行开关"）
        try:
            _inside = Path(resolved_cwd).resolve().is_relative_to(Path(ctx.workspace_root).resolve())
        except (OSError, ValueError):
            _inside = False
        if not _inside:
            data["outside_access"] = True
        return ToolResult(
            ok=rc == 0,
            output=combined or "(无输出)",
            data=data,
            error="" if rc == 0 else f"退出码 {rc}",
        )

    def approval_precheck(self, args: dict[str, Any], ctx: ToolContext) -> tuple[bool, str]:
        """plan-75-332: 工具不再自行决定免审。

        改造前本钩子会把只读命令判为"免审批"，等于工具自身做了审批决策；
        现在是否放行统一由 `approval_policy.decide()` 判定（只读命令归
        exec_readonly：自动审批下放行，询问审批下仍需确认）。
        保留空实现仅为兼容基类契约。
        """
        return False, ""

    @staticmethod
    def _resolve_path(workspace_root: str, rel: str, allow_outside: bool = True) -> str:
        """把相对/绝对路径解析为绝对路径。

        plan-75-332: 默认不再限制在工作区内（参数 `allow_outside` 保留以兼容旧调用）。
        工具的职责是"解析并执行"；越界访问是否需询问用户，交由 approval_policy 裁决。
        """
        from app.orchestration.tools.safe_path import resolve_loose

        if allow_outside:
            resolved = resolve_loose(rel, workspace_root)
            return resolved or workspace_root
        from app.orchestration.tools.safe_path import safe_resolve
        # 保守路径：仅限工作区内，越界回退 workspace_root（外部调用方显式要求时使用）
        resolved = safe_resolve(workspace_root, rel)
        if resolved is not None:
            return str(resolved)
        p = Path(rel)
        if p.is_absolute():
            logger.warning("terminal cwd 绝对路径越界: %s，回退 workspace", rel)
            return workspace_root
        return workspace_root

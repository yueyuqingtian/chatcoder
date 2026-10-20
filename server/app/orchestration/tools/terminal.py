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
from app.orchestration.output_paging import RAW_OUTPUT_CEILING, present_output
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.bg_process import (
    bg_process_registry, decode_output, kill_process_tree,
)
from app.orchestration.tools.git_root import resolve_repo_for_cwd, list_git_repos
from app.orchestration.tools.shell_env import resolve_shell, shell_kind
from app.orchestration.tools.shell_hints import failure_hint, rewrite_null_device

logger = logging.getLogger(__name__)

# plan-20261020-pts：命令开头的切换目录（cd / chdir / Set-Location / sl / pushd / Push-Location，路径可带引号）
_LEADING_CD_RE = re.compile(
    r"^\s*(?:cd|chdir|set-location|sl|pushd|push-location)\s+(?:-(?:literal)?path\s+)?"
    r"(?P<dir>\"[^\"]+\"|'[^']+'|[^\s&;|]+)\s*(?:&&|;|&)\s*",
    re.I,
)
# 命令中任意位置的切换目录（用于判断「命令自己进了仓库目录」）
_CD_ANYWHERE_RE = re.compile(r"(?:^|[;&|(\s])(?:cd|chdir|set-location|sl|pushd|push-location)\s+\S", re.I)
# git 命令词（排除 .gitignore / github / digit 这类子串）；git -C <dir> 表示已指定仓库
_GIT_CMD_RE = re.compile(r"(?<![\w.-])git(?:\.exe)?(?=\s|$)", re.I)
_GIT_DASH_C_RE = re.compile(r"(?<![\w.-])git(?:\.exe)?\s+-C\s+\S", re.I)

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


async def detach_to_background(
    proc: asyncio.subprocess.Process,
    *,
    command: str,
    cwd: str | None,
    session_id: int,
    turn_id: int | None = None,
    seeded: str = "",
    out_task: asyncio.Task | None = None,
    err_task: asyncio.Task | None = None,
    started_at: float | None = None,
) -> ToolResult:
    """把**已在运行**的前台命令转后台（不终止进程），立即返回 shell_id。

    plan-474-2105：主代理在同步命令里收到子会话提问时，不能把命令杀掉（等于白跑），
    也不能一直等（提问会饿死）。这里把进程交给后台注册表继续收输出，控制权立刻让回。
    转之前已读到的输出用 `seed_output` 写进后台缓冲，日志不会断档。
    plan-20261020-bgtask：`started_at` 沿用命令真正的开跑时刻，后台任务栏的计时不从转后台那一刻重数。
    """
    for t in (out_task, err_task):
        if t is not None and not t.done():
            t.cancel()
    pending = [t for t in (out_task, err_task) if t is not None]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    from app.orchestration.bg_notify import on_bg_exit

    shell_id = bg_process_registry.register(
        proc, command, cwd, session_id, turn_id=turn_id, on_exit=on_bg_exit,
        started_at=started_at,
    )
    if seeded:
        bg_process_registry.seed_output(shell_id, seeded)
    logger.info(
        "[term.detach] shell_id=%s pid=%s cmd=%r",
        shell_id, getattr(proc, "pid", None), command[:200],
    )
    return ToolResult(
        ok=True,
        output=(
            "命令已转后台继续运行（收到需要优先处理的消息，先处理消息）。\n"
            f"shell_id: {shell_id}\n"
            f"命令: {command}\n"
            "用 terminal_bg_status 查询状态与日志(offset 增量读取)，terminal_bg_kill 终止。\n"
            "进程不会因为本轮结束而停止；跑完后结果会自动回到本会话。"
        ),
        data={"shell_id": shell_id, "background": True, "detached": True,
              "command": command, "cwd": cwd or ""},
    )


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
        "三种运行方式:\n"
        "1. 同步(默认 waitForCompletion=true): 阻塞到命令结束并直接返回输出,适合几分钟内能跑完的命令。\n"
        "2. 后台(waitForCompletion=false)·会自己结束: 构建、全量测试、批处理等耗时命令——立即返回 shell_id,\n"
        "   你可以先去做别的事;命令跑完后系统会主动把结果送回本会话,不必轮询。\n"
        "3. 后台(waitForCompletion=false)·长驻服务: dev server、watch、后端服务永远不会自己结束——\n"
        "   用 terminal_bg_status 看日志确认启动成功,用完后必须调用 terminal_bg_kill 主动停止,别留在后台占着端口。\n"
        "   用户也能在界面右上角「后台任务」里手动停止,停止后会通知你。\n"
        "重要: 等待后台命令完成时,禁止用 Start-Sleep / sleep 固定等待(无法感知真正完成,\n"
        "且会让界面长时间无输出)。需要在当前步骤用到结果时,传 terminal_bg_status 的 wait_until_done=true\n"
        "一次等待完成(后台进程结束即返回,非固定秒数);对长驻服务不要这样等。\n"
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

        # 2. 如果命令以 'cd xxx && yyy' / 'Set-Location xxx; yyy' 开头,提取 xxx 作为 cwd,并移除这一段
        #    (否则 subprocess 的 cwd=xxx 已切到该目录,cd xxx 再执行一次会失败)
        #    plan-20261020-pts：补上 Set-Location / sl / pushd / Push-Location 与带引号（含空格）的路径；
        #    只处理开头那一段——中途的 cd 交给 shell 自己执行，摘掉会改变后续命令的语义。
        cd_chain_match = _LEADING_CD_RE.match(command)
        explicit_cwd = args.get("cwd", None)
        # plan-75-332: 取消了 cwd 越界限制——工具不再阻止在工作区外目录执行命令
        # （是否询问用户由 approval_policy 按权限模式裁决）。
        resolved_cwd: str | None = None

        if explicit_cwd:
            resolved_cwd = self._resolve_path(ctx.workspace_root, explicit_cwd)
        elif cd_chain_match:
            cd_dir = cd_chain_match.group("dir").strip().strip('"').strip("'")
            resolved_cwd = self._resolve_path(ctx.workspace_root, cd_dir)
            command = command[cd_chain_match.end():].strip()

        # 3. 若仍未确定 cwd 且命令含 git,尝试自动探测
        # plan-20261020-pts：按 git 命令词判断（此前子串 "git" 会命中 .gitignore / github / digit）
        uses_git = bool(_GIT_CMD_RE.search(command))
        if not resolved_cwd:
            if uses_git:
                resolved_cwd = resolve_repo_for_cwd(ctx.workspace_root)
            else:
                resolved_cwd = ctx.workspace_root

        # 4. 如果 cwd 是 workspace 根(非 git 仓库)但命令是 git → 报错提示 agent 用 cwd 参数
        #    plan-20261020-pts：命令自己切换了目录（Set-Location / cd / pushd）或用 git -C 指定了仓库时放行——
        #    此前 `Set-Location clinic; git diff` 这类写法一律被拦（真实库里 22 次）。
        if (
            uses_git and resolved_cwd == ctx.workspace_root
            and not _CD_ANYWHERE_RE.search(command) and not _GIT_DASH_C_RE.search(command)
        ):
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
        # plan-20261020-pts：PowerShell / cmd 下把 > /dev/null 改写成 $null / nul（Windows 没有这个设备，
        # 原样执行要么报找不到路径，要么在盘符根目录建出 dev 目录）
        command, _null_rewritten = rewrite_null_device(command, kind)
        wait_for_completion = _parse_wait_for_completion(args.get("waitForCompletion"))
        timeout_sec = _parse_timeout(args.get("timeout"))
        _t0 = time.monotonic()
        _started_wall = time.time()  # plan-20261020-bgtask：转后台时沿用真正的开跑时刻
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
            # plan-474-2105：注册完成回调——进程退出后主动把结果送回发起会话
            from app.orchestration.bg_notify import on_bg_exit

            shell_id = bg_process_registry.register(
                proc, command, resolved_cwd, ctx.session_id,
                turn_id=getattr(ctx, "turn_id", None),
                on_exit=on_bg_exit,
            )
            return ToolResult(
                ok=True,
                output=(
                    f"命令已进入后台运行。\n"
                    f"shell_id: {shell_id}\n"
                    f"命令: {command}\n"
                    f"工作目录: {resolved_cwd}\n"
                    f"用 terminal_bg_status 查询状态与日志(offset 增量读取),"
                    f"terminal_bg_kill 终止。\n"
                    f"如果它会自己结束,跑完后系统会把结果送回本会话,不必轮询;"
                    f"如果是长驻服务(dev server / watch),确认可用后记得用完就 terminal_bg_kill 停止。"
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
                    # 防内存无限膨胀：超过原始输出上限才丢弃最早的部分（保留尾部——报错通常在末尾）。
                    # plan-156-620：此前按上下文预算（max_output 的 2 倍）就开始丢头部，
                    # 最终又按 max_output 截开头，长输出中间与结尾的报错都可能丢失且无法取回。
                    if sum(len(x) for x in sink) > RAW_OUTPUT_CEILING:
                        joined = "".join(sink)
                        sink[:] = ["...（更早的输出超过保留上限已丢弃）\n" + joined[-RAW_OUTPUT_CEILING:]]
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
                # plan-474-2105：收到子会话消息 → 命令转后台，进程不终止，控制权让回主代理
                if ctx.detach_event is not None and ctx.detach_event.is_set():
                    return await detach_to_background(
                        proc, command=command, cwd=resolved_cwd,
                        session_id=ctx.session_id,
                        turn_id=getattr(ctx, "turn_id", None),
                        seeded="".join(out_parts),
                        out_task=out_task, err_task=err_task,
                        started_at=_started_wall,
                    )
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
            # plan-156-620：不再按上下文预算硬截断；超出预算的部分由执行层落盘，给出续读方式
            combined = present_output(
                combined, max_output, session_id=getattr(ctx, "session_id", None), tag="terminal",
            )

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
        # plan-20261020-pts：失败时识别常见的 shell 误用（PowerShell 里写 head / grep、Select-String -Recurse、
        # cmd 里写 PowerShell 命令、非 git 目录……），把正确写法直接附在错误后面
        hint = "" if rc == 0 else failure_hint(command, combined, kind)
        return ToolResult(
            ok=rc == 0,
            output=combined or "(无输出)",
            data=data,
            error="" if rc == 0 else f"退出码 {rc}{hint}",
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

"""写盘后的最小检查（plan-548-2277）。

【为什么只做「最小」】
改造前模型改完文件就算完成：语法错误、JSON 写坏、`require` 路径打错，都要等用户跑
命令才暴露——错误从「反馈回路」退化成「用户发现」。业界共识是把可自动判定的检查前移
到写完那一刻（backpressure / feedback sensor），让错误在下一步就被看到。

但全量 typecheck / build 是分钟级，挂在每次编辑后面会把交互拖死。所以这里只跑
**单文件级、秒级、零副作用**的检查：

| 后缀 | 检查 | 说明 |
|---|---|---|
| `.py` | `ast.parse`（`tokenize.open` 读，尊重编码声明/BOM） | 语法错误；不生成 `__pycache__` |
| `.json` | `json.loads` | 写坏的 JSON |
| `.cjs` / `.js` | `node --check` | 语法错误（CommonJS；ESM 由项目 lint 覆盖） |
| 其它 | 跳过并给出原因 | `.ts/.tsx` 的类型检查属于项目级 `npm run typecheck`，不在这里重复跑 |

【限次】
由调用方（agent_loop）控制：同一文件连续 2 轮检查失败就如实报告，不再自动重试。
本模块只负责「跑一次、给结果」，不做重试、不改文件。
"""
from __future__ import annotations

import asyncio
import ast
import json
import shutil
import subprocess
import tokenize
from dataclasses import dataclass
from pathlib import Path

# 单文件检查的后缀白名单
_CHECKABLE = {".py", ".json", ".cjs", ".js"}
# .ts/.tsx 等交给项目级 typecheck；纯文档/样式没有可自动判定的语法错误
_SKIP_REASON = {
    ".ts": "TypeScript 类型检查属于项目级 typecheck / build，不在单文件写后检查里重复执行",
    ".tsx": "TypeScript 类型检查属于项目级 typecheck / build，不在单文件写后检查里重复执行",
    ".md": "文档无可自动判定的语法错误",
    ".css": "样式无可自动判定的语法错误",
    ".scss": "样式无可自动判定的语法错误",
    ".html": "模板无可自动判定的语法错误",
    ".yml": "YAML 未接入单文件检查（避免引入新依赖）",
    ".yaml": "YAML 未接入单文件检查（避免引入新依赖）",
}

# 模型会照抄展示串自查，这里必须是它真能跑通的命令（plan-549-2281 复盘修复）。
# utf-8-sig 对无 BOM 文件等同于 utf-8，同时容忍 PowerShell 5 Set-Content 写出的 BOM。
def _py_command_display(name: str) -> str:
    return (
        'python -c "import ast,pathlib;'
        f"ast.parse(pathlib.Path(r'{name}').read_text(encoding='utf-8-sig'))"
        '"'
    )


@dataclass
class CheckResult:
    """一次写后检查的结果。ok=True 表示通过或跳过。"""

    ok: bool = True
    skipped: bool = False
    reason: str = ""
    command: str = ""
    output: str = ""

    def as_note(self, path_display: str) -> str:
        """给模型看到的一段提示；通过/跳过时返回空串（不打扰上下文）。"""
        if self.ok:
            return ""
        head = f"写后检查未通过（{path_display}）"
        if self.command:
            head += f"：`{self.command}`"
        return head + "\n" + (self.output or "（无输出）")


def _truncate(text: str, *, head: int = 2000, tail: int = 800) -> str:
    """错误输出截断：保留头尾（报错通常在前几行，根因常在尾部）。"""
    text = (text or "").strip()
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n...（已省略 {len(text) - head - tail} 字）...\n{text[-tail:]}"


def _run(cmd: list[str], *, timeout: float) -> tuple[int, str]:
    """跑命令并合并 stdout/stderr；超时/异常统一按失败处理，不让检查本身炸掉 agent。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"检查超时（>{timeout:.0f}s）"
    except FileNotFoundError:
        return 127, f"命令不存在：{cmd[0]}"
    except Exception as e:  # noqa: BLE001 — 检查失败不能影响主流程
        return 1, f"检查执行异常：{e}"


def _check_python_syntax(path: Path) -> CheckResult:
    """就地做 `ast.parse` 语法检查，不拉任何子进程。

    plan-549-2281 复盘修复：此前用 `[sys.executable, "-c", …]` 跑子进程，而后端以
    PyInstaller 发布——打包态 `sys.executable` 是 chatcoder-server.exe（入口 run_server.py），
    `-c` 不是它认识的参数，`_maybe_run_mcp_server` 只认 `--mcp-server`，其余 argv 直接走
    main() → _setup_env() → 抢实例锁；锁被运行中的后端持有即非零退出。
    后果是发行版里**每次写 .py 都报「写后检查未通过」**，报错正文是「后端已在运行」，
    模型会去修一个不存在的语法错误，每次编辑还试图拉起一个完整后端争端口/争锁。

    `ast.parse` 零副作用（不生成 __pycache__、不执行被检代码），开发态与打包态行为一致，
    因此不需要子进程隔离；`tokenize.open` 负责尊重编码声明与 BOM（对应本机 PowerShell 5
    的 Set-Content 默认写 BOM 这一环境事实）。
    """
    display = _py_command_display(str(path))
    try:
        with tokenize.open(path) as fh:
            source = fh.read()
    except (OSError, SyntaxError, UnicodeDecodeError) as e:
        return CheckResult(ok=False, command=display, output=f"读取文件失败：{e}")
    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as e:
        where = f"第 {e.lineno or 0} 行第 {e.offset or 0} 列"
        return CheckResult(
            ok=False, command=display, output=f"语法错误：{where}：{e.msg}",
        )
    return CheckResult(ok=True, command=display)


def check_written_file_sync(path: Path, *, timeout: float = 30.0) -> CheckResult:
    """同步版（内部用；外面请走 check_written_file）。"""
    if not path.is_file():
        return CheckResult(skipped=True, reason="文件不存在或不是普通文件")

    suffix = path.suffix.lower()
    if suffix not in _CHECKABLE:
        return CheckResult(skipped=True, reason=_SKIP_REASON.get(suffix, "该类型未接入写后检查"))

    if suffix == ".py":
        # plan-549-2281 复盘修复：不再依赖 sys.executable（打包态是 exe，拉起来只会抢实例锁），
        # 改为进程内 ast.parse；展示串同步换成模型能照抄的单行检查。
        return _check_python_syntax(path)

    if suffix == ".json":
        try:
            # plan-549-2281 复盘修复：utf-8-sig——带 BOM 的 JSON 解码后首字符是 \ufeff，
            # 旧写法按 utf-8 读会抛 JSONDecodeError「line 1 column 1」，对合法文件误报。
            # 本机 PowerShell 5 的 Set-Content 默认写 BOM（AGENTS.md 第 7 节），触发条件真实。
            json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
            return CheckResult(ok=True, command=f"json.loads {path.name}")
        except json.JSONDecodeError as e:
            return CheckResult(
                ok=False,
                command=f"json.loads {path.name}",
                output=f"JSON 解析失败：第 {e.lineno} 行第 {e.colno} 列：{e.msg}",
            )

    # .cjs / .js
    node = shutil.which("node")
    if not node:
        return CheckResult(skipped=True, reason="未找到 node，跳过 JS 语法检查")
    if suffix == ".js" and _package_type_is_module(path):
        # plan-549-2281 复盘修复：`node --check` 对 .js 按 CommonJS 解析——ESM 项目
        # （package.json "type": "module"）里的 import 语句会被误报语法错，跳过并说明。
        return CheckResult(
            skipped=True,
            reason="该 .js 属于 ESM 包（package.json type=module），node --check 按 CommonJS "
                   "解析会误报，已跳过（ESM 语法由项目 lint / build 覆盖）",
        )
    code, out = _run([node, "--check", str(path)], timeout=timeout)
    return CheckResult(
        ok=code == 0,
        command=f"node --check {path.name}",
        output="" if code == 0 else _truncate(out),
    )


def _package_type_is_module(path: Path) -> bool:
    """从文件向上找最近的 package.json，判断是否声明 `"type": "module"`。

    读不到 / 解析失败一律按非 ESM 处理（宁可多跑一次检查，不静默跳过）。
    """
    for parent in path.resolve().parents[:12]:
        pkg = parent / "package.json"
        try:
            if not pkg.is_file():
                continue
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            return bool(isinstance(data, dict) and data.get("type") == "module")
        except Exception:  # noqa: BLE001 — 判定失败按非 ESM 处理
            return False
    return False


async def check_written_file(path: Path | str, *, timeout: float = 30.0) -> CheckResult:
    """异步入口：把同步检查挪到线程，避免阻塞事件循环。"""
    return await asyncio.to_thread(check_written_file_sync, Path(path), timeout=timeout)


def is_checkable(path: Path | str) -> bool:
    """该路径是否属于会真正执行检查的类型（调用方可据此避免无意义的线程切换）。"""
    return Path(path).suffix.lower() in _CHECKABLE


# ── 与工具结果对接 ────────────────────────────────────────────────────────────
# 会写盘的工具：约定它们的 ToolResult.data 里带本次写过的文件
_EDIT_TOOLS = ("fs_write", "editor_apply_diff", "multi_file_edit")

# plan-568-2333 P8：多文件编辑时并发跑检查。改造前逐个 await，`multi_file_edit` 一次
# 20 个 .js 文件就是 20 次 `node --check` 子进程串行启动（每次几十到数百毫秒），最坏
# 还要按 20 × timeout 累计预算。并发度取 4：足以吃满进程启动的等待，又不会在写盘
# 高峰把 CPU 抢光；单个文件仍然各自受 timeout 约束。
_CHECK_CONCURRENCY = 4

# 按会话保存「同一文件连续失败次数」。跨 turn 保留是有意的：同一文件反复检查失败时
# 应当提醒模型停下来读错误，而不是每个 turn 重新开始盲改。
_FAIL_COUNTS: dict[int, dict[str, int]] = {}
_MAX_TRACKED_FILES = 50


def fail_counts_for(session_id: int) -> dict[str, int]:
    """取该会话的连续失败计数表（顺势清理已恢复的键，防止长会话无限增长）。"""
    counts = _FAIL_COUNTS.get(session_id)
    if counts is None:
        counts = {}
        _FAIL_COUNTS[session_id] = counts
    elif len(counts) > _MAX_TRACKED_FILES:
        for k in [k for k, v in counts.items() if v <= 0]:
            counts.pop(k, None)
    return counts


def clear_fail_counts(session_id: int) -> None:
    """会话删除/关闭时清掉该会话的失败计数——plan-549-2281 复盘修复：
    _FAIL_COUNTS 此前没有任何清理入口，长驻服务上带失败记录的会话键常驻内存。"""
    _FAIL_COUNTS.pop(int(session_id), None)


def paths_from_result(tool_name: str, data: object) -> list[str]:
    """从工具结果的 data 里取出本次写过的文件（相对工作区或绝对路径）。

    非编辑类工具一律返回空：调用方可能对任意工具结果复用本函数，
    不能因为 data 里碰巧有 path 字段（如 fs_read）就把只读操作当成写盘。
    """
    if tool_name not in _EDIT_TOOLS or not isinstance(data, dict):
        return []
    if tool_name == "multi_file_edit":
        files = data.get("files") or []
        return [str(f) for f in files if f] if isinstance(files, list) else []
    path = data.get("path")
    return [str(path)] if path else []


async def check_after_tool(
    tool_name: str,
    data: object,
    workspace_root: str,
    *,
    fail_counts: dict[str, int] | None = None,
    max_rounds: int = 2,
    timeout: float = 30.0,
) -> str:
    """编辑类工具成功后的统一入口：返回要附给模型的提示（通过时为空串）。

    `fail_counts` 由调用方跨轮持有：同一文件连续失败到 `max_rounds` 后不再跑检查，
    改为提醒模型先读报错定位根因——避免「检查 → 盲改 → 再检查」的空转。
    """
    if tool_name not in _EDIT_TOOLS:
        return ""
    paths = paths_from_result(tool_name, data)
    if not paths:
        return ""

    counts = fail_counts if fail_counts is not None else {}
    # plan-560-2311 D4：键必须与 read_cache 的路径口径一致。此前用 `str(p).lower()`，
    # 而 pathlib 不会折叠 `..`——`a/../b.py` 与 `b.py` 得到两个键，同一文件的失败计数
    # 被拆成两份，「连续 N 次未通过就停止重试」的限次形同失效（可被 `..` 无限绕过）。
    # norm_path 走 abspath + normcase，正是缺的那一步；只在键计算处使用，展示值不变。
    from app.orchestration.tools.read_cache import norm_path

    # plan-568-2333 P8：分三阶段——先同步解析路径并算计数键，再并发跑检查，最后按
    # 原顺序汇总提示与计数。这样既拿到并发的耗时收益，又保证：提示顺序、`fail_counts`
    # 的递增/归零语义、以及「同一文件重复失败」的计数方式与串行版完全一致。
    entries: list[tuple[str, str]] = []   # (展示路径, 计数键)，顺序即提示顺序
    targets: dict[str, Path] = {}          # 计数键 → 绝对路径，同一文件只查一次
    for rel in paths:
        p = Path(rel)
        if not p.is_absolute():
            p = Path(workspace_root) / rel
        if not is_checkable(p):
            continue
        key = norm_path(p)
        entries.append((rel, key))
        # 已达上限的文件不会真跑检查（下面汇总时会出「请先读报错」的提示），无需排进并发表
        if int(counts.get(key, 0)) < max_rounds:
            targets.setdefault(key, p)

    results: dict[str, CheckResult] = {}
    if targets:
        sem = asyncio.Semaphore(_CHECK_CONCURRENCY)

        async def _check_one(key: str, path: Path) -> tuple[str, CheckResult]:
            async with sem:
                try:
                    return key, await check_written_file(path, timeout=timeout)
                except Exception as e:  # noqa: BLE001 — 单文件异常不能影响同批其它文件
                    return key, CheckResult(ok=False, output=f"检查执行异常：{e}")

        # 结果按计数键回填，不依赖完成先后；gather 会等全部完成再返回，异常已在任务内收敛
        for key, res in await asyncio.gather(
            *(_check_one(k, v) for k, v in targets.items())
        ):
            results[key] = res

    notes: list[str] = []
    for rel, key in entries:
        # 按顺序重读计数（而不是用并发前的快照）：同一文件在一次调用里出现多次时，
        # 后续那次能看到前面已累加的失败次数，与改造前串行累加的结果一致。
        used = int(counts.get(key, 0))
        if used >= max_rounds:
            notes.append(
                f"（{rel}：写后检查已连续 {used} 次未通过，请先读报错定位根因再改，"
                "不要继续盲目改写；若确认是无关的既有问题，在回复里说明。）"
            )
            continue
        res = results.get(key)
        if res is None:
            continue
        if res.ok:
            counts[key] = 0
            continue
        counts[key] = used + 1
        notes.append(res.as_note(rel))
    return "\n\n".join(n for n in notes if n)

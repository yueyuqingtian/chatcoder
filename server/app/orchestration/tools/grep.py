"""grep 搜索工具 — 在工作区内搜索文件内容。

支持正则表达式和纯文本搜索，可按文件扩展名过滤。
这是 Agent 最核心的代码导航能力之一，没有它 Agent 只能靠 fs.list + fs.read
逐个猜测文件，效率极低。
"""
import asyncio
import fnmatch
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
# 单次返回条数：默认 50，可经 max_matches 上调到 200。
# plan-548-2276（阶段 4.1）：此前是固定硬截断，命中被切掉后模型永远看不到后续，
# 只能换关键词反复试；现在改为可翻页，截断时头部直接给出下一次的完整调用串。
_MAX_MATCHES = 50
_MAX_MATCHES_CEILING = 200

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


def _as_int(value: Any, default: int) -> int:
    """模型偶尔把整数参数写成字符串；解析不了就用默认值，不让整次搜索失败。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    """布尔参数同样要容错：模型可能传 null / "true" / 0。

    plan-562-2318 M7：只认明确的真值，其余（含 None）一律回落默认值——
    不能让 null 被当成真，也不能让类型错误把整次搜索打成「工具异常」。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    if isinstance(value, (int, float)):
        return value != 0
    return default


def _quote_arg(value: str) -> str:
    """回填进「下一次调用」串时按 JSON 字符串转义，保证复制出去就能直接执行。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _next_call(
    pattern: str, path: str, include: str, next_offset: int,
    case_sensitive: bool = False,
    context_lines: int = 0,
    max_matches: int = _MAX_MATCHES,
) -> str:
    """生成可直接复制的续读调用：参数按本次实际传入值原样回填，模型不必自己想。

    plan-560-2311 D5：此前只回填 pattern / path / include / offset，漏了
    case_sensitive / context_lines / max_matches——续页会换一套匹配口径与页宽，
    与第一页的命中集对不上（大小写敏感翻转、上下文消失、页大小变化导致重复或漏项）。
    只在参数偏离默认值时才写进串里，保持提示简洁。
    """
    parts = [f'pattern="{_quote_arg(pattern)}"']
    if path:
        parts.append(f'path="{_quote_arg(path)}"')
    if include:
        parts.append(f'include="{_quote_arg(include)}"')
    if case_sensitive:
        parts.append("case_sensitive=true")
    if context_lines > 0:
        parts.append(f"context_lines={context_lines}")
    if max_matches != _MAX_MATCHES:
        parts.append(f"max_matches={max_matches}")
    parts.append(f"offset={next_offset}")
    return "fs_grep(" + ", ".join(parts) + ")"


def _page_notes(
    *, pattern: str, path: str, include: str, offset: int,
    returned: int, has_more: bool, files_searched: int = 0,
    case_sensitive: bool = False, context_lines: int = 0,
    max_matches: int = _MAX_MATCHES,
) -> list[str]:
    """头部说明：跳过了多少、是否还有后续（附续读调用）、是否撞到扫描上限。

    plan-549-2281 复盘修复：`has_more` 由调用方按「多读一条」的口径算好后传入，
    不再在这里用 `scanned >= window` 自行推断——两套口径在「命中数恰好等于
    offset+max_matches」时自相矛盾：页头写「找到 50 个匹配」（无 +），notes 却说已达上限，
    模型翻页只得到「本页无匹配」，白耗一次调用。页头与 notes 必须同一个事实源。

    plan-560-2311 D5：把 case_sensitive / context_lines / max_matches 一并传给
    `_next_call`，让续读串与本次调用口径完全一致。
    """
    notes: list[str] = []
    if offset > 0:
        notes.append(f"本页从第 {offset + 1} 条起（已跳过前 {offset} 条）")
    if has_more:
        notes.append(
            f"已达本次上限 {returned} 条，如需继续请调用 "
            f"{_next_call(pattern, path, include, offset + returned, case_sensitive, context_lines, max_matches)}"
        )
    if files_searched >= _MAX_FILES_SCAN:
        notes.append(f"已扫描上限 {_MAX_FILES_SCAN} 个文件")
    return notes


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
                        "offset": {
                            "type": "integer",
                            "description": (
                                "跳过前 N 条匹配后再返回（分页用，默认 0）。"
                                "本次结果被截断时，头部会给出下一次应传的 offset"
                            ),
                        },
                        "max_matches": {
                            "type": "integer",
                            "description": (
                                "本次最多返回多少条匹配"
                                f"（默认 {_MAX_MATCHES}，上限 {_MAX_MATCHES_CEILING}）"
                            ),
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
        # plan-562-2318 M7：此前是裸 args.get + min(...)，模型传 null 时
        # min(None, 5) 抛 TypeError，被 executor 兜成「工具异常」，整次搜索零结果。
        # 现在与下方 offset / max_matches 同口径：先做类型归一，再 clamp 到合法区间。
        case_sensitive = _as_bool(args.get("case_sensitive"), False)
        context_lines = min(max(_as_int(args.get("context_lines"), 0), 0), 5)
        # plan-548-2276（阶段 4.1）：分页参数。内部只收集到 offset+max_matches 即停，
        # 不先扫全量再切片——大仓库下后者等于每次翻页都全量扫一遍。
        offset = max(0, _as_int(args.get("offset"), 0))
        max_matches = min(
            max(_as_int(args.get("max_matches"), _MAX_MATCHES), 1), _MAX_MATCHES_CEILING,
        )
        window = offset + max_matches

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
        # plan-560-2311 D5：此前手工替换 `.` 与 `*`，其它正则元字符（+ ( ) [ ] { } ^ $ | \）
        # 不转义会让 re.compile 抛 re.error，把整次搜索打断；`?` 也没按 glob 语义转。
        # 改用 fnmatch.translate（标准库自带完整转义与 `?` → 单字符语义），
        # 并加异常保护：失败只放弃 include 过滤，不让整次搜索失败。
        include_re = None
        if include_glob:
            try:
                include_re = re.compile(fnmatch.translate(include_glob), re.IGNORECASE)
            except re.error as e:
                logger.warning(
                    "[grep] include glob 无法转成正则，已忽略 include 过滤: %r (%s)",
                    include_glob, e,
                )
                include_re = None

        # v1.1: 增强搜索 — enhanced_search 开启且能定位到 ripgrep（内置或 PATH）时走 rg 快路径；
        # rg 不可用/超时/失败返回 None，回退下方纯 Python 逐行匹配。
        # v1.2: rg 快路径仅对目录生效（rg 的 cwd 必须是目录），单文件走纯 Python 匹配。
        rg = locate_ripgrep() if (root.is_dir() and settings.enhanced_search) else None
        if rg is not None:
            rg_result = await self._rg_search(
                root=root, ws_root=ws_root, pattern=pattern_str,
                include_glob=include_glob, case_sensitive=case_sensitive,
                context_lines=context_lines, fixed=bool(literal_note), rg_path=rg.path,
                offset=offset, max_matches=max_matches, path_arg=str(search_path or ""),
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

        # plan-549-2281 复盘修复：has_more 原先按「计数达到窗口」判定——末条恰好填满窗口时
        # 误报还有更多，下一页返回「本页无匹配」。改为多读一条（窗口 +1）：只有真的数出
        # 第 window+1 条才报 has_more；多读的那条只计数、不进本页结果。
        # plan-549-2281 复盘修复（2）：撞到文件扫描上限时记标志——空结果 / 页头据此区分
        # 「确实没有更多匹配」与「只是还没扫到」，否则模型会误判到底而放弃。
        scan_capped = False
        for file_path in iter_files():
            if files_searched >= _MAX_FILES_SCAN:
                scan_capped = True
                break
            if total_matches > window:
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
                if total_matches > window:
                    break
                if regex.search(line):
                    total_matches += 1
                    if total_matches <= offset or total_matches > window:
                        continue  # 本页之前的命中 / 仅用于判定 has_more 的额外一条：计数但不出现在结果里
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

        # plan-549-2281 复盘修复：has_more 只算一次，页头 / notes / data 共用同一个值
        has_more = total_matches > window
        if not results:
            # plan-549-2281 复盘修复：被 _MAX_FILES_SCAN 截断时 total 只是「已扫描部分」的计数，
            # 不能只说「已超出结果数，共 N 条」——那会让模型以为翻到底了而放弃。
            scan_note = ""
            if scan_capped:
                scan_note = (
                    f"；注意：已达文件扫描上限（只扫描了 {files_searched} 个文件，"
                    "可能仍有未覆盖的文件），可用 include / path 缩小范围后重试，"
                    "不要据此断定没有更多匹配"
                )
            if offset > 0:
                output = (
                    f"本页无匹配（offset={offset} 已超出结果数，共 {total_matches} 条；"
                    f"搜索了 {files_searched} 个文件{scan_note}）"
                )
            else:
                output = f"未找到匹配项 (搜索了 {files_searched} 个文件{scan_note})"
        else:
            count_note = f"{window}+" if has_more else str(total_matches)
            header = f"找到 {count_note} 个匹配 (搜索了 {files_searched} 个文件)"
            notes = _page_notes(
                pattern=pattern_str, path=str(search_path or ""),
                include=str(include_glob or ""), offset=offset,
                returned=len(results), has_more=has_more,
                files_searched=files_searched,
                # plan-562-2318 H2：续读串必须与本次调用的匹配口径、页宽完全一致，
                # 否则 case_sensitive / context_lines 会在续页丢失，max_matches 还会
                # 回落成默认 50 直接跳过中间命中。
                case_sensitive=case_sensitive, context_lines=context_lines,
                max_matches=max_matches,
            )
            if notes:
                header += "（" + "；".join(notes) + "）"
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
                "offset": offset,
                "max_matches": max_matches,
                "returned": len(results),
                "has_more": has_more,
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
        offset: int = 0,
        max_matches: int = _MAX_MATCHES,
        path_arg: str = "",
    ) -> ToolResult | None:
        """v1.1: ripgrep 快路径。任何异常/不可用返回 None，由调用方回退纯 Python。

        fixed=True：按字面文本搜索（-F），用于模型给的正则不合法时的降级。
        注意：--max-filesize 只接受纯字节数或 K/M/G 后缀，不能带 B（否则 rg 报错，快路径形同虚设）。

        plan-548-2276：offset/max_matches 为分页参数，Python 侧按条数跨文件累计截断。

        plan-549-2281 复盘修复：`--max-count` 是**单文件**上限，此前传
        offset+max_matches+1——offset=10000 时每个文件都要吐 10051 条，绝大部分在
        Python 侧解析到 stop_at 就被丢弃，纯属白造一份巨型输出。现在先按小值
        `max_matches+1` 跑，只有真的被单文件上限截断（某文件命中数顶满上限、且还没
        数够 offset+max_matches+1 条）时才按需要的上限重跑一次：常见翻页只产生一页的
        量，而「同一查询连续翻页不重不漏」完全不变。
        path_arg 只用于把用户原始 path 回填进「下一次调用」提示串。"""
        window = offset + max_matches
        stop_at = window + 1
        base_cmd = [
            rg_path, "--no-heading", "-n", "--color", "never",
            # -0：路径后接 NUL 而不是 ":"，匹配行为 "路径\0行号:内容"、上下文行为 "路径\0行号-内容"，
            # 解析时不会被路径或内容里的 ":数字:" 误导
            "-0",
            "--max-filesize", str(_MAX_FILE_SIZE),
        ]
        if fixed:
            base_cmd.append("-F")
        if not case_sensitive:
            base_cmd.append("-i")
        if context_lines > 0:
            base_cmd.append(f"-C{context_lines}")
        if include_glob:
            base_cmd += ["-g", include_glob]
        for d in _DEFAULT_EXCLUDE_DIRS:
            base_cmd += ["-g", f"!{d}/**"]
        for ext in _BINARY_EXTENSIONS:
            base_cmd += ["-g", f"!*{ext}"]

        async def _run_rg(per_file_cap: int, *, need_stable_order: bool = False) -> str | None:
            """按给定的「单文件命中上限」跑一次 rg；不可用/失败返回 None（调用方回退）。

            plan-560-2311 D5：`need_stable_order=True` 时追加 `--sort path`。rg 默认多线程
            输出，跨文件的条目顺序每次都可能不同——「同一查询连续翻页不重不漏」因此失效。
            但 `--sort` 会让 rg 退化为串行，全量检索明显变慢，所以只在需要跨页对齐时加。

            plan-562-2318 M8：此前口径是「首屏并行、续页排序」，首页与续页的全局命中序号
            基准不同，续页会重复首页条目并漏掉若干条。现在由调用方判定「本页是否会被
            max_matches 截断」：一旦截断（模型必然续页），立即用排序口径重跑同一页，
            使首页与后续所有页共享同一套稳定顺序；未截断的单页检索仍走并行快路径。
            """
            cmd = base_cmd + ["--max-count", str(per_file_cap)]
            if need_stable_order:
                cmd += ["--sort", "path"]
            cmd += ["-e", pattern, "."]
            timeout_s = 30.0
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(root),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                out_b, _err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError:
                # plan-562-2318 M9：超时后必须真正终止子进程——此前直接 return None，
                # 每次超时都泄漏一个仍在跑的 rg，同时回退到更慢的纯 Python 全量扫描，
                # 大仓库下容易连续超时。注意 asyncio.TimeoutError 在 3.10+ 是 OSError
                # 的子类，本分支必须排在 OSError 之前。
                if proc is not None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except (OSError, ValueError):
                        pass  # 进程已自行退出 / 句柄失效，不影响回退
                logger.warning(
                    "[grep] rg 超时（上限 %.0fs）已终止子进程，回退纯 Python 逐行匹配",
                    timeout_s,
                )
                return None
            except OSError as e:
                logger.debug("[grep] rg 启动失败，回退纯 Python 逐行匹配: %s", e)
                return None
            if proc.returncode not in (0, 1):
                return None  # rc=2: 参数/正则错误 → 回退 Python 分支以复用错误提示
            return out_b.decode("utf-8", errors="replace")

        # rg 输出路径相对 cwd=root；root 在 ws_root 下时补相对前缀
        rel_prefix = ""
        if root != ws_root:
            try:
                rel_prefix = str(root.relative_to(ws_root)).replace("\\", "/") + "/"
            except ValueError:
                rel_prefix = ""

        # 先按「一页的量 + 1」取；出现单文件截断（有文件命中数顶满该上限）时再按
        # stop_at 重跑一次——截断会让后续文件的命中「挤位」，不重跑就会翻页重复/漏项。
        def _parse(raw: str, per_file_cap: int) -> tuple[list[tuple], int, set[str], bool]:
            """解析 rg 输出 → (条目, 全局命中数, 命中文件集, 是否被单文件上限截断)。

            plan-549-2281 复盘修复（两处，与纯 Python 路径同口径）：
            ① 被 offset 跳过的匹配，其上下文行不得漏进本页——此前 continue 只跳过匹配行本身，
               rg -C 的上下文行照常进结果，翻页边界出现「上一页匹配的残影」。
               改为两遍：先解析出（路径/行号/是否命中/命中序号/渲染行），再按本页保留的
               命中行号筛上下文行（与命中行同文件且距离 ≤ context_lines 才保留）。
            ② has_more 多读一条（窗口 +1）判定，末条恰好填满窗口时不再误报。
            """
            entries: list[tuple[str, int, bool, int, str]] = []
            total = 0
            file_set: set[str] = set()
            per_file_hits: dict[str, int] = {}
            for line in raw.splitlines():
                if total >= stop_at:
                    break
                if not line or line == "--":
                    continue
                path_part, nul, rest = line.partition("\0")
                if not nul:
                    continue
                # rg 输出的路径形如 ".\\src\\a.py"（cwd=root 时带 ./ 前缀），统一成与纯 Python 分支一致的相对路径
                path = path_part[2:] if path_part[:2] in ("./", ".\\") else path_part
                m = re.match(r"^(\d+)([:-])", rest)
                if not m:
                    continue
                is_match = m.group(2) == ":"
                match_idx = 0
                if is_match:
                    total += 1
                    match_idx = total
                    file_set.add(path)
                    per_file_hits[path] = per_file_hits.get(path, 0) + 1
                entries.append((path, int(m.group(1)), is_match, match_idx,
                                f"{rel_prefix}{path}{m.group(2)}{rest}"))
            # 某文件命中数顶满单文件上限 → 它可能还有没吐出来的命中
            truncated = any(n >= per_file_cap for n in per_file_hits.values())
            return entries, total, file_set, truncated

        # plan-562-2318 M8：分页一致性优先。续页（offset>0）必然排序；首屏先按并行
        # 快路径跑，一旦发现本页会被 max_matches 截断（total_matches > window，即模型
        # 一定会继续翻页），立即用排序口径重跑同一页——否则首页无序、续页按 path 有序，
        # 两页的全局命中序号基准不同，翻页会重复首页条目并漏掉若干条。
        # 取舍：未截断的单页检索（最常见）仍只跑一次、不排序，不牺牲并行速度；
        # 只在「确实会被截断」时多付一次同页检索的代价。--sort 不改变命中集合，
        # 只改变跨文件顺序，重跑不会让 total_matches 与命中集失真。
        need_stable_order = offset > 0
        per_file_cap = max_matches + 1
        text = await _run_rg(per_file_cap, need_stable_order=need_stable_order)
        if text is None:
            return None
        entries, total_matches, file_set, truncated = _parse(text, per_file_cap)

        if truncated:
            # 有文件顶满了单文件上限，它可能还有没吐出来的命中——此时全局命中序号
            # 会被后续文件的命中「挤位」（本该属于该文件的第 offset+1 条变成了别处的命中），
            # 翻页就会重复/漏项。只要出现截断就按 stop_at 重跑一次：
            # 一页最多需要 stop_at 条命中，单个文件最多也只能贡献这么多，所以这个上限够用。
            # plan-560-2311 D5：重跑必须 `--sort path`——否则两次 rg 的跨文件顺序可能不同，
            # 「挤位」的判断与首次结果对不上，重跑反而引入新的重复/漏项。
            # plan-562-2318 M8：此分支顺带把首屏的排序补上，故与下面的排序重跑互斥，
            # 一次查询最多起两次 rg。
            per_file_cap = stop_at
            text = await _run_rg(per_file_cap, need_stable_order=True)
            if text is None:
                return None
            entries, total_matches, file_set, truncated = _parse(text, per_file_cap)
        elif not need_stable_order and total_matches > window:
            # plan-562-2318 M8：首屏会被 max_matches 截断 ⇒ 模型接着一定会翻页。
            # 用同一套页宽、但带 `--sort path` 的口径重跑本页，使首页与后续各页的
            # 全局命中序号基准一致；页宽不变，所以 total_matches 与命中集不受影响。
            text = await _run_rg(per_file_cap, need_stable_order=True)
            if text is None:
                return None
            entries, total_matches, file_set, truncated = _parse(text, per_file_cap)

        included_idx = set(range(offset + 1, min(total_matches, window) + 1))
        included_lines: dict[str, list[int]] = {}
        for path, lineno, is_match, match_idx, _rendered in entries:
            if is_match and match_idx in included_idx:
                included_lines.setdefault(path, []).append(lineno)
        lines: list[str] = []
        for path, lineno, is_match, match_idx, rendered in entries:
            if is_match:
                if match_idx in included_idx:
                    lines.append(rendered)
            elif context_lines > 0 and any(
                abs(ml - lineno) <= context_lines
                for ml in included_lines.get(path, ())
            ):
                lines.append(rendered)

        has_more = total_matches > window
        page_matches = len(included_idx)
        if not lines:
            if offset > 0:
                output = f"本页无匹配（rg；offset={offset} 已超出结果数，共 {total_matches} 条）"
            else:
                output = "未找到匹配项 (rg)"
        else:
            count_note = f"{window}+" if has_more else str(total_matches)
            header = f"找到 {count_note} 个匹配 (rg)"
            notes = _page_notes(
                pattern=pattern, path=path_arg, include=include_glob,
                offset=offset, returned=page_matches, has_more=has_more,
                # plan-562-2318 H2：续读串必须带上本次的匹配口径与页宽，
                # 否则续页会翻转大小写敏感、丢上下文、页宽回落成默认 50。
                case_sensitive=case_sensitive, context_lines=context_lines,
                max_matches=max_matches,
                # files_searched 不传：rg 路径没有「扫描文件数上限」概念，
                # 传命中文件数会在 ≥_MAX_FILES_SCAN 时给出误导性的「已扫描上限」提示
            )
            if notes:
                header += "（" + "；".join(notes) + "）"
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
                "offset": offset,
                "max_matches": max_matches,
                "returned": page_matches,
                "has_more": has_more,
            },
        )

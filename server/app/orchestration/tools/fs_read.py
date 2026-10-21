"""fs_read 工具 — 读取文件内容(支持行号范围)。

参照 CodeBuddy/Codex 的 read_file 行为，并对齐 zcode / deepseek-harness 的窗口口径：
- 自动显示行号；默认 600 行，单次最多 2000 行（offset/limit 分段）；
- 单行超过 2000 字符截断并标注原长；
- 单次输出按 UTF-8 字节封顶（tool_output_bytes_read，默认 32KB），超出时在页脚给出下一次的 offset；
- 窗口内容完整写入会话落库，续接不需要重新读取；文件本身即来源。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.read_cache import UNCHANGED_STUB, norm_path
from app.orchestration.tools.safe_path import safe_resolve_read
from app.orchestration.tools.text_edit import TextFileError, read_text_file

_DEFAULT_LINES = 600  # 默认行数：多数源文件一次读完，减少分段重读
_MAX_LINES = 2000  # 单次行数上限（与 zcode / deepseek-harness 的 2000 行对齐）
_MAX_LINE_CHARS = 2000  # 单行字符上限，防止超长行（压缩产物、单行 JSON）独占窗口
_MAX_FILE_BYTES = 2 * 1024 * 1024  # 超过则拒绝读取：分段读取仍需整体加载，过大的文件应改用 fs_grep 定位


def _budget_bytes() -> int:
    """fs_read 单次窗口的字节预算（配置项可调，缺失时回落 32KB）。"""
    from app.core.config import settings
    return int(getattr(settings, "tool_output_bytes_read", 32768) or 32768)


def _render_window(
    lines: list[str], start: int, limit: int, budget: int, total: int,
) -> tuple[list[str], int]:
    """按行窗口 + 字节预算渲染带行号的文本，返回 (行列表, 最后显示的行号)。至少返回一行。"""
    width = len(str(total))
    out: list[str] = []
    used = 0
    last = start - 1
    stop = min(start - 1 + limit, len(lines))
    for idx in range(start - 1, stop):
        raw = lines[idx]
        shown = raw
        if len(raw) > _MAX_LINE_CHARS:
            shown = f"{raw[:_MAX_LINE_CHARS]}…[行截断，原长 {len(raw)} 字符]"
        row = f"{idx + 1:>{width}}: {shown}"
        size = len(row.encode("utf-8")) + 1  # +1 为换行符
        if out and used + size > budget:
            break
        out.append(row)
        used += size
        last = idx + 1
    return out, last


class FsReadTool(Tool):
    name = "fs_read"
    risk_level = "low"
    description = (
        "读取工作区内指定文件的文本内容，自动显示行号。\n"
        "默认读取前 600 行；用 offset / limit 分段阅读大文件（单次最多 2000 行，单次输出约 32KB）。\n"
        "- path: 文件路径（相对工作根，如 'clinic/pom.xml'，也可用绝对路径）\n"
        "- offset: 从第几行开始读（1-based，默认 1）\n"
        "- limit: 最多读取多少行（默认 600，单次最多 2000 行）\n"
        "输出被截断时，末尾会给出下一次应使用的 offset；按提示续读即可，不必整体重读。"
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
                        "path": {
                            "type": "string",
                            "description": (
                                "文件路径(相对工作根,如 'clinic/pom.xml';"
                                "也可用绝对路径)"
                            ),
                        },
                        "offset": {
                            "type": "integer",
                            "description": "从第几行开始读取(1-based, 默认1)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多读取多少行(默认600, 单次最多2000行; 输出过长时按页脚的 offset 续读)",
                        },
                    },
                    "required": ["path"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = args.get("path", "")
        # offset/limit 安全解析：模型偶发输出字符串数字或越界值，
        # 直接 max/min 会在 str vs int 比较时抛 TypeError（plan-645）
        try:
            offset = max(1, int(args.get("offset") or 1))
        except (TypeError, ValueError):
            offset = 1
        try:
            # limit=0 是非法值（读 0 行），钳到 1；缺失/None 才回退默认——
            # 不能写 int(args.get("limit") or _DEFAULT_LINES)，0 会被 or 短路成默认
            raw_limit = args.get("limit")
            limit = max(1, min(_MAX_LINES, int(raw_limit) if raw_limit is not None else _DEFAULT_LINES))
        except (TypeError, ValueError):
            limit = _DEFAULT_LINES

        # 附件目录兜底：用户消息附件在工作区外的 uploads 目录，允许只读
        target = safe_resolve_read(ctx.workspace_root, path)
        # plan-20261020-pts：上下文折叠的落盘文件（.compact-cache/<hash>.txt）——旧会话的占位提示指向全局工作区，
        # 会话工作区里没有时回落过去读（本软件自己的缓存，不走工作区外读取的审批）
        if target is None or not target.exists():
            from app.orchestration.tools.path_hints import compact_cache_fallback

            _cache = compact_cache_fallback(ctx.workspace_root, path)
            if _cache is not None:
                target = _cache
        if target is None:
            # plan-75-332: 工作区外读取不再由工具自身弹审批卡——直接解析放行，
            # 是否询问用户由 approval_policy 按权限模式统一裁决。
            from app.orchestration.tools import outside_access

            _outside, _err = outside_access.resolve_outside_target(ctx, path)
            if _outside is None:
                return ToolResult(ok=False, output="", error=_err)
            target = _outside
        elif not target.exists():
            # 工作区内解析成功但文件不存在：可能是附件的「{file_id}/{name}」相对路径，
            # 交由下方统一报「文件不存在」（不再触发审批，工作区内无需授权）。
            pass
        if not target.exists():
            # plan-20261020-pts：附上同名文件的实际位置与最近存在的上级目录，免得模型反复猜路径
            import asyncio

            from app.orchestration.tools.path_hints import missing_path_hint

            hint = await asyncio.to_thread(missing_path_hint, ctx.workspace_root, str(path), want_dir=False)
            return ToolResult(ok=False, output="", error=f"文件不存在: {path}{hint}", error_code="NOT_FOUND")
        if target.is_dir():
            # plan-20261020-pts：传了目录——直接给出目录内容（模型下一步通常就是挑一个文件读），不再报错让它重来
            from app.orchestration.tools.path_hints import list_dir_brief

            listing = list_dir_brief(target, limit=60)
            return ToolResult(
                ok=True,
                output=f"{path} 是目录（不是文件），其中有：{listing}\n读取文件请传具体文件路径；列目录可用 fs_list。",
                data={"path": path, "is_dir": True},
            )
        if not target.is_file():
            return ToolResult(ok=False, output="", error=f"不是文件: {path}", error_code="NOT_FOUND")

        _st = target.stat()
        size = _st.st_size
        if size > _MAX_FILE_BYTES:
            return ToolResult(
                ok=False, output="",
                error=f"文件过大（{size} 字节，超过 {_MAX_FILE_BYTES // (1024 * 1024)}MB 读取上限）: {path}",
                hint="不要反复重试 fs_read。先用 fs_grep 搜索关键字定位行号，再说明需要的内容。",
            )
        # 未变化短路：同一文件、同一行范围、指纹未变，且较早的结果仍完整在上下文中 → 只回存根
        _read_key = (norm_path(target), offset, limit)
        _fingerprint = (_st.st_mtime_ns, _st.st_size)
        _cache = getattr(ctx, "read_cache", None)
        if _cache is not None and _cache.lookup(_read_key, _fingerprint):
            return ToolResult(ok=True, output=UNCHANGED_STUB, data={"path": path, "unchanged": True})

        # plan-20261016-ux（问题 3）：与编辑工具同一解码器——GBK/GB18030 文件显示真实中文（此前是「�」，
        # 模型照着乱码写 old_text 必然匹配失败）；只按换行符切行，行号与编辑器、编辑工具一致
        # （splitlines 会把换页符、U+2028 等也当换行，行号错位）。
        try:
            tf = read_text_file(target, strict=False)
        except TextFileError as e:
            return ToolResult(ok=False, output="", error=str(e))
        text = tf.text

        lines = text.split("\n")
        if len(lines) > 1 and lines[-1] == "":
            lines.pop()
        if lines == [""]:
            lines = []
        total_lines = len(lines)

        header = f"文件: {path} (共 {total_lines} 行)"
        if tf.encoding != "utf-8":
            header += f" | 编码 {tf.encoding.upper()}"

        if total_lines and offset > total_lines:
            return ToolResult(
                ok=True,
                output=f"{header}\n（offset={offset} 已超出文件末尾，文件共 {total_lines} 行；无可显示内容）",
                data={"path": path, "total_lines": total_lines, "shown_lines": ""},
            )
        if not lines:
            return ToolResult(
                ok=True,
                output=f"{header}\n（文件为空）",
                data={"path": path, "total_lines": 0, "shown_lines": ""},
            )

        rows, last = _render_window(lines, offset, limit, _budget_bytes(), total_lines)
        body = "\n".join(rows)
        if last >= total_lines:
            footer = f"（已到文件末尾，共 {total_lines} 行）"
        else:
            # 页脚必须给出精确的下一次 offset，模型照做即可续读，不需要猜
            cut_by_budget = last < min(offset + limit - 1, total_lines)
            footer = (
                f"（显示第 {offset}-{last} 行，共 {total_lines} 行；"
                f"继续读取请用 offset={last + 1}"
                f"{'，本次因单次输出上限提前结束' if cut_by_budget else ''}）"
            )

        return ToolResult(
            ok=True,
            output=f"{header}\n{body}\n{footer}",
            data={
                "path": path, "total_lines": total_lines, "shown_lines": f"{offset}-{last}",
                # 供 agent_loop 在工具结果进入上下文后登记（JSON 安全的列表形式）
                "read_key": [norm_path(target), offset, limit],
                "fingerprint": [_st.st_mtime_ns, _st.st_size],
            },
        )

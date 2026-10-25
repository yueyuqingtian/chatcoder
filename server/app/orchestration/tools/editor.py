"""v0.3: editor.apply_diff 工具(medium risk,走审批)。

简化版:仅支持对单个文件应用 unified diff(基于 Python 标准库 difflib 不易反向,
故采用 "整文件替换 + 旧片段定位" 策略;若失败提示 agent 改用 fs.write)。
"""
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import resolve_loose
from app.orchestration.tools.text_edit import (
    STRATEGY_NOTES,
    TextFileError,
    apply_replacement,
    pick_arg,
    read_text_file,
    write_text_file,
)

# plan-1085: data 带回新内容上限（对齐 agent_loop.MAX_TOOL_OUTPUT_CHARS）；超限不带，走磁盘读取兜底路径
_NEW_CONTENT_MAX_CHARS = 16000


class EditorApplyDiffTool(Tool):
    name = "editor_apply_diff"
    risk_level = "medium"
    description = (
        "对工作区内文件应用 diff:用 new_text 替换文件中出现的 old_text(默认必须唯一匹配,replace_all=true 时替换所有匹配)。需用户审批。"
        "old_text 请逐字复制原文(含缩进与空白),不要带 fs_read 输出的行号前缀。"
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
                        "path": {"type": "string", "description": "工作区内文件相对路径(如 'clinic/src/Main.java';也可用绝对路径)"},
                        "old_text": {"type": "string", "description": "要被替换的原文(默认必须唯一匹配)"},
                        "new_text": {"type": "string", "description": "替换后的文本"},
                        "replace_all": {"type": "boolean", "description": "true=替换文件中所有匹配的 old_text；false=仅唯一匹配时替换，多匹配报错(默认)"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # plan-20261016-ux（问题 3）：参数名容错——模型偶尔沿用其它工具的命名（file_path / old_string …）
        from app.models.providers.tool_args import args_parse_error

        parse_error = args_parse_error(args)
        if parse_error:
            return ToolResult(
                ok=False, output="",
                error=f"参数不是合法 JSON（{parse_error}），未修改文件；请原样重发完整参数。",
            )
        path = str(pick_arg(args, "path", "file_path", "filepath", "filePath", "file") or "")
        old_text = pick_arg(args, "old_text", "old_string", "oldString", "old_str")
        new_text = pick_arg(args, "new_text", "new_string", "newString", "new_str")
        replace_all = bool(pick_arg(args, "replace_all", "replaceAll") or False)
        # plan-20261020-pts：缺参时给出完整写法示例——不做路径推断（用户决定），只让模型一次补对
        if not path:
            return ToolResult(
                ok=False, output="",
                error=(
                    "缺少 path 参数（要编辑的文件路径，相对工作根），未修改任何文件。正确写法："
                    '{"path": "src/app.ts", "old_text": "要替换的原文", "new_text": "替换后的文本"}'
                ),
            )
        if old_text is None or new_text is None:
            return ToolResult(
                ok=False, output="",
                error=(
                    f"缺少 {'old_text' if old_text is None else 'new_text'} 参数，未修改文件。"
                    "本工具的参数名是 path / old_text / new_text（不是 file_path / old_string / new_string）。"
                ),
            )
        # plan-75-332: 不再限制写入落在工作区内（越界写入交给 approval_policy 裁决）
        resolved = resolve_loose(path, ctx.workspace_root)
        if not resolved:
            return ToolResult(ok=False, output="", error=f"路径非法: {path}")
        target = Path(resolved)
        if not target.exists():
            import asyncio

            from app.orchestration.tools.path_hints import missing_path_hint

            hint = await asyncio.to_thread(missing_path_hint, ctx.workspace_root, path, want_dir=False)
            return ToolResult(
                ok=False, output="",
                error=f"文件不存在: {path}（新建文件请用 fs_write）{hint}",
            )
        # plan-20261016-ux（问题 3）：按原编码 / BOM / 换行读写（见 text_edit），匹配支持空白与行号容错
        try:
            tf = read_text_file(target)
        except TextFileError as e:
            return ToolResult(ok=False, output="", error=f"{e}: {path}")
        res = apply_replacement(tf.text, str(old_text), str(new_text), replace_all)
        if not res.ok:
            return ToolResult(ok=False, output="", error=res.error)
        # plan-548-2277：「先读后改」闸门。默认 warn（放行 + 警告），strict 才拒绝——
        # 机械批改时逐段先读代价过高，过严会逼模型绕去 terminal_exec 写文件。
        from app.core.config import settings

        _gate_warning = ""
        _read_verified = True
        _gate_mode = str(getattr(settings, "edit_gate_mode", "warn") or "warn").lower()
        if _gate_mode != "off" and res.start_line > 0:
            _rc = getattr(ctx, "read_cache", None)
            if _rc is not None:
                # plan-549-2281 复盘修复：replace_all 时逐处校验（res.spans 为全部命中
                # 区间），此前只查第一处——其余替换位置未读过时静默放行。
                _gate_spans = list(res.spans) or [(res.start_line, res.end_line)]
                _uncovered = [(s, e) for s, e in _gate_spans if not _rc.covers(target, s, e)]
                if _uncovered:
                    _read_verified = False
                    _s0, _e0 = _uncovered[0]
                    _more = f" 等 {len(_uncovered)} 处" if len(_uncovered) > 1 else ""
                    _hint = (
                        f'fs_read(path="{path}", offset={max(1, _s0 - 20)}, '
                        f"limit={max(40, _e0 - _s0 + 40)})"
                    )
                    # 缓存为空（进程刚重启 / 首轮）时不拒绝，沿用 fs_write 的既有口径
                    if _gate_mode == "strict" and _rc.has_any():
                        return ToolResult(
                            ok=False, output="",
                            error=(
                                f"拒绝修改未读取的区域：第 {_s0}-{_e0} 行{_more}本会话没有读过。"
                                f"请先调用 {_hint} 确认原文后重试（若确认目标正确，可改用 fs_write 整文件重写）。"
                            ),
                            error_code="STALE_READ",
                        )
                    _gate_warning = (
                        f"（提示：{len(_uncovered)} 处替换位置本会话未读取过（首处第 {_s0}-{_e0} 行），"
                        f"本次修改未经读取确认；如需核对请调用 {_hint}）"
                    )
        try:
            write_text_file(target, tf, res.text)
        except OSError as e:
            return ToolResult(ok=False, output="", error=f"写入失败: {e}")
        note = STRATEGY_NOTES.get(res.strategy)
        output = f"已应用 diff 到 {path}"
        if replace_all:
            output += f"（共替换 {res.count} 处）"
        if note:
            output += f"（{note}）"
        if _gate_warning:
            output += _gate_warning
        # plan-1085: data 带回新内容，供 agent_loop 磁盘读取失败时兜底统计/记录（不进模型上下文）
        # plan-20261019-diff: 同时带回写前内容——写前读盘失败时若没有它，整篇会被误算为新增
        _data: dict[str, Any] = {"path": path, "delta": len(str(new_text)) - len(str(old_text))}

        # plan-548-2277：闸门结果带给 agent_loop（写后检查与前端变更卡可据此标注）
        _data["read_verified"] = _read_verified
        _data["edit_lines"] = [res.start_line, res.end_line]
        if len(tf.text) <= _NEW_CONTENT_MAX_CHARS:
            _data["old_content"] = tf.text
        if len(res.text) <= _NEW_CONTENT_MAX_CHARS:
            _data["new_content"] = res.text
        _rc = getattr(ctx, "read_cache", None)
        if _rc is not None:
            # plan-560-2311 D1：这里是**局部**编辑，只能登记刚写过的那些行。
            # 此前调 mark_known 把整文件标成「内容已知」，于是本会话后续对任意行
            # 的编辑/覆盖都被无条件放行，两个门禁（covers / has_read）一起失守。
            _gate_spans_after = list(res.spans) or [(res.start_line, res.end_line)]
            _line_delta = res.text.count("\n") - tf.text.count("\n")
            _rc.record_edit(target, _gate_spans_after, _line_delta)
        return ToolResult(ok=True, output=output, data=_data)

"""v1.0: 多文件结构化编辑工具 — 原子性 multi-file diff + apply。

支持在一次调用中对多个文件执行 search-replace 操作。
原子性保证：全部成功或全部回滚（先验证所有 old_text 存在）。
"""
import json
import logging
from pathlib import Path
from typing import Any

from app.models.providers.tool_args import args_parse_error
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import resolve_loose
from app.orchestration.tools.text_edit import (
    STRATEGY_NOTES,
    TextFile,
    TextFileError,
    apply_replacement,
    encode_text,
    pick_arg,
    read_text_file,
)

logger = logging.getLogger(__name__)

_MAX_EDITS = 20  # 单次最多编辑文件数
# plan-1085: data 带回单文件新内容上限（对齐 agent_loop.MAX_TOOL_OUTPUT_CHARS）
_NEW_CONTENT_MAX_CHARS = 16000

_EDIT_PATH_KEYS = ("path", "file_path", "filepath", "filePath")
_EDIT_OLD_KEYS = ("old_text", "old_string", "oldString", "old_str", "old")
_EDIT_NEW_KEYS = ("new_text", "new_string", "newString", "new_str", "new")


def _coerce_edits(raw: Any) -> list[dict] | None:
    """edits 允许是数组，也允许模型把整个数组作为 JSON 字符串传入。"""
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if isinstance(val, list):
            return [e for e in val if isinstance(e, dict)]
    return None


class MultiFileEditTool(Tool):
    name = "multi_file_edit"
    risk_level = "medium"
    description = (
        "多文件结构化编辑。在一次调用中对多个文件执行 search-replace。\n"
        "原子性：全部成功或全部回滚。\n"
        "每个 edit 都必须写齐 path(文件路径), old_text(要替换的原文), new_text(替换后的文本)——"
        "同一文件的多处修改也要逐项写 path。\n"
        "old_text 必须在文件中唯一匹配（逐字复制原文，含缩进）。new_text 为空字符串表示删除。\n"
        '例: {"edits": [{"path": "src/a.ts", "old_text": "原文", "new_text": "新文本"}]}'
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
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "文件路径(相对工作根)"},
                                    "old_text": {"type": "string", "description": "要替换的原始文本"},
                                    "new_text": {"type": "string", "description": "替换后的文本(空=删除)"},
                                },
                                "required": ["path", "old_text", "new_text"],
                            },
                            "description": "编辑操作列表",
                        },
                    },
                    "required": ["edits"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parse_error = args_parse_error(args)
        if parse_error:
            return ToolResult(
                ok=False, output="",
                error=f"参数不是合法 JSON（{parse_error}），未修改任何文件；请原样重发完整参数。",
            )
        edits = _coerce_edits(args.get("edits"))
        if edits is None:
            return ToolResult(ok=False, output="", error="edits 必须是编辑对象数组")
        if not edits:
            return ToolResult(ok=False, output="", error="edits 列表为空")
        if len(edits) > _MAX_EDITS:
            return ToolResult(ok=False, output="", error=f"单次最多 {_MAX_EDITS} 个编辑")

        # Phase 1: 在内存里按顺序应用全部编辑（不写盘）。
        # plan-20261016-ux（问题 3）：同一文件的多处编辑按顺序叠加校验——旧实现每处都对照「原始内容」
        # 校验，后一处依赖前一处结果时必然报未找到；读写走 text_edit（原编码 / BOM / 换行），
        # 匹配支持空白与行号容错，GBK 文件不再因严格 UTF-8 解码抛出未捕获异常。
        files: dict[Path, tuple[TextFile, str, str]] = {}  # resolved -> (原文件, 当前内容, 显示路径)
        order: list[Path] = []
        notes: list[str] = []
        errors: list[str] = []
        # plan-548-2277：每条编辑的命中区间（路径、显示路径、起始行、结束行），
        # 供 Phase 2 写盘前的「先读后改」校验使用
        gate_spans: list[tuple[Path, str, int, int]] = []
        # plan-560-2311 D3：同一文件的多处编辑在这里是**在内存中间态上顺序叠加**的，
        # apply_replacement 算出的 spans 是中间态行号；而 fs_read 登记在 read_cache 里的
        # 区间是**原始文件**行号。前面某处 edit 增删了行数时，后面 edit 的行号就整体偏移，
        # 门禁会拿偏移后的行号去比对原始区间——本已读过的区域被误拒，真正没读过的反而放行。
        # 因此按文件累计行数增量，把每处命中折算回原始文件行号（见 _to_original_line）。
        line_shifts: dict[Path, list[tuple[int, int]]] = {}  # resolved -> [(中间态起始行, 行增量)]
        # plan-560-2311 D1：写盘后要登记「写后真实区间」，这里按文件记下每处编辑的
        # (该次编辑发生时的命中区间, 该次编辑的行增量)——写盘成功后按顺序回放给
        # read_cache.record_edit，只登记真正改过的行，不再把整文件标成已知。
        edit_spans: dict[Path, list[tuple[list[tuple[int, int]], int]]] = {}

        def _to_original_line(resolved: Path, mid_line: int) -> int:
            """中间态行号 → 原始文件行号。

            只扣掉「起始行落在本次命中之前」的编辑带来的行增量：排在本处之后的编辑
            影响的是文件更后面的行号，不会推动本处的行。plan-560-2311 D3
            """
            delta = sum(d for anchor, d in line_shifts.get(resolved, ()) if anchor <= mid_line)
            return mid_line - delta

        # 模型偶尔漏写某个 edit 的 path（同文件多处编辑时最常见）。本次调用中
        # 只出现过一个文件路径时，缺 path 的 edit 直接复用它，避免整批失败重来。
        known_paths: list[str] = []
        for e in edits:
            p = str(pick_arg(e, *_EDIT_PATH_KEYS) or "").strip()
            if p and p not in known_paths:
                known_paths.append(p)
        sole_path = known_paths[0] if len(known_paths) == 1 else ""
        missing_path = False
        missing_files: list[str] = []

        for i, edit in enumerate(edits):
            path_str = str(pick_arg(edit, *_EDIT_PATH_KEYS) or "").strip()
            old_text = pick_arg(edit, *_EDIT_OLD_KEYS)
            new_text = pick_arg(edit, *_EDIT_NEW_KEYS)
            if new_text is None:
                new_text = edit.get("content")

            if not path_str:
                if sole_path:
                    path_str = sole_path
                else:
                    # plan-20261020-pts：不按最近读写的文件推断路径（用户决定），只在末尾给出正确写法示例
                    errors.append(f"edit[{i}]: 缺少 path")
                    missing_path = True
                    continue
            if not old_text:
                errors.append(f"edit[{i}]: old_text 为空")
                continue

            # plan-75-332: 不再限制写入落在工作区内（越界写入交给 approval_policy 裁决）
            _resolved = resolve_loose(path_str, ctx.workspace_root)
            if not _resolved:
                errors.append(f"edit[{i}]: 路径非法: {path_str}")
                continue
            resolved = Path(_resolved)

            if resolved not in files:
                if not resolved.exists():
                    errors.append(f"edit[{i}]: 文件不存在: {path_str}（新建文件请用 fs_write）")
                    if path_str not in missing_files:
                        missing_files.append(path_str)
                    continue
                try:
                    tf = read_text_file(resolved)
                except TextFileError as e:
                    errors.append(f"edit[{i}]: {e}: {path_str}")
                    continue
                files[resolved] = (tf, tf.text, path_str)
                order.append(resolved)

            tf, current, shown = files[resolved]
            _before_lines = current.count("\n")
            res = apply_replacement(current, str(old_text), str(new_text or ""))
            if not res.ok:
                errors.append(f"edit[{i}]（{path_str}）: {res.error}")
                continue
            files[resolved] = (tf, res.text, shown)
            _delta = res.text.count("\n") - _before_lines
            # plan-548-2277：记录本条编辑命中的行区间（apply_replacement 已带回）。
            # plan-549-2281 复盘修复：replace_all 时 spans 带回全部命中区间，逐处入列校验。
            # plan-560-2311 D3：spans 是中间态行号，必须折算回原始文件行号后再生效——
            # 否则前面某处 edit 增删行数后，后面 edit 的门禁区间整体偏移
            # （fs_read 登记的是原始行号）。两条路径（元信息 spans / 回落到 start-end）
            # 都要折算；折算只扣「锚点在本处之前」的增量。
            _raw_spans = list(res.spans) if res.spans else []
            if not _raw_spans and res.start_line > 0:
                _raw_spans = [(res.start_line, res.end_line)]
            for _gs, _ge in _raw_spans:
                if _gs <= 0:
                    continue
                _os = _to_original_line(resolved, _gs)
                _oe = max(_os, _to_original_line(resolved, _ge))
                if _os <= 0:
                    continue
                gate_spans.append((resolved, shown, _os, _oe))
            if _raw_spans:
                # 中间态区间按顺序回放给 read_cache（它记的也是「当前文件」行号），
                # 每次 record_edit 会先折算旧区间再登记新区间，链式推进即等价于写盘结果
                edit_spans.setdefault(resolved, []).append(
                    ([(s, e) for s, e in _raw_spans if s > 0], _delta)
                )
            if _delta:
                # plan-562-2318 M5：逐处记 anchor，不再「首处命中行 + 全部命中总增量」。
                # 同一份 old→new 的多处命中，每处的行数增量必然相同（都是 new-old 的行数差），
                # 原先只把总增量挂在第一处的行上：后续 edit 落在「第 2 处命中之后、总增量算完之前」
                # 时会被多扣几行，门禁区间整体错位（strict 下误拒正确目标 / 放行未读行）。
                _shifts = line_shifts.setdefault(resolved, [])
                _per_hit = str(new_text or "").count("\n") - str(old_text or "").count("\n")
                _anchors = [s for s, _e in _raw_spans if s > 0]
                if _per_hit and _anchors and _per_hit * len(_anchors) == _delta:
                    for _a in _anchors:
                        _shifts.append((max(1, _a), _per_hit))
                elif _per_hit and _anchors:
                    # spans 被上限截断（命中数超过返回上限）：已知命中逐处记，
                    # 剩余增量挂到最后一处之后——这样「最后一处之后」的累计平移仍然正确。
                    for _a in _anchors:
                        _shifts.append((max(1, _a), _per_hit))
                    _rest = _delta - _per_hit * len(_anchors)
                    if _rest:
                        _shifts.append((max(1, _anchors[-1]), _rest))
                else:
                    # 拿不到逐处信息（无 spans，或 new/old 行数差与总增量不自洽）：
                    # 保守沿用「首处锚点 + 总增量」，宁可少扣也不错扣。
                    _shifts.append((max(1, res.start_line), _delta))
            note = STRATEGY_NOTES.get(res.strategy)
            if note:
                notes.append(f"edit[{i}] {note}")

        if errors:
            tail = ""
            if missing_path:
                tail += (
                    "\n每一项都要写自己的 path（同一文件的多处修改也要逐项写），例如："
                    '{"edits": [{"path": "src/a.ts", "old_text": "原文", "new_text": "新文本"}, '
                    '{"path": "src/a.ts", "old_text": "另一处原文", "new_text": "另一处新文本"}]}'
                )
            if missing_files:
                import asyncio

                from app.orchestration.tools.path_hints import missing_path_hint

                for p in missing_files[:2]:
                    hint = await asyncio.to_thread(missing_path_hint, ctx.workspace_root, p, want_dir=False)
                    if hint:
                        tail += f"\n关于 {p}：{hint.lstrip()}"
            return ToolResult(
                ok=False, output="",
                error="验证失败（未修改任何文件）:\n" + "\n".join(errors) + tail,
            )

        # plan-548-2277：「先读后改」闸门——任一 edit 的目标行区间未读过就提示/拒绝。
        # 默认 warn（放行 + 警告），strict 才拒绝；strict 时一个文件都不写，避免留下半套改动。
        from app.core.config import settings

        _gate_warnings: list[str] = []
        _unverified: list[str] = []
        _gate_mode = str(getattr(settings, "edit_gate_mode", "warn") or "warn").lower()
        if _gate_mode != "off":
            _rc = getattr(ctx, "read_cache", None)
            if _rc is not None:
                for _resolved, _shown_path, _s, _e in gate_spans:
                    if _s <= 0 or _rc.covers(_resolved, _s, _e):
                        continue
                    _unverified.append(_shown_path)
                    _hint = (
                        f'fs_read(path="{_shown_path}", offset={max(1, _s - 20)}, '
                        f"limit={max(40, _e - _s + 40)})"
                    )
                    _gate_warnings.append(
                        f"{_shown_path} 第 {_s}-{_e} 行本会话未读取过（可调用 {_hint} 核对）"
                    )
                if _unverified and _gate_mode == "strict" and _rc.has_any():
                    return ToolResult(
                        ok=False, output="",
                        error=(
                            "拒绝修改未读取的区域（未修改任何文件）：\n"
                            + "\n".join(f"  - {w}" for w in _gate_warnings)
                            + "\n请先 fs_read 确认原文后重试。"
                        ),
                        error_code="STALE_READ",
                    )

        # Phase 2: 原子性写入（先备份原始字节，全部成功或回滚）
        backups: list[tuple[Path, bytes]] = []
        applied: list[str] = []
        new_contents: dict[str, str] = {}  # plan-1085: 各文件写后新内容（统计/回滚兜底数据源）
        old_contents: dict[str, str] = {}  # plan-20261019-diff: 写前内容（写前读盘失败时兜底统计）

        try:
            for resolved in order:
                tf, text, _shown = files[resolved]
                backups.append((resolved, resolved.read_bytes()))
                resolved.write_bytes(encode_text(tf, text))
                # plan-75-332: 越界写入时相对路径算不出来，回退绝对路径（仅影响展示）
                try:
                    _rel = str(resolved.relative_to(ctx.workspace_root))
                except ValueError:
                    _rel = str(resolved)
                applied.append(_rel)
                if len(tf.text) <= _NEW_CONTENT_MAX_CHARS:
                    old_contents[_rel] = tf.text
                if len(text) <= _NEW_CONTENT_MAX_CHARS:
                    new_contents[_rel] = text

        except Exception as e:
            # 回滚所有已写入的文件（按原始字节恢复，编码与换行完全不变）
            for path, data in backups:
                try:
                    path.write_bytes(data)
                except OSError:
                    logger.error("[multi_edit] 回滚失败: %s", path)
            return ToolResult(
                ok=False, output="",
                error=f"写入异常，已全部回滚: {e}",
            )

        # plan-549-2281 复盘修复：写盘成功后要把「这些文件已被自己改过」记进读缓存，
        # 否则随后再用 editor_apply_diff 改同一区域会被 strict 误拒。
        # plan-560-2311 D1：登记口径改为**本次真正写过的行区间**（record_edit），
        # 不再调 mark_known——后者表示「整文件内容已知」，只会由 fs_write 整文件重写产生；
        # 局部编辑用它会让 fs_write 的「先读后写」与 editor 的区间门禁对所有行永久失守。
        # 放在写盘成功之后：回滚路径不登记（文件内容已恢复原样，模型并未真正掌握新内容）。
        _rc_after = getattr(ctx, "read_cache", None)
        if _rc_after is not None:
            for resolved in order:
                for _spans_here, _delta_here in edit_spans.get(resolved, ()):
                    _rc_after.record_edit(resolved, _spans_here, _delta_here)

        output = f"成功编辑 {len(applied)} 个文件:\n" + "\n".join(f"  - {p}" for p in applied)
        if notes:
            output += "\n" + "；".join(notes)
        if _gate_warnings:
            output += "\n【提示】" + "；".join(_gate_warnings)
        return ToolResult(
            ok=True,
            output=output,
            data={"files": applied, "count": len(applied),
                  "new_contents": new_contents, "old_contents": old_contents,
                  # plan-548-2277：闸门结果，供写后检查与变更卡标注
                  "read_verified": not _unverified,
                  "unverified_paths": _unverified},
        )

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
        "每个 edit 包含: path(文件路径), old_text(要替换的原文), new_text(替换后的文本)。\n"
        "old_text 必须在文件中唯一匹配。new_text 为空字符串表示删除。"
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

        # 模型偶尔漏写某个 edit 的 path（同文件多处编辑时最常见）。本次调用中
        # 只出现过一个文件路径时，缺 path 的 edit 直接复用它，避免整批失败重来。
        known_paths: list[str] = []
        for e in edits:
            p = str(pick_arg(e, *_EDIT_PATH_KEYS) or "").strip()
            if p and p not in known_paths:
                known_paths.append(p)
        sole_path = known_paths[0] if len(known_paths) == 1 else ""

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
                    errors.append(
                        f"edit[{i}]: 缺少 path（每个 edit 都要写 path；多个文件时无法推断）"
                    )
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
                    errors.append(f"edit[{i}]: 文件不存在: {path_str}")
                    continue
                try:
                    tf = read_text_file(resolved)
                except TextFileError as e:
                    errors.append(f"edit[{i}]: {e}: {path_str}")
                    continue
                files[resolved] = (tf, tf.text, path_str)
                order.append(resolved)

            tf, current, shown = files[resolved]
            res = apply_replacement(current, str(old_text), str(new_text or ""))
            if not res.ok:
                errors.append(f"edit[{i}]（{path_str}）: {res.error}")
                continue
            files[resolved] = (tf, res.text, shown)
            note = STRATEGY_NOTES.get(res.strategy)
            if note:
                notes.append(f"edit[{i}] {note}")

        if errors:
            return ToolResult(
                ok=False, output="",
                error="验证失败（未修改任何文件）:\n" + "\n".join(errors),
            )

        # Phase 2: 原子性写入（先备份原始字节，全部成功或回滚）
        backups: list[tuple[Path, bytes]] = []
        applied: list[str] = []
        new_contents: dict[str, str] = {}  # plan-1085: 各文件写后新内容（统计/回滚兜底数据源）

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

        output = f"成功编辑 {len(applied)} 个文件:\n" + "\n".join(f"  - {p}" for p in applied)
        if notes:
            output += "\n" + "；".join(notes)
        return ToolResult(
            ok=True,
            output=output,
            data={"files": applied, "count": len(applied), "new_contents": new_contents},
        )

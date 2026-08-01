"""v1.0: 多文件结构化编辑工具 — 原子性 multi-file diff + apply。

支持在一次调用中对多个文件执行 search-replace 操作。
原子性保证：全部成功或全部回滚（先验证所有 old_text 存在）。
"""
import logging
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve, safe_resolve_parent

logger = logging.getLogger(__name__)

_MAX_EDITS = 20  # 单次最多编辑文件数


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
        edits = args.get("edits") or []
        if not edits:
            return ToolResult(ok=False, output="", error="edits 列表为空")
        if len(edits) > _MAX_EDITS:
            return ToolResult(ok=False, output="", error=f"单次最多 {_MAX_EDITS} 个编辑")

        # Phase 1: 验证所有编辑（不写入）
        validated: list[tuple[Path, str, str]] = []
        errors: list[str] = []

        for i, edit in enumerate(edits):
            path_str = edit.get("path", "")
            old_text = edit.get("old_text", "")
            new_text = edit.get("new_text", "")

            if not path_str:
                errors.append(f"edit[{i}]: path 为空")
                continue
            if not old_text:
                errors.append(f"edit[{i}]: old_text 为空")
                continue

            # 路径安全校验
            resolved = safe_resolve(ctx.workspace_root, path_str)
            if resolved is None:
                # 可能是新文件，用 safe_resolve_parent
                resolved = safe_resolve_parent(ctx.workspace_root, path_str)
            if resolved is None:
                errors.append(f"edit[{i}]: 路径越界或非法: {path_str}")
                continue

            # 读取文件验证 old_text 存在
            if not resolved.exists():
                errors.append(f"edit[{i}]: 文件不存在: {path_str}")
                continue

            try:
                content = resolved.read_text(encoding="utf-8")
            except OSError as e:
                errors.append(f"edit[{i}]: 读取失败: {e}")
                continue

            # 验证 old_text 唯一匹配
            count = content.count(old_text)
            if count == 0:
                errors.append(f"edit[{i}]: old_text 在 {path_str} 中未找到")
                continue
            if count > 1:
                errors.append(f"edit[{i}]: old_text 在 {path_str} 中匹配 {count} 次(需唯一)")
                continue

            validated.append((resolved, old_text, new_text))

        if errors:
            return ToolResult(
                ok=False, output="",
                error="验证失败:\n" + "\n".join(errors),
            )

        # Phase 2: 原子性写入（先备份，全部成功或回滚）
        backups: list[tuple[Path, str]] = []
        applied: list[str] = []

        try:
            for resolved, old_text, new_text in validated:
                # 备份原始内容
                original = resolved.read_text(encoding="utf-8")
                backups.append((resolved, original))

                # 执行替换
                new_content = original.replace(old_text, new_text, 1)
                resolved.write_text(new_content, encoding="utf-8")
                applied.append(str(resolved.relative_to(ctx.workspace_root)))

        except Exception as e:
            # 回滚所有已写入的文件
            for path, content in backups:
                try:
                    path.write_text(content, encoding="utf-8")
                except OSError:
                    logger.error("[multi_edit] 回滚失败: %s", path)
            return ToolResult(
                ok=False, output="",
                error=f"写入异常，已全部回滚: {e}",
            )

        return ToolResult(
            ok=True,
            output=f"成功编辑 {len(applied)} 个文件:\n" + "\n".join(f"  - {p}" for p in applied),
            data={"files": applied, "count": len(applied)},
        )

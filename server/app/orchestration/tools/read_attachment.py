"""read_attachment 工具 — 读取用户上传的附件文件。

v14: 附件统一以「文件地址」形式存在 uploads 目录（settings.uploads_dir），
AI 通过本工具按 path（如 `{file_id}/{filename}`）读取内容：
- 图片: 返回 base64 + 元信息（供多模态模型理解，同 view_image）
- 文本/表格/文档: 返回解析后的文本（docx/pdf/xlsx/csv/txt 等）

上传目录在项目工作区之外，fs_read/view_image 的 safe_resolve 不覆盖，
因此必须有专用工具读取。
"""
import base64
from pathlib import Path

from app.core.config import settings
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve
from app.services.doc_parser import extract_plain_text, is_image

_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB
# 单次返回的文本页长（plan-156-620：低于执行层的上下文预算 16000，保证分页结果不会被再次落盘）
_MAX_TEXT_CHARS = 12000


def _find_by_name(root: Path, rel: str) -> Path | None:
    """v15 容错：模型可能传错路径（如把附件猜成 D:\\downloads\\xxx.png 或只给文件名），
    按文件名在 uploads 目录内兜底查找，多个匹配取最新。"""
    name = rel.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."}:
        return None
    try:
        matches = [p for p in root.rglob(name) if p.is_file()]
    except OSError:
        return None
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


class ReadAttachmentTool(Tool):
    name = "read_attachment"
    description = (
        "读取用户上传的附件文件内容。path 参数可直接使用用户消息附件中的"
        "服务器磁盘绝对路径（如 'C:/Users/xx/AppData/Local/chatcoder/uploads/1a2b3c/报告.docx'），"
        "也兼容相对路径（如 '1a2b3c/报告.docx'）。\n"
        "支持 docx / pdf / xlsx / csv / txt / md 等文本类（返回解析文本）"
        "以及 png/jpg/jpeg/gif/webp 图片（返回 base64 与元信息，多模态模型可直接理解）。\n"
        "path 取自用户消息中附件的 path 字段，或对话上下文「用户上传的附件」列表中的路径。\n"
        "也用于读取被保存的长工具输出（工具结果里给出的 path 与 offset）。"
        "内容较长时分段返回：结果末尾提示尚未读完时，按给出的 offset 再次调用，直到读完。"
    )
    risk_level = "low"

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
                            "description": "附件路径——优先使用消息中给出的服务器绝对路径（如 'C:/Users/xx/AppData/Local/chatcoder/uploads/1a2b3c/报告.docx'），相对路径（'1a2b3c/报告.docx'）亦可",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "从第几个字符开始读取（默认 0）；长内容按结果末尾提示的值续读",
                        },
                    },
                    "required": ["path"],
                },
            },
        }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        rel = str(args.get("path", "") or "").strip()
        if not rel:
            return ToolResult(ok=False, output="path is required")

        root = Path(settings.uploads_dir).resolve()
        target = safe_resolve(str(root), rel)
        if target is None or not target.is_file():
            # v15: 路径解析失败/文件不存在时按文件名兜底（模型猜错目录也能读到）
            target = _find_by_name(root, rel)
        if target is None:
            # plan-20261020-pts：不是上传附件而是工作区里的文件（模型常用它读 docx / pdf / xlsx）——按工作区路径解析。
            # 只放行工作区内：本工具不在「读取工作区外文件」的审批判定范围里，放开外部路径等于绕过审批。
            ws_target = safe_resolve(ctx.workspace_root, rel)
            if ws_target is not None and ws_target.is_file():
                target = ws_target
        if target is None:
            import asyncio

            from app.orchestration.tools.path_hints import missing_path_hint

            hint = await asyncio.to_thread(missing_path_hint, ctx.workspace_root, rel, want_dir=False)
            return ToolResult(
                ok=False,
                output=f"附件路径非法或不存在: {rel}（工作区外的文件请用 fs_read 读取）{hint}",
            )
        if not target.is_file():
            return ToolResult(ok=False, output=f"附件不存在: {rel}")

        file_size = target.stat().st_size
        if file_size > _MAX_IMAGE_BYTES and is_image(target.name):
            return ToolResult(
                ok=True,
                output=f"附件过大: {rel} ({file_size / 1024 / 1024:.1f}MB, 图片上限 {_MAX_IMAGE_BYTES // 1024 // 1024}MB)",
                data={"path": rel, "size": file_size},
            )

        try:
            data = target.read_bytes()
        except OSError as e:
            return ToolResult(ok=False, output=f"附件读取失败: {e}")

        if is_image(target.name):
            b64 = base64.b64encode(data).decode("ascii")
            return ToolResult(
                ok=True,
                output=f"附件图片: {rel}\n大小: {file_size} bytes ({file_size / 1024:.1f}KB)\nBase64 length: {len(b64)}",
                data={"path": rel, "filename": target.name, "size": file_size, "base64": b64},
            )

        from app.orchestration.output_paging import SPILL_DIRNAME

        if SPILL_DIRNAME in target.parts:
            # 落盘的长工具输出：按原始字节解码，不经过附件解析的包装与首尾裁剪，
            # 保证续读提示里的 offset 与原文逐字对齐（plan-156-620）
            text = data.decode("utf-8", errors="replace")
        else:
            # AI 主动读附件时表格不限行数（篇幅由下方分段控制），此前只给前 200 行
            text = extract_plain_text(target.name, data, max_rows=None)
        if text is None:
            return ToolResult(ok=False, output=f"无法解析附件内容: {rel}（不支持的类型）")
        # plan-156-620：长内容按 offset 分段返回，不再截掉后半段且无法取回
        from app.orchestration.output_paging import page_of, paging_hint, tool_ref

        try:
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        page = page_of(text, offset, _MAX_TEXT_CHARS)
        hint = paging_hint(page, tool_ref("read_attachment", path=rel, offset="{offset}"))
        return ToolResult(
            ok=True,
            output=f"附件 {target.name} 内容:\n{page.text}{hint}",
            data={"path": rel, "filename": target.name, "size": file_size, "text": page.text,
                  "offset": page.offset, "total": page.total},
        )

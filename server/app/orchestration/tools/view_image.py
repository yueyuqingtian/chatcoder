"""view_image 工具 — 查看图片文件。

参照 codex 的 view_image tool。支持将图片转为 base64 描述供模型理解，
或返回图片元信息（尺寸、格式、文件大小）。
"""
import base64
import os
from pathlib import Path

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.safe_path import safe_resolve_read

_MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4MB
_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


class ViewImageTool(Tool):
    name = "view_image"
    description = "View an image file. Returns image metadata (size, format, dimensions)."
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
                            "description": "Image file path (relative to workspace root, absolute path, or user attachment path)",
                        },
                    },
                    "required": ["path"],
                },
            },
        }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        rel_path = args.get("path", "").strip()
        if not rel_path:
            return ToolResult(ok=False, output="path is required")

        # 附件目录兜底：用户消息附件在工作区外的 uploads 目录，允许只读
        target = safe_resolve_read(ctx.workspace_root, rel_path)
        if target is None:
            return ToolResult(ok=False, output=f"Cannot resolve path: {rel_path}")

        if not target.exists():
            return ToolResult(ok=False, output=f"File not found: {rel_path}")

        if not target.is_file():
            return ToolResult(ok=False, output=f"Not a file: {rel_path}")

        ext = target.suffix.lower()
        if ext not in _ALLOWED_EXTS:
            return ToolResult(ok=False, output=f"Unsupported image format: {ext}")

        file_size = target.stat().st_size
        if file_size > _MAX_IMAGE_BYTES:
            return ToolResult(
                ok=True,
                output=f"Image too large to view: {rel_path} ({file_size / 1024 / 1024:.1f}MB, max {_MAX_IMAGE_BYTES / 1024 / 1024:.0f}MB)",
                data={"path": str(target), "size": file_size, "format": ext},
            )

        # 获取图片格式(Python 3.13 移除了 imghdr，用扩展名兜底)
        fmt = ext.lstrip(".")

        # 获取图片尺寸（尝试用 Pillow，没有则跳过）
        dimensions = None
        try:
            from PIL import Image
            with Image.open(target) as img:
                dimensions = f"{img.width}x{img.height}"
        except ImportError:
            pass
        except Exception:
            pass

        # 返回 base64（供多模态模型理解）
        try:
            with open(target, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            return ToolResult(ok=False, output=f"Failed to read image: {e}")

        info_lines = [
            f"Image: {rel_path}",
            f"Format: {fmt}",
            f"Size: {file_size} bytes ({file_size / 1024:.1f}KB)",
        ]
        if dimensions:
            info_lines.append(f"Dimensions: {dimensions}")
        info_lines.append(f"Base64 length: {len(b64)} chars")

        return ToolResult(
            ok=True,
            output="\n".join(info_lines),
            data={
                "path": str(target),
                "format": fmt,
                "size": file_size,
                "dimensions": dimensions,
                "base64": b64,
            },
        )

"""文件上传 API（v14 附件地址化）。

统一约定：前端所有附件（图片 / docx / pdf / xlsx / 文本等）先经
POST /api/upload 落盘，返回文件的实际地址（path + url）。
- path: 相对 uploads 根目录的路径（如 `{file_id}/{filename}`），
  AI 通过 read_attachment 工具按此路径读取。
- url: 静态访问地址（GET /api/uploads/{file_id}/{filename}），前端预览用。

上传后不再传输 base64 data_url，消息附件统一为 {file_id, filename,
path, url, size, mime_type, type} 结构。
"""
import logging
import mimetypes
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import settings
from app.services.doc_parser import MAX_FILE_SIZE, parse_file

logger = logging.getLogger(__name__)

router = APIRouter()


def uploads_root() -> Path:
    """上传根目录（启动时确保存在）。"""
    root = Path(settings.uploads_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_join(root: Path, rel: str) -> Path | None:
    """将 rel 解析到 root 内，防路径穿越。"""
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
        return target
    except (ValueError, OSError):
        return None


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)) -> dict:
    """上传文件并落盘，返回文件的实际地址。

    返回:
    - file_id: 唯一标识
    - filename: 原始文件名
    - path: 相对 uploads 根目录的路径（read_attachment 工具用）
    - url: 静态访问地址（前端预览用）
    - size / mime_type / type: 元信息
    """
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(413, f"文件过大: {len(data) // 1024}KB, 超过 {MAX_FILE_SIZE // 1024 // 1024}MB 限制")
    filename = (file.filename or "unknown").strip()
    if not filename or filename in {".", ".."}:
        raise HTTPException(400, "非法文件名")

    parsed = parse_file(filename, data)
    if parsed["type"] == "unsupported":
        raise HTTPException(415, parsed.get("content") or f"不支持的文件类型: {Path(filename).suffix}")

    file_id = uuid.uuid4().hex[:16]
    root = uploads_root()
    target = root / file_id / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    mime_type = parsed.get("mime") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    result = {
        "file_id": file_id,
        "filename": filename,
        "path": f"{file_id}/{filename}",
        "url": f"/api/uploads/{file_id}/{quote(filename)}",
        "size": len(data),
        "mime_type": mime_type,
        "type": parsed["type"],  # image / text / spreadsheet / document
    }
    logger.info("[upload] %s (%d bytes, type=%s) → %s", filename, len(data), parsed["type"], result["path"])
    return result


@router.get("/uploads/{file_id}/{filename}")
async def serve_upload(file_id: str, filename: str) -> FileResponse:
    """静态服务上传的文件（前端预览 / 下载）。"""
    root = uploads_root()
    target = _safe_join(root, f"{file_id}/{filename}")
    if target is None or not target.is_file():
        raise HTTPException(404, "文件不存在")
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    # v15: inline 预览（图片/PDF/文本在浏览器直接打开而不是触发下载）
    return FileResponse(target, media_type=mime, filename=target.name,
                        content_disposition_type="inline")

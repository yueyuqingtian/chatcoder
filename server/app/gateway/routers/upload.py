"""文件上传 API：支持图片、CSV/Excel、文本等附件上传与解析。"""
import logging

from fastapi import APIRouter, File, UploadFile

from app.services.doc_parser import parse_file

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)) -> dict:
    """上传文件并自动解析。

    返回:
    - type: image / text / spreadsheet / unsupported
    - content: 解析后的内容(图片为 data URL, 文档为文本)
    - filename: 原始文件名
    - size: 文件字节数
    """
    data = await file.read()
    result = parse_file(file.filename or "unknown", data)
    result["filename"] = file.filename
    result["size"] = len(data)
    logger.info("[upload] %s (%d bytes) → %s", file.filename, len(data), result["type"])
    return result

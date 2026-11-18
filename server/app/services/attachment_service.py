"""上传附件的元信息和有界文件读写。"""
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

from anyio import CapacityLimiter, to_thread

from app.core.config import settings
from app.services.doc_parser import is_document, is_image, is_spreadsheet, is_text

_FILE_ID = re.compile(r"[0-9a-f]{16}")
_attachment_io = CapacityLimiter(4)


async def run_attachment_io(fn, *args):
    return await to_thread.run_sync(fn, *args, limiter=_attachment_io)


def uploads_root() -> Path:
    root = Path(settings.uploads_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def valid_filename(filename: str) -> bool:
    return bool(filename and filename not in {".", ".."} and not any(
        c in filename for c in ("/", "\\", ":", "\x00")
    ))


def describe_upload(file_id: str, filename: str, size: int) -> dict:
    kinds = (("image", is_image), ("spreadsheet", is_spreadsheet),
             ("document", is_document), ("text", is_text))
    return {
        "file_id": file_id,
        "filename": filename,
        "path": f"{file_id}/{filename}",
        "url": f"/api/uploads/{file_id}/{quote(filename)}",
        "size": size,
        "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "type": next((kind for kind, matches in kinds if matches(filename)), "file"),
    }


def resolve_upload(reference: dict) -> dict:
    file_id, filename = reference.get("file_id"), reference.get("filename")
    if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
        raise ValueError("上传文件编号无效")
    if not isinstance(filename, str) or not valid_filename(filename):
        raise ValueError("上传文件名无效")
    root = uploads_root()
    path = root / file_id / filename
    try:
        if path.resolve() == path and path.is_file():
            return describe_upload(file_id, filename, path.stat().st_size)
    except OSError:
        pass
    raise ValueError(f"附件「{filename}」已不可用，请重新上传")

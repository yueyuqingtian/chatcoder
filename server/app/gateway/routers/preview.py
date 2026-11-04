"""plan-20261103-attachment-preview：受控资源入口，每次请求重新校验来源。"""
import mimetypes
import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from app.persistence.database import get_db
from app.persistence.models.message import Message, Session
from app.services import project_service
from app.services.preview_service import safe_project_path, preview_kind, decode_text, list_children, initial_tree, check_office, TEXT_LIMIT
from app.services import spreadsheet_preview

router = APIRouter(prefix="/preview", tags=["preview"])


async def resource(source: str, session_id: int | None = None, project_id: int | None = None,
                   message_id: int | None = None, file_id: str | None = None,
                   path: str = "", relative: str = "", filename: str = "", db: AsyncSession = Depends(get_db)) -> Path:
    # plan-20261103-preview-polish：草稿尚无消息，沿用上传下载的边界，仅开放上传副本。
    if source == "upload":
        if not file_id or not re.fullmatch(r"[0-9a-fA-F]{8,32}",file_id):
            raise HTTPException(400,"非法附件标识")
        if not filename or filename in {".",".."} or any(c in filename for c in ("/","\\",":","\x00")):
            raise HTTPException(400,"非法附件名称")
        if relative: raise HTTPException(403,"未发送附件不支持访问其他关联文件")
        from app.gateway.routers.upload import uploads_root
        target = safe_project_path(uploads_root(),f"{file_id}/{filename}")
        if not target.is_file(): raise HTTPException(404,"上传附件不存在或已被清理")
        return target
    if session_id is None: raise HTTPException(400,"缺少会话标识")
    session = await db.get(Session, session_id)
    if session is None: raise HTTPException(404, "会话不存在")
    if source == "attachment":
        if not file_id: raise HTTPException(400,"缺少附件标识")
        message = await db.get(Message, message_id) if message_id is not None else None
        if message is None or message.session_id != session_id or message.deleted:
            raise HTTPException(404, "消息附件不存在")
        attachments = (message.content or {}).get("attachments", [])
        att = next((a for a in attachments if isinstance(a,dict) and a.get("file_id") == file_id), None)
        if att is None: raise HTTPException(404, "消息附件不存在")
        if relative:
            from urllib.parse import unquote
            name = unquote(relative).replace("\\", "/")
            if "/" in name or name in {".",".."}: raise HTTPException(403, "附件仅支持当前消息中明确关联的资源")
            att = next((a for a in attachments if isinstance(a,dict) and a.get("filename")==name),None)
            if att is None: raise HTTPException(404, "关联附件不存在")
        from app.gateway.routers.upload import uploads_root
        target = safe_project_path(uploads_root(), str(att.get("path", "")))
        if not target.is_file(): raise HTTPException(404, "附件副本已不存在")
        return target
    if source != "project": raise HTTPException(400, "未知资源来源")
    if project_id != session.project_id: raise HTTPException(403, "项目不属于当前会话")
    project = await project_service.get_project(db, project_id)
    if project is None: raise HTTPException(404, "项目不存在")
    root = Path(session.worktree_path or project.path)
    target = safe_project_path(root, path)
    if relative:
        from urllib.parse import unquote
        target = safe_project_path(root, str(target.parent / unquote(relative)))
    if not target.exists(): raise HTTPException(404, "文件或目录不存在")
    return target


@router.get("/resource")
async def resolve_resource(target: Path = Depends(resource)):
    kind = "directory" if target.is_dir() else preview_kind(target)
    result = {"name":target.name, "path":target.as_posix(), "kind":kind, "size":target.stat().st_size if target.is_file() else 0}
    if kind == "text":
        result.update(await read_text(target))
    return result


async def read_text(target: Path):
    def read():
        with target.open("rb") as f: data = f.read(TEXT_LIMIT+4)
        truncated=len(data)>TEXT_LIMIT
        chunk=data[:TEXT_LIMIT]
        return {"content":decode_text(chunk,truncated),"truncated":truncated}
    try: return await run_in_threadpool(read)
    except PermissionError: raise HTTPException(403,"没有权限读取此文件")


@router.get("/text")
async def text(target: Path = Depends(resource)):
    if not target.is_file():raise HTTPException(400,"此路径是目录")
    return await read_text(target)


@router.get("/content")
async def content(download: bool = False, target: Path = Depends(resource)):
    if not target.is_file(): raise HTTPException(400, "此路径是目录")
    if not download and preview_kind(target) == "word": check_office(target)
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    # HTML/SVG 等活动内容只作为下载提供，不能在应用源下运行。
    if target.suffix.lower() in {".html", ".htm", ".svg", ".js"}: download = True
    return FileResponse(target, media_type=mime,filename=target.name,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"X-Content-Type-Options":"nosniff", "Content-Security-Policy":("default-src 'none'" if target.suffix.lower()==".pdf" else "sandbox; default-src 'none'")})


@router.get("/children")
async def children(depth: int = Query(1,ge=1,le=8), target: Path = Depends(resource)):
    if not target.is_dir(): raise HTTPException(400, "此路径不是目录")
    return {"path":target.as_posix(), "children":await run_in_threadpool(initial_tree,target,depth) if depth>1 else await run_in_threadpool(list_children,target)}


@router.get("/workbook")
async def workbook(target: Path = Depends(resource)):
    return await run_in_threadpool(spreadsheet_preview.metadata, target)


@router.get("/region")
async def region(sheet: str, row: int = Query(0,ge=0), col: int = Query(0,ge=0),
                 rows: int = Query(200,ge=1,le=200), cols: int = Query(50,ge=1,le=50), target: Path = Depends(resource)):
    return await run_in_threadpool(spreadsheet_preview.region,target,sheet,row,col,rows,cols)

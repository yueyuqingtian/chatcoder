"""plan-20261106-file-drop：上传不以可解析格式作为准入条件。"""
import io
import zipfile

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.gateway.routers import upload


@pytest.fixture
async def upload_client(tmp_path, monkeypatch):
    monkeypatch.setattr(upload, "uploads_root", lambda: tmp_path)
    app = FastAPI()
    app.include_router(upload.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, tmp_path


def archive_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("目录/说明.txt", "压缩包内容保持原样")
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,data,kind", [
    ("资料 包.ZIP", archive_bytes(), "file"),
    ("资料.7z", b"7z\xbc\xaf\x27\x1c\x00", "file"),
    ("资料.rar", b"Rar!\x1a\x07\x00", "file"),
    ("未知.custom", bytes(range(256)), "file"),
    ("程序.exe", b"MZ\x00\xff", "file"),
    ("README", b"no extension", "text"),
    ("空文件.empty", b"", "file"),
    ("说明.txt", "中文文本".encode(), "text"),
    ("图片.png", b"\x89PNG\x00", "image"),
    ("文档.pdf", b"%PDF-", "document"),
    ("文档.docx", b"PK\x00", "document"),
    ("表格.xlsx", b"PK\x00", "spreadsheet"),
    ("表格.csv", b"a,b\n1,2", "spreadsheet"),
])
async def test_any_format_round_trip(upload_client, filename, data, kind):
    client, root = upload_client
    response = await client.post("/api/upload", files={"file": (filename, data)})
    assert response.status_code == 200, response.text
    attachment = response.json()
    assert attachment["filename"] == filename
    assert attachment["type"] == kind
    assert attachment["size"] == len(data)
    assert attachment["mime_type"]
    assert (root / attachment["path"]).read_bytes() == data
    downloaded = await client.get(attachment["url"])
    assert downloaded.status_code == 200
    assert downloaded.content == data


@pytest.mark.asyncio
async def test_size_limit_still_applies(upload_client, monkeypatch):
    client, root = upload_client
    monkeypatch.setattr(upload, "MAX_FILE_SIZE", 8)
    response = await client.post("/api/upload", files={"file": ("资料.zip", b"123456789")})
    assert response.status_code == 413
    assert not list(root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["..", "../外部.zip", "目录/资料.zip", "C:资料.zip"])
async def test_invalid_filename_is_not_saved(upload_client, filename):
    client, root = upload_client
    response = await client.post("/api/upload", files={"file": (filename, b"data")})
    assert response.status_code == 400
    assert not list(root.iterdir())

"""plan-20261103-attachment-preview：预览独立于 AI 文本提取，保留来源边界。"""
import codecs
import mimetypes
import zipfile
from pathlib import Path
from fastapi import HTTPException

TEXT_LIMIT = 512 * 1024
OFFICE_LIMIT = 10 * 1024 * 1024


def safe_project_path(root: Path, path: str) -> Path:
    root = root.resolve()
    # Windows 的反斜杠在 Linux 测试中也按目录分隔符处理。
    candidate = Path(path.replace("\\", "/"))
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(403, "路径位于当前项目之外")
    return target


def decode_text(data: bytes, truncated: bool = False) -> str:
    encodings = ("utf-16",) if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else ("utf-8-sig", "gb18030")
    if encodings[0] != "utf-16" and (b"\x00" in data or any(b < 8 for b in data)):
        raise HTTPException(415, "该文件为二进制文件，暂不支持文本预览")
    for encoding in encodings:
        try:
            return codecs.getincrementaldecoder(encoding)().decode(data, final=not truncated)
        except UnicodeError:
            pass
    raise HTTPException(415, "无法识别文本编码")


def preview_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx": return "word"
    if ext in {".xlsx", ".xls", ".csv", ".tsv"}: return "spreadsheet"
    if ext == ".pdf": return "pdf"
    if ext in {".doc", ".exe", ".zip", ".7z", ".dll"}: return "unsupported"
    if (mimetypes.guess_type(path.name)[0] or "").startswith("image/"): return "image"
    return "text"


def check_office(path: Path):
    if path.stat().st_size > OFFICE_LIMIT:
        raise HTTPException(413, "文件超过 10MB 预览上限")
    if path.suffix.lower() in {".xlsx", ".docx"}:
        try:
            with zipfile.ZipFile(path) as archive:
                if sum(i.file_size for i in archive.infolist()) > 100 * 1024 * 1024:
                    raise HTTPException(413, "文档解压后过大，无法预览")
        except zipfile.BadZipFile:
            raise HTTPException(422, "文档已损坏或加密，无法预览")


def list_children(path: Path):
    try:
        return [{"name": p.name, "path": p.as_posix(), "type": "dir" if p.is_dir() else "file",
                 "children": [] if p.is_dir() else None}
                for p in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))]
    except PermissionError:
        raise HTTPException(403, "没有权限读取此目录")


def initial_tree(root: Path, depth: int):
    """保留文件过滤的初始多层视图；深层定位仍按需补齐，不扫描整棵树。"""
    remaining = [5000]
    seen = set()
    def walk(path, level):
        if level <= 0 or remaining[0] <= 0: return []
        resolved = path.resolve()
        if resolved in seen: return []
        try: resolved.relative_to(root.resolve())
        except ValueError: return []
        seen.add(resolved)
        nodes = list_children(path)
        result = []
        for node in nodes:
            if node["name"].startswith(".") or node["name"] == "node_modules": continue
            if remaining[0] <= 0: break
            remaining[0] -= 1
            if node["type"] == "dir": node["children"] = walk(Path(node["path"]),level-1)
            result.append(node)
        return result
    return walk(root, depth)

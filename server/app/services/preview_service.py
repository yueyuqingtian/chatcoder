"""plan-20261103-attachment-preview：预览独立于 AI 文本提取，保留来源边界。"""
import codecs
import mimetypes
import zipfile
from collections import deque
from pathlib import Path
from fastapi import HTTPException

TEXT_LIMIT = 512 * 1024
OFFICE_LIMIT = 10 * 1024 * 1024
INITIAL_TREE_NODE_BUDGET = 5000

# plan-771-3177：统一隐藏文件与重型目录过滤口径，list_children 与 initial_tree 共用。
DEFAULT_IGNORED_DIRS = frozenset({
    "node_modules", "__pycache__", ".git", ".venv", "venv",
    "dist", "build", "target", ".next", ".nuxt",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".cache",
})


def is_hidden_entry(name: str, is_dir: bool = False) -> bool:
    """判断目录项在 show_hidden=False 时是否应隐藏。"""
    if name.startswith("."):
        return True
    if is_dir and name in DEFAULT_IGNORED_DIRS:
        return True
    return False


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


def _scan_children(path: Path, show_hidden: bool, limit: int | None = None):
    try:
        out = []
        for p in path.iterdir():
            is_dir = p.is_dir()
            if not show_hidden and is_hidden_entry(p.name, is_dir=is_dir):
                continue
            if limit is not None and len(out) >= limit:
                return None
            out.append({
                "name": p.name,
                "path": p.as_posix(),
                "type": "dir" if is_dir else "file",
                "children": None,
            })
        return sorted(out, key=lambda node: (node["type"] != "dir", node["name"].lower()))
    except PermissionError:
        raise HTTPException(403, "没有权限读取此目录")


def list_children(path: Path, show_hidden: bool = False):
    return _scan_children(path, show_hidden) or []


def initial_tree(root: Path, depth: int, show_hidden: bool = False):
    """plan-20261121-file-tree-settings：只预读能完整返回的分支，根目录始终完整。"""
    root_resolved = root.resolve()
    result = list_children(root, show_hidden=show_hidden) if depth > 0 else []
    remaining = INITIAL_TREE_NODE_BUDGET - len(result)
    seen = {root_resolved}

    def candidates(nodes, level):
        if level > 0:
            for node in nodes:
                if node["type"] == "dir" and node["name"] not in DEFAULT_IGNORED_DIRS:
                    yield node, level

    queue = deque(candidates(result, depth - 1))
    while queue and remaining > 0:
        node, level = queue.popleft()
        path = Path(node["path"])
        try:
            resolved = path.resolve()
            if resolved in seen or not resolved.is_relative_to(root_resolved):
                continue
            children = _scan_children(path, show_hidden, limit=remaining)
        except (OSError, RuntimeError, HTTPException):
            # 子目录消失或无法访问时，仍保留其同级项；手动打开会得到具体错误。
            continue
        seen.add(resolved)
        if children is None:
            continue
        node["children"] = children
        remaining -= len(children)
        queue.extend(candidates(children, level - 1))
    return result

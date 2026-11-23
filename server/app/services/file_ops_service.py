"""项目安全文件增删改操作服务（plan-771-3177）。

所有路径操作均经 `safe_project_path` 校验，禁止越出工作区根目录、禁止操作工作区根目录本身、
禁止触碰 `.git` 元数据内部；兼容 `session.worktree_path`。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.message import Session
from app.services import project_service
from app.services.preview_service import safe_project_path


async def resolve_workspace_root(
    db: AsyncSession,
    project_id: int,
    session_id: int | None = None,
) -> Path:
    """解析操作根目录（优先校验 session.worktree_path 归属）。"""
    project = await project_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    root = Path(project.path)
    if session_id is not None:
        session = await db.get(Session, session_id)
        if session is None:
            raise HTTPException(404, "会话不存在")
        if session.project_id != project_id:
            raise HTTPException(403, "会话不属于当前项目")
        if session.worktree_path:
            root = Path(session.worktree_path)
    resolved = root.resolve()
    if not resolved.is_dir():
        raise HTTPException(404, "项目工作目录不存在")
    return resolved


def _safe_op_path(root: Path, raw_path: str, *, allow_root: bool = False) -> Path:
    """校验项目内路径：防穿越、禁空路径、禁修改 `.git` 内部。"""
    cleaned = (raw_path or "").strip()
    if not cleaned:
        raise HTTPException(400, "路径不能为空")
    if "\x00" in cleaned:
        raise HTTPException(400, "路径包含非法字符")
    target = safe_project_path(root, cleaned)
    root_resolved = root.resolve()
    lexical = Path(os.path.abspath(root_resolved / cleaned.replace("\\", "/")))
    if lexical != target:
        raise HTTPException(403, "不能修改符号链接或通过链接操作文件")
    rel = target.relative_to(root_resolved)
    parts = rel.parts
    # Win32 在调文件 API 前会剥掉每个路径组件尾随的空格与点，`".. "` 在文件系统层等同 `..`；
    # 而 Python 的 resolve/normpath 只识别精确的 `..`，两者不对齐时上面的越界检查会被绕过
    # （如 "sub/.. /.. /outside.txt" 实际落到工作区之外）。这里按 Win32 语义再判一次。
    for part in parts:
        if part.rstrip(" .") in ("", ".", ".."):
            raise HTTPException(403, "路径包含非法字符")
    if not parts:
        if not allow_root:
            raise HTTPException(403, "不允许操作项目根目录本身")
        return target
    if any(part.lower() == ".git" for part in parts):
        raise HTTPException(403, "禁止修改 .git 内部文件或目录")
    return lexical


def _rel_posix(root: Path, target: Path) -> str:
    return target.relative_to(root.resolve()).as_posix()


def _move_to_trash(target: Path) -> bool:
    """尝试将文件或目录移入系统回收站；不可用时返回 False。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", ctypes.c_uint),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            FO_DELETE = 0x0003
            FOF_ALLOWUNDO = 0x0040
            FOF_NOCONFIRMATION = 0x0010
            FOF_NOERRORUI = 0x0400
            FOF_SILENT = 0x0004
            op = SHFILEOPSTRUCTW(
                hwnd=None,
                wFunc=FO_DELETE,
                pFrom=str(target) + "\0\0",
                pTo=None,
                fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT,
                fAnyOperationsAborted=False,
                hNameMappings=None,
                lpszProgressTitle=None,
            )
            rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            return rc == 0 and not op.fAnyOperationsAborted and not target.exists()
        except Exception:  # noqa: BLE001
            return False
    return False


def create_entry(
    root: Path,
    *,
    path: str,
    entry_type: Literal["file", "dir"],
    content: str | None = None,
) -> dict:
    """在项目内创建文件或目录；重名返回 409，父目录缺失时自动创建。"""
    if entry_type not in {"file", "dir"}:
        raise HTTPException(400, "type 仅支持 file 或 dir")
    target = _safe_op_path(root, path)
    if target.exists() or target.is_symlink():
        raise HTTPException(409, f"目标已存在: {_rel_posix(root, target)}")
    try:
        if entry_type == "dir":
            target.mkdir(parents=True, exist_ok=False)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as stream:
                stream.write(content or "")
    except FileExistsError:
        raise HTTPException(409, f"目标已存在: {_rel_posix(root, target)}")
    except PermissionError:
        raise HTTPException(403, "没有权限创建该文件或目录")
    except OSError as exc:
        raise HTTPException(400, f"创建失败: {exc}")
    return {
        "ok": True,
        "name": target.name,
        "path": target.as_posix(),
        "relative_path": _rel_posix(root, target),
        "type": entry_type,
    }


def rename_entry(
    root: Path,
    *,
    src: str,
    dst: str,
) -> dict:
    """重命名或移动项目内文件/目录；源不存在返回 404，目标已存在返回 409。"""
    src_path = _safe_op_path(root, src)
    dst_path = _safe_op_path(root, dst)
    if not src_path.exists() and not src_path.is_symlink():
        raise HTTPException(404, "源文件或目录不存在")
    if str(src_path) == str(dst_path):
        return {
            "ok": True,
            "name": dst_path.name,
            "path": dst_path.as_posix(),
            "relative_path": _rel_posix(root, dst_path),
            "type": "dir" if dst_path.is_dir() else "file",
        }
    # 禁止把目录移动到自身子目录下
    if src_path.is_dir():
        try:
            dst_path.relative_to(src_path)
            raise HTTPException(400, "不能将目录移动到其自身内部")
        except ValueError:
            pass
    # 兼容大小写不敏感文件系统下的仅大小写修改（如 foo.ts -> Foo.ts）
    if dst_path.exists() or dst_path.is_symlink():
        same_entry = os.name == "nt" and src_path == dst_path and src_path.samefile(dst_path)
        if not same_entry:
            raise HTTPException(409, f"目标已存在: {_rel_posix(root, dst_path)}")
    entry_type = "dir" if src_path.is_dir() else "file"
    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        src_path.rename(dst_path)
    except PermissionError:
        raise HTTPException(403, "没有权限重命名或移动目标")
    except OSError as exc:
        raise HTTPException(400, f"重命名失败: {exc}")
    return {
        "ok": True,
        "name": dst_path.name,
        "path": dst_path.as_posix(),
        "relative_path": _rel_posix(root, dst_path),
        "type": entry_type,
    }


def delete_entry(
    root: Path,
    *,
    path: str,
    recursive: bool = False,
    permanent: bool = False,
) -> dict:
    """删除文件或目录。非空目录需显式传入 recursive=True；默认先尝试移入系统回收站。"""
    target = _safe_op_path(root, path)
    if not target.exists() and not target.is_symlink():
        raise HTTPException(404, "文件或目录不存在")
    is_dir = target.is_dir() and not target.is_symlink()
    if is_dir and not recursive:
        try:
            has_items = any(target.iterdir())
        except PermissionError:
            raise HTTPException(403, "没有权限读取该目录")
        if has_items:
            raise HTTPException(400, "目录非空，需传 recursive=true 确认删除")
    trashed = False
    try:
        if not permanent:
            trashed = _move_to_trash(target)
        if target.exists() or target.is_symlink():
            if is_dir:
                if recursive:
                    shutil.rmtree(target)
                else:
                    target.rmdir()
            else:
                target.unlink()
    except PermissionError:
        raise HTTPException(403, "没有权限删除目标")
    except OSError as exc:
        raise HTTPException(400, f"删除失败: {exc}")
    return {
        "ok": True,
        "path": target.as_posix(),
        "relative_path": _rel_posix(root, target),
        "type": "dir" if is_dir else "file",
        "trashed": trashed,
    }


def _next_duplicate_path(src: Path) -> Path:
    """生成不冲突的副本目标路径：name_copy.ext / name_copy_2.ext。"""
    parent = src.parent
    if src.is_dir():
        stem, suffix = src.name, ""
    else:
        stem, suffix = src.stem, src.suffix
    cand = parent / f"{stem}_copy{suffix}"
    if not cand.exists() and not cand.is_symlink():
        return cand
    idx = 2
    while True:
        cand = parent / f"{stem}_copy_{idx}{suffix}"
        if not cand.exists() and not cand.is_symlink():
            return cand
        idx += 1


def duplicate_entry(
    root: Path,
    *,
    path: str,
    dst: str | None = None,
) -> dict:
    """在项目内创建文件或目录副本。"""
    src_path = _safe_op_path(root, path)
    if not src_path.exists():
        raise HTTPException(404, "源文件或目录不存在")
    if dst and dst.strip():
        dst_path = _safe_op_path(root, dst)
        if dst_path.exists() or dst_path.is_symlink():
            raise HTTPException(409, f"目标已存在: {_rel_posix(root, dst_path)}")
    else:
        dst_path = _next_duplicate_path(src_path)
    is_dir = src_path.is_dir()
    if is_dir:
        try:
            dst_path.relative_to(src_path)
            raise HTTPException(400, "不能将目录复制到其自身内部")
        except ValueError:
            pass
    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        if is_dir:
            shutil.copytree(src_path, dst_path, symlinks=True)
        else:
            shutil.copy2(src_path, dst_path)
    except PermissionError:
        raise HTTPException(403, "没有权限复制目标")
    except OSError as exc:
        raise HTTPException(400, f"复制失败: {exc}")
    return {
        "ok": True,
        "name": dst_path.name,
        "path": dst_path.as_posix(),
        "relative_path": _rel_posix(root, dst_path),
        "type": "dir" if is_dir else "file",
    }

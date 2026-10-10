"""工作目录辅助:扫描规范文档候选、浏览本机文件夹、按名称定位拖入的文件夹。"""
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import native_dialog, project_service

router = APIRouter(prefix="/utils", tags=["utils"])

_RULE_NAMES = ("AGENTS.md", ".cursorrules", "CLAUDE.md")


@router.get("/scan-rules-docs", response_model=list[str])
async def scan_rules_docs(path: str = Query(..., description="工作目录路径")) -> list[str]:
    """扫描指定目录(根+一级子目录)下的规范文档,返回相对路径列表。"""
    root = Path(path)
    if not root.is_dir():
        return []
    found: list[str] = []
    dirs = [root]
    try:
        dirs += [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")][:8]
    except OSError:
        pass
    for d in dirs:
        for name in _RULE_NAMES:
            p = d / name
            if p.is_file():
                try:
                    found.append(str(p.relative_to(root)))
                except ValueError:
                    found.append(str(p))
    return found


# ═══════════════════════════════════════════════════════════════════════
# plan-20261009-ui U1：新建项目弹窗的「选择文件夹」
# 桌面版走 Electron 的系统选择器与 webUtils 取拖入路径；浏览器调试环境两者都没有，
# 由后端（与浏览器同机）代为弹出系统自带的文件夹选择窗口并回传绝对路径（pick-directory）；
# 弹不出来的环境（后端不在本机 / 无桌面会话）回落到浏览本机目录（browse-dirs），
# 拖入文件夹时按名称在本机定位（resolve-dir）——弹窗任何环境下都不退化成手输路径。
# ═══════════════════════════════════════════════════════════════════════

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class PickDirectoryBody(BaseModel):
    title: str | None = None
    initial: str | None = None


@router.post("/pick-directory", response_model=dict)
async def pick_directory(request: Request, body: PickDirectoryBody | None = None) -> dict:
    """弹出本机系统自带的「选择文件夹」窗口，等待用户选择后返回绝对路径。

    返回 {path, canceled, unsupported, reason}：用户取消 → path=null, canceled=true；
    当前环境弹不出窗口 → unsupported=true，前端据此回落到自绘的目录浏览器。
    窗口会出现在**后端所在机器**的桌面上，因此只接受本机页面的调用（远程访问时弹出的窗口用户看不到）。
    """
    def unsupported(reason: str) -> dict:
        return {"path": None, "canceled": False, "unsupported": True, "reason": reason}

    client_host = request.client.host if request.client else ""
    origin = request.headers.get("origin")
    origin_host = (urlparse(origin).hostname or "") if origin and origin != "null" else ""
    if client_host not in _LOOPBACK_HOSTS or (origin_host and origin_host not in _LOOPBACK_HOSTS):
        return unsupported("后端不在本机，无法在你的桌面上弹出选择窗口")

    b = body or PickDirectoryBody()
    try:
        path = await native_dialog.pick_directory(title=(b.title or "选择文件夹").strip() or "选择文件夹", initial=b.initial)
    except native_dialog.NativeDialogBusy:
        raise HTTPException(409, "已有一个选择窗口打开，请先在那里完成选择")
    except native_dialog.NativeDialogUnavailable as exc:
        return unsupported(str(exc))
    return {"path": path, "canceled": path is None, "unsupported": False, "reason": ""}

# Windows 下系统保留或回收类目录：浏览时不展示
_SKIP_DIR_NAMES = {"system volume information", "recovery", "config.msi", "documents and settings"}
_BROWSE_CAP = 1000


def _is_hidden_dir(entry: "os.DirEntry[str]") -> bool:
    name = entry.name
    if name.startswith(".") or name.startswith("$"):
        return True
    if sys.platform == "win32":
        if name.lower() in _SKIP_DIR_NAMES:
            return True
        try:
            return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x2)
        except OSError:
            return True
    return False


def _roots() -> list[dict]:
    """文件系统根：Windows 为已挂载盘符（位掩码读取，不触碰光驱 / 网络盘），其余平台为 /。"""
    if sys.platform == "win32":
        try:
            import ctypes

            mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            mask = 0
        drives = [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]
        return [{"name": d.rstrip("\\"), "path": d} for d in drives]
    return [{"name": "/", "path": "/"}]


def _quick_places() -> list[dict]:
    home = Path.home()
    out = []
    for label, p in (("主目录", home), ("桌面", home / "Desktop"), ("文档", home / "Documents"), ("下载", home / "Downloads")):
        try:
            if p.is_dir():
                out.append({"name": label, "path": str(p)})
        except OSError:
            continue
    return out


def _crumbs(p: Path) -> list[dict]:
    parts: list[dict] = []
    cur = p
    while True:
        label = cur.name or str(cur)
        parts.append({"name": label.rstrip("\\/") or str(cur), "path": str(cur)})
        if cur.parent == cur:
            break
        cur = cur.parent
    parts.reverse()
    return parts


@router.get("/browse-dirs", response_model=dict)
async def browse_dirs(
    path: str = Query("", description="要浏览的目录绝对路径；为空 = 根（盘符 / 常用位置）"),
    show_hidden: bool = Query(False, description="是否显示隐藏目录"),
) -> dict:
    """列出目录下的子文件夹（只含文件夹，按名称排序）。"""
    raw = (path or "").strip()
    if not raw:
        return {"path": "", "parent": None, "crumbs": [], "entries": _roots(), "places": _quick_places(), "truncated": False}
    if re.fullmatch(r"[A-Za-z]:", raw):
        raw += "\\"
    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(400, "路径无效")
    if not p.is_dir():
        raise HTTPException(404, "目录不存在或不可访问")

    entries: list[dict] = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if not e.is_dir():
                        continue
                except OSError:
                    continue
                if not show_hidden and _is_hidden_dir(e):
                    continue
                entries.append({"name": e.name, "path": e.path})
    except PermissionError:
        raise HTTPException(403, "没有权限读取该目录")
    except OSError as exc:
        raise HTTPException(400, f"无法读取目录：{exc}")
    entries.sort(key=lambda x: x["name"].lower())
    truncated = len(entries) > _BROWSE_CAP
    parent = "" if p.parent == p else str(p.parent)
    return {
        "path": str(p),
        "parent": parent,
        "crumbs": _crumbs(p),
        "entries": entries[:_BROWSE_CAP],
        "places": [],
        "truncated": truncated,
    }


def _norm_key(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _search_roots(project_paths: list[str]) -> list[Path]:
    """定位优先级：已有项目所在的父目录（用户习惯把项目放在一起）→ 常用位置 → 各盘根。"""
    seen: set[str] = set()
    out: list[Path] = []

    def add(p: Path) -> None:
        try:
            if p.is_dir() and _norm_key(str(p)) not in seen:
                seen.add(_norm_key(str(p)))
                out.append(p)
        except OSError:
            pass

    for pp in project_paths:
        add(Path(pp).parent)
    home = Path.home()
    for sub in ("", "Desktop", "Documents", "Downloads", "source", "work", "code", "projects", "dev"):
        add(home / sub if sub else home)
    for r in _roots():
        add(Path(r["path"]))
    return out


@router.get("/resolve-dir", response_model=dict)
async def resolve_dir(
    name: str = Query(..., description="拖入的文件夹名称"),
    children: str = Query("", description="该文件夹内的子项名称（逗号分隔），用于在同名目录间消歧"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """浏览器无法读出拖入文件夹的绝对路径时，按「名称 + 子项」在本机常见位置定位。

    只在无 Electron 的调试环境用到；检索有时间与数量上限，找不到就如实返回空，由前端引导点击选择。
    """
    target = name.strip().strip("\\/")
    if not target or any(ch in target for ch in "\\/:*?\"<>|"):
        return {"candidates": []}
    wanted = {c.strip().lower() for c in children.split(",") if c.strip()}

    projects = await project_service.list_projects(db, include_archived=True)
    roots = _search_roots([p.path for p in projects])

    deadline = time.monotonic() + 2.5
    visited = 0
    found: dict[str, Path] = {}

    def check(base: Path) -> None:
        cand = base / target
        try:
            if cand.is_dir():
                found.setdefault(_norm_key(str(cand)), cand)
        except OSError:
            pass

    for root in roots:
        if time.monotonic() > deadline or visited > 8000:
            break
        check(root)
        try:
            with os.scandir(root) as it:
                subs = [e for e in it if _safe_is_dir(e) and not _is_hidden_dir(e) and e.name != "node_modules"]
        except OSError:
            continue
        for e in subs:
            visited += 1
            if time.monotonic() > deadline or visited > 8000:
                break
            check(Path(e.path))

    scored: list[tuple[float, Path]] = []
    for cand in found.values():
        score = 1.0
        if wanted:
            try:
                have = {c.name.lower() for c in cand.iterdir()}
            except OSError:
                have = set()
            score = len(wanted & have) / len(wanted)
            if score < 0.6:
                continue
        scored.append((score, cand))
    scored.sort(key=lambda t: (-t[0], len(str(t[1])), str(t[1]).lower()))
    return {"candidates": [{"name": c.name, "path": str(c), "score": round(s, 2)} for s, c in scored[:5]]}


def _safe_is_dir(entry: "os.DirEntry[str]") -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False

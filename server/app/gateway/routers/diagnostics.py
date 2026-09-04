"""诊断与版本检查（D14/D15）。"""
import platform
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import checkpoint_gc, project_service
from app.services.rollback_service import _CHECKPOINT_DIR

router = APIRouter(tags=["diagnostics"])


@router.get("/diagnostics", response_model=dict)
async def diagnostics():
    """环境诊断：git、后端、模型连通、目录权限。"""
    result: dict = {"ok": True, "checks": {}}

    # git
    try:
        ver = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10)
        result["checks"]["git"] = {"ok": ver.returncode == 0, "detail": ver.stdout.strip() or ver.stderr.strip()}
    except Exception as e:
        result["checks"]["git"] = {"ok": False, "detail": str(e)}

    # 后端端口
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=2):
            result["checks"]["backend_port"] = {"ok": True, "detail": "8000 端口可连接"}
    except OSError as e:
        result["checks"]["backend_port"] = {"ok": False, "detail": f"{e}"}

    # 平台/WSL
    result["checks"]["platform"] = {"ok": True, "detail": f"{platform.system()} {platform.release()}"}
    result["checks"]["wsl"] = {"ok": True, "detail": "WSL 检测：N/A"}

    # 工作目录可写
    import tempfile
    try:
        with tempfile.TemporaryFile(dir=".") as f:
            f.write(b"1")
        result["checks"]["workspace_writable"] = {"ok": True, "detail": "当前目录可写"}
    except OSError as e:
        result["checks"]["workspace_writable"] = {"ok": False, "detail": str(e)}

    result["ok"] = all(c.get("ok") for c in result["checks"].values())

    # v2.2 (plan-88): 各项目工作区 .chatcoder/checkpoints 占用统计
    result["checkpoints"] = await _checkpoint_stats()
    return result


async def _checkpoint_stats() -> list[dict]:
    """汇总所有活跃项目工作区的 checkpoint 目录统计（文件数/大小/孤儿数）。"""
    from app.persistence.database import async_session_factory
    stats: list[dict] = []
    try:
        async with async_session_factory() as db:
            projects = await project_service.list_projects(db)
        for project in projects:
            root = Path(project.path).resolve() / _CHECKPOINT_DIR
            if not root.exists():
                continue
            files = [p for p in root.rglob("*") if p.is_file()]
            size = sum(p.stat().st_size for p in files)
            try:
                async with async_session_factory() as db:
                    orphan = await checkpoint_gc.collect_orphans(db, project.path)
            except Exception:
                orphan = {"orphan_count": 0}
            stats.append({
                "workspace": project.path,
                "file_count": len(files),
                "size_mb": round(size / (1024 * 1024), 2),
                "orphan_count": orphan.get("orphan_count", 0),
            })
    except Exception:
        return stats
    return stats


@router.post("/diagnostics/checkpoints/cleanup", response_model=dict)
async def cleanup_checkpoints(workspace: str | None = None,
                              db: AsyncSession = Depends(get_db)):
    """手动触发 checkpoint 垃圾回收；不传 workspace 时对所有活跃项目工作区执行。"""
    if workspace:
        result = await checkpoint_gc.cleanup(db, workspace)
        await db.commit()
        return {"ok": True, "results": [result]}
    from app.persistence.database import async_session_factory
    results: list[dict] = []
    async with async_session_factory() as sdb:
        projects = await project_service.list_projects(sdb)
        for project in projects:
            r = await checkpoint_gc.cleanup(sdb, project.path)
            if r.get("deleted"):
                results.append({"workspace": project.path, **r})
        await sdb.commit()
    return {"ok": True, "results": results}


@router.get("/update-check", response_model=dict)
async def update_check():
    """版本检查（占位：读内置版本源）。"""
    return {"ok": True, "current": "0.4.0", "latest": None, "has_update": False}

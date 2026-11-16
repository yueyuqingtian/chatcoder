"""诊断与版本检查（D14/D15）。"""
import platform
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.persistence.database import get_db
from app.services import checkpoint_gc, project_service
from app.services.rollback_service import _CHECKPOINT_DIR

router = APIRouter(tags=["diagnostics"])


@router.get("/diagnostics/context", response_model=dict)
async def context_structure_snapshot(session_id: int | None = None):
    """plan-20261109-compact-structure：支持导出最近结构取证，不暴露消息正文或凭据。"""
    from app.orchestration.history_sequence import context_diagnostics
    return {"events": context_diagnostics(session_id), "limit": 100}


@router.get("/diagnostics/performance", response_model=dict)
async def performance_snapshot():
    """plan-20261101-performance：按需诊断，不建立常驻采集任务或记录正文。"""
    from app.gateway.ws import manager
    from app.services.message_window import stats
    from app.persistence.write_behind import queue
    return {"websocket": manager.snapshot(), "message_window": dict(stats),
            "write_queue_pending": queue.pending_count}


@router.get("/diagnostics", response_model=dict)
async def diagnostics():
    """环境诊断：git、后端、模型连通、目录权限和数据库实例归属。"""
    result: dict = {"ok": True, "checks": {}}
    from app.persistence.database import database_info
    result["database"] = database_info()

    # git
    try:
        ver = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10)
        result["checks"]["git"] = {"ok": ver.returncode == 0, "detail": ver.stdout.strip() or ver.stderr.strip()}
    except Exception as e:
        result["checks"]["git"] = {"ok": False, "detail": str(e)}

    # 后端端口：探测本进程**实际监听**的端口。
    # 旧实现写死 8000，而默认配置是 12973（见 core/config.py server_port），
    # 因此诊断每次都报「timed out」——服务本身完全正常（用户反馈）。
    import socket
    port = settings.server_port
    host = "127.0.0.1"
    try:
        with socket.create_connection((host, port), timeout=2):
            result["checks"]["backend_port"] = {"ok": True, "detail": f"{host}:{port} 端口可连接"}
    except OSError as e:
        result["checks"]["backend_port"] = {"ok": False, "detail": f"{host}:{port} 连接失败：{e}"}

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
    """汇总所有活跃项目工作区的 checkpoint 目录统计（文件数/大小/孤儿数）。

    plan-20261115: 引用集只算一次。旧实现对每个工作区各跑一遍 collect_orphans，
    每次都把 turn_snapshots 全表遍历一遍（本机 915 条快照 × 5 个工作区），
    诊断接口因此稳定耗时近 4 秒。
    """
    from app.persistence.database import async_session_factory
    stats: list[dict] = []
    try:
        async with async_session_factory() as db:
            projects = await project_service.list_projects(db)
        try:
            async with async_session_factory() as db:
                refs = await checkpoint_gc._referenced_ckpts(db)
        except Exception:
            refs = set()
        for project in projects:
            root = Path(project.path).resolve() / _CHECKPOINT_DIR
            if not root.exists():
                continue
            files = [p for p in root.rglob("*") if p.is_file()]
            size = sum(p.stat().st_size for p in files)
            orphan_count = 0
            if refs:
                key = checkpoint_gc._path_key
                orphan_count = sum(1 for p in files if key(p) not in refs)
            stats.append({
                "workspace": project.path,
                "file_count": len(files),
                "size_mb": round(size / (1024 * 1024), 2),
                "orphan_count": orphan_count,
                "retention_days": settings.checkpoint_retention_days,
            })
    except Exception:
        return stats
    return stats


@router.post("/diagnostics/checkpoints/cleanup", response_model=dict)
async def cleanup_checkpoints(workspace: str | None = None,
                              db: AsyncSession = Depends(get_db)):
    """手动触发 checkpoint 垃圾回收；不传 workspace 时对所有活跃项目工作区执行。

    plan-20261115: 即使没有删除任何文件也必须回传 workspace——前端据此区分
    「清理失败」与「本来就没可删的」。旧实现只在 deleted>0 时才追加结果，
    服务端明明正常执行完毕却返回 results=[]，界面看上去就是「永远清理不完」。
    保留期的文件是设计上不删的，一并回传 kept 让前端能解释原因。
    """
    from app.persistence.database import async_session_factory

    async with async_session_factory() as sdb:
        # 引用集只算一次（所有工作区共用），避免逐工作区全表扫快照
        try:
            refs = await checkpoint_gc._referenced_ckpts(sdb)
        except Exception as exc:
            # plan-20261116-review-fixes：引用未知时不能把有效备份当作孤儿删除。
            raise HTTPException(503, "读取回滚备份引用失败，未执行清理，请稍后重试") from exc
        results: list[dict] = []
        if workspace:
            r = await checkpoint_gc.cleanup(sdb, workspace, refs=refs)
            results.append({"workspace": workspace, **r})
        else:
            projects = await project_service.list_projects(sdb)
            for project in projects:
                root = Path(project.path).resolve() / _CHECKPOINT_DIR
                if not root.exists():
                    continue
                r = await checkpoint_gc.cleanup(sdb, project.path, refs=refs)
                results.append({"workspace": project.path, **r})
        await sdb.commit()
    return {"ok": True, "results": results,
            "retention_days": settings.checkpoint_retention_days}


@router.get("/update-check", response_model=dict)
async def update_check():
    """版本检查（占位：读内置版本源）。"""
    return {"ok": True, "current": "0.4.0", "latest": None, "has_update": False}


# ── plan-230-1144 M3: 符号索引状态与手动重建 ──

async def _fallback_workspace() -> str | None:
    """无显式 workspace 时的兜底：项目列表中排序首项。

    list_projects 按 (pinned desc, updated_at desc) 排序，即置顶/最近更新的项目。
    注意：这只是兜底。前端应始终传当前项目的 workspace——此前诊断页不传参，
    页面展示的是本项目而非用户当前所在项目（用户反馈：诊断里总是别的项目）。
    """
    from app.persistence.database import async_session_factory

    try:
        async with async_session_factory() as db:
            projects = await project_service.list_projects(db)
        if projects:
            return getattr(projects[0], "path", None) or None
    except Exception:  # noqa: BLE001
        return None
    return None


@router.get("/diagnostics/symbol-index", response_model=dict)
async def symbol_index_status(workspace: str | None = None):
    """符号索引状态。

    workspace 缺省时回退到最近使用的项目（仅兜底）；前端应传当前项目路径。
    """
    import asyncio

    from app.services import symbol_index_service as sis

    ws = workspace or await _fallback_workspace()
    if not ws:
        return {"ok": False, "error": "无可用工作区（请传 workspace 或先创建项目）", "available": False}
    stats = await asyncio.to_thread(sis.index_stats, ws)
    return {"ok": True, "workspace": ws, **stats}


@router.post("/diagnostics/symbol-index/rebuild", response_model=dict)
async def symbol_index_rebuild(workspace: str | None = None):
    """手动全量重建符号索引。

    plan-248-1273: 提交到独立 worker 进程并即时返回，
    不再在主服务进程内执行文件扫描与 AST 解析。
    进度通过 WS symbol_index.progress 广播，可在索引库页面查看。
    """
    from app.services import symbol_index_manager as sim

    ws = workspace or await _fallback_workspace()
    if not ws:
        return {"ok": False, "error": "无可用工作区"}

    state = await sim.rebuild(ws)
    if state.get("status") == "error":
        return {"ok": False, "error": state.get("error") or "提交失败", "workspace": ws}
    return {"ok": True, "workspace": ws, "submitted": True,
            "status": state.get("status"), "message": "重建已提交到后台独立进程"}

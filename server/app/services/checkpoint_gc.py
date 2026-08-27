"""checkpoint 垃圾回收（plan-88 任务 D）：治理 .chatcoder/checkpoints 目录膨胀。

清理三类文件：
1. 孤儿：DB 中无任何 TurnSnapshot.file_list 引用的备份（旧平铺残留/回滚清理遗留）；
2. 过期：mtime 超过 checkpoint_retention_days；
3. 超量：文件数超 checkpoint_max_files 或总大小超 checkpoint_max_mb 时按 mtime 从旧删。

删除磁盘文件后同步清理对应 TurnSnapshot.file_list 登记，避免残留引用。
"""
import logging
import os
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.persistence.models.rollback import TurnSnapshot
from app.services.rollback_service import _CHECKPOINT_DIR

logger = logging.getLogger(__name__)


def _path_key(p: Path) -> str:
    s = str(p.resolve())
    return s.lower() if os.name == "nt" else s


def _iter_checkpoint_files(workspace: str):
    root = Path(workspace).resolve() / _CHECKPOINT_DIR
    if not root.exists():
        return
    for p in root.rglob("*"):
        if p.is_file():
            yield p


async def _referenced_ckpts(db: AsyncSession) -> set[str]:
    """DB 中全部快照引用的 checkpoint 绝对路径（归一化，Windows 大小写不敏感）。"""
    res = await db.execute(select(TurnSnapshot))
    refs: set[str] = set()
    for snap in res.scalars().all():
        for item in snap.file_list or []:
            if isinstance(item, dict) and item.get("ckpt"):
                refs.add(_path_key(Path(str(item["ckpt"]))))
    return refs


async def collect_orphans(db: AsyncSession, workspace: str) -> dict:
    """识别孤儿 checkpoint：磁盘存在但 DB 无引用。返回统计（不删除）。"""
    refs = await _referenced_ckpts(db)
    orphan_paths = [p for p in _iter_checkpoint_files(workspace)
                    if _path_key(p) not in refs]
    return {"workspace": workspace, "orphan_count": len(orphan_paths)}


async def cleanup(db: AsyncSession, workspace: str) -> dict:
    """执行清理（孤儿 + 过期 + 超量），返回删除统计。

    workspace 为空或目录不存在时直接返回零统计。
    """
    if not workspace:
        return {"deleted": 0, "orphans": 0, "expired": 0, "overflow": 0}
    refs = await _referenced_ckpts(db)
    root = Path(workspace).resolve() / _CHECKPOINT_DIR
    if not root.exists():
        return {"deleted": 0, "orphans": 0, "expired": 0, "overflow": 0}

    all_files = sorted(_iter_checkpoint_files(workspace), key=lambda p: p.stat().st_mtime)
    deleted: list[Path] = []
    orphan_cnt = expired_cnt = overflow_cnt = 0
    now = time.time()

    for p in all_files:
        if _path_key(p) not in refs:
            orphan_cnt += 1
            deleted.append(p)
        elif (now - p.stat().st_mtime) > settings.checkpoint_retention_days * 86400:
            expired_cnt += 1
            deleted.append(p)

    # 超量：剩余文件超过上限，按 mtime 从旧删（大小超限优先）
    remaining = [p for p in all_files if p not in deleted]
    total_bytes = sum(p.stat().st_size for p in remaining)
    max_bytes = settings.checkpoint_max_mb * 1024 * 1024
    while remaining and (len(remaining) > settings.checkpoint_max_files or total_bytes > max_bytes):
        oldest = remaining.pop(0)
        total_bytes -= oldest.stat().st_size
        deleted.append(oldest)
        overflow_cnt += 1

    if not deleted:
        return {"deleted": 0, "orphans": 0, "expired": 0, "overflow": 0}

    deleted_keys = {_path_key(p) for p in deleted}
    removed_entries = 0
    for p in deleted:
        try:
            p.unlink()
        except OSError:
            pass

    # 同步清理 DB 登记：被删除但仍被快照引用的 {ckpt, path} 条目
    try:
        snap_res = await db.execute(select(TurnSnapshot))
        for snap in snap_res.scalars().all():
            items = list(snap.file_list or [])
            kept: list = []
            changed = False
            for item in items:
                if isinstance(item, dict) and item.get("ckpt") and _path_key(Path(str(item["ckpt"]))) in deleted_keys:
                    changed = True
                    removed_entries += 1
                    continue
                kept.append(item)
            if changed:
                snap.file_list = kept
        await db.flush()
    except Exception:
        logger.debug("[checkpoint_gc] DB 登记清理失败(非阻塞)", exc_info=True)

    logger.info(
        "[checkpoint_gc] %s 清理完成: 删除 %d 个（孤儿 %d / 过期 %d / 超量 %d），同步 DB 登记 %d 条",
        workspace, len(deleted), orphan_cnt, expired_cnt, overflow_cnt, removed_entries,
    )
    return {"deleted": len(deleted), "orphans": orphan_cnt,
            "expired": expired_cnt, "overflow": overflow_cnt}


async def run_cleanup_for_workspace(db: AsyncSession, workspace: str) -> dict:
    """turn 完成后的低频触发入口（engine finally 调用）。失败不抛异常。"""
    try:
        return await cleanup(db, workspace)
    except Exception:
        logger.debug("[checkpoint_gc] GC 执行失败(非阻塞)", exc_info=True)
        return {"deleted": 0, "orphans": 0, "expired": 0, "overflow": 0}

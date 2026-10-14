"""归档内容的永久删除（plan-20261014-fix4）。

设置 → 归档恢复的「批量删除」此前由前端对项目与会话**并发**逐个调用删除接口：
- 项目删除直接 `DELETE FROM projects`，项目下还有会话（归档项目的会话通常仍是活跃状态）即外键失败；
- 会话删除漏清工作流表，用过工作流的会话同样外键失败；
- 任一失败整体中断、列表不刷新，已删掉的会话仍显示且保持选中，再点就报「会话不存在」。

现在一次请求完成整批删除：
1. 解析目标：项目 + 其工作树子项目 + 它们名下的全部会话 + 单独选中的会话；已不存在的 id 计入 missing（幂等）；
2. 先停止这些会话的执行、取消未结束的工作流；
3. 一个写事务内按外键安全顺序清理全部关联行（会话子表见 session_service.purge_session_children）；
4. 提交后清理这些会话独占的附件目录（仍被其它消息引用的保留，例如分支会话复制过去的消息），
   以及回滚快照登记、已无引用的备份文件（仅限工作区 .chatcoder/checkpoints 目录内）；
5. 没有任务在运行时 VACUUM 回收空间（SQLite 删除行不会缩小文件）。

不触碰用户的代码文件：会话 / 工作树的 git 工作树目录、项目内的符号索引都保留，删除只清理应用数据。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import String, cast, select

from app.core.config import settings

logger = logging.getLogger(__name__)

# 空闲页不足该值时不做 VACUUM（耗时与库大小成正比，少量空闲页会被后续写入复用）
MIN_RECLAIM_BYTES = 1 * 1024 * 1024
_FILE_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
_CHUNK = 400


def _chunks(ids: list[int]) -> list[list[int]]:
    return [ids[i:i + _CHUNK] for i in range(0, len(ids), _CHUNK)]


async def _resolve_targets(project_ids: list[int], session_ids: list[int]) -> dict[str, Any]:
    """解析出实际要删除的项目与会话（只读）。"""
    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Session
    from app.persistence.models.project import Project

    want_projects = sorted({int(x) for x in project_ids})
    want_sessions = sorted({int(x) for x in session_ids})
    async with async_session_factory() as db:
        found_projects: set[int] = set()
        for chunk in _chunks(want_projects):
            rows = await db.execute(select(Project.id).where(Project.id.in_(chunk)))
            found_projects.update(int(x) for x in rows.scalars().all())
        # 工作树子项目跟随主项目：主项目删除后它们在侧栏中无处挂靠，会成为删不掉的残留
        all_projects = set(found_projects)
        for chunk in _chunks(sorted(found_projects)):
            rows = await db.execute(select(Project.id).where(Project.parent_project_id.in_(chunk)))
            all_projects.update(int(x) for x in rows.scalars().all())
        all_sessions: set[int] = set()
        for chunk in _chunks(sorted(all_projects)):
            rows = await db.execute(select(Session.id).where(Session.project_id.in_(chunk)))
            all_sessions.update(int(x) for x in rows.scalars().all())
        found_sessions: set[int] = set()
        for chunk in _chunks(want_sessions):
            rows = await db.execute(select(Session.id).where(Session.id.in_(chunk)))
            found_sessions.update(int(x) for x in rows.scalars().all())
    all_sessions |= found_sessions
    return {
        "projects": sorted(all_projects),
        "sessions": sorted(all_sessions),
        "missing_projects": [p for p in want_projects if p not in found_projects],
        "missing_sessions": [s for s in want_sessions if s not in found_sessions and s not in all_sessions],
    }


async def _stop_sessions(session_ids: list[int]) -> None:
    """删除前停止执行：运行中的轮次 / 子代理先取消，未结束的工作流取消（运行时同时停掉节点任务）。"""
    from app.orchestration import engine
    from app.orchestration.workflow import service as workflow_service

    for sid in session_ids:
        try:
            if engine.session_has_live_execution(sid):
                await engine.stop_session_execution(sid)
            await workflow_service.on_session_deleted(sid)
        except Exception:  # noqa: BLE001 — 停止失败不阻断删除（行删除后残留任务写入会失败并记日志）
            logger.warning("[archive] 删除前停止会话执行失败 sid=%s", sid, exc_info=True)


def _attachment_ids(content: Any) -> set[str]:
    out: set[str] = set()
    if not isinstance(content, dict):
        return out
    for att in content.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        fid = str(att.get("file_id") or "").strip()
        if not fid:
            path = str(att.get("path") or "").replace("\\", "/")
            fid = path.split("/", 1)[0] if "/" in path else ""
        if _FILE_ID_RE.match(fid):
            out.add(fid)
    return out


async def _collect_attachments(session_ids: list[int]) -> set[str]:
    """这些会话的消息引用的附件 file_id（附件只由用户消息携带）。"""
    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Message

    found: set[str] = set()
    async with async_session_factory() as db:
        for chunk in _chunks(session_ids):
            rows = await db.execute(
                select(Message.content).where(
                    Message.session_id.in_(chunk), Message.sender_type == "user"
                )
            )
            for content in rows.scalars().all():
                found |= _attachment_ids(content)
    return found


async def _collect_checkpoints(session_ids: list[int]) -> set[str]:
    """这些会话的回滚快照登记的备份文件（工作区 .chatcoder/checkpoints 下，应用生成）。"""
    from app.persistence.database import async_session_factory
    from app.persistence.models.rollback import TurnSnapshot

    found: set[str] = set()
    async with async_session_factory() as db:
        for chunk in _chunks(session_ids):
            rows = await db.execute(
                select(TurnSnapshot.file_list).where(TurnSnapshot.session_id.in_(chunk))
            )
            for file_list in rows.scalars().all():
                for item in file_list or []:
                    if isinstance(item, dict) and item.get("ckpt"):
                        found.add(str(item["ckpt"]))
    return found


def _in_checkpoint_dir(path: Path) -> bool:
    parts = [p.lower() for p in path.parts]
    return any(parts[i] == ".chatcoder" and parts[i + 1] == "checkpoints" for i in range(len(parts) - 1))


async def _remove_orphan_checkpoints(paths: set[str]) -> int:
    """删除已无任何快照引用的备份文件；只动 .chatcoder/checkpoints 目录内的文件。"""
    if not paths:
        return 0
    from app.persistence.database import async_session_factory
    from app.services.checkpoint_gc import _path_key, _referenced_ckpts

    async with async_session_factory() as db:
        refs = await _referenced_ckpts(db)
    removed = 0
    for raw in sorted(paths):
        try:
            target = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if _path_key(target) in refs or not _in_checkpoint_dir(target) or not target.is_file():
            continue
        try:
            target.unlink()
            removed += 1
        except OSError:
            logger.debug("[archive] 删除备份文件失败 %s", target, exc_info=True)
    return removed


def _purge_rows(s, project_ids: list[int], session_ids: list[int]) -> dict[str, int]:
    """写事务内：会话及其全部关联行 → 会话 → 项目级配置 / 记忆 / 数据库连接 → 项目（工作树子项目先删）。"""
    from sqlalchemy import delete as _delete

    from app.persistence.models.config import ConfigProfile
    from app.persistence.models.db_connection import DbConnection, DbPolicy
    from app.persistence.models.memory import MemoryEntry
    from app.persistence.models.message import Session
    from app.persistence.models.project import Project
    from app.services.session_service import purge_session_children

    counts = purge_session_children(s, session_ids)

    def _purge(model, *criteria) -> None:
        res = s.execute(_delete(model).where(*criteria))
        counts[model.__tablename__] = counts.get(model.__tablename__, 0) + int(res.rowcount or 0)

    for chunk in _chunks(session_ids):
        _purge(Session, Session.id.in_(chunk))
    for chunk in _chunks(project_ids):
        # 兜底：目标项目下若有解析之后新建的会话，一并清理，避免项目删除再次撞上外键
        late = [
            int(x)
            for x in s.execute(select(Session.id).where(Session.project_id.in_(chunk))).scalars().all()
        ]
        if late:
            for key, value in purge_session_children(s, late).items():
                counts[key] = counts.get(key, 0) + value
            _purge(Session, Session.id.in_(late))
        _purge(ConfigProfile, ConfigProfile.project_id.in_(chunk))
        _purge(MemoryEntry, MemoryEntry.project_id.in_(chunk), MemoryEntry.scope == "project")
        _purge(DbConnection, DbConnection.project_id.in_(chunk))
        _purge(DbPolicy, DbPolicy.project_id.in_(chunk))
        _purge(Project, Project.id.in_(chunk), Project.parent_project_id.is_not(None))
        _purge(Project, Project.id.in_(chunk))
    s.commit()
    return counts


async def _remove_orphan_uploads(file_ids: set[str]) -> int:
    """删除不再被任何消息引用的附件目录；返回删除的目录数。"""
    if not file_ids:
        return 0
    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Message

    root = Path(settings.uploads_dir).resolve()
    removed = 0
    async with async_session_factory() as db:
        for fid in sorted(file_ids):
            still_used = (
                await db.execute(
                    select(Message.id).where(cast(Message.content, String).like(f"%{fid}%")).limit(1)
                )
            ).first()
            if still_used is not None:
                continue
            target = (root / fid).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                removed += 0 if target.exists() else 1
    return removed


def _sqlite_files() -> list[Path]:
    url = settings.database_url
    if not url.startswith("sqlite"):
        return []
    raw = url.rsplit("///", 1)[-1]
    if not raw or raw == ":memory:":
        return []
    db = Path(raw).resolve()
    return [db, Path(f"{db}-wal")]


def _size_of(files: list[Path]) -> int:
    total = 0
    for f in files:
        try:
            total += os.path.getsize(f)
        except OSError:
            pass
    return total


async def reclaim_space() -> dict[str, Any]:
    """回收数据库空闲页（VACUUM）并截断 WAL。有任务在运行或空闲页很少时跳过。"""
    from app.orchestration import engine
    from app.orchestration.workflow import service as workflow_service
    from app.persistence.database import run_write_locked

    files = _sqlite_files()
    if not files:
        return {"compacted": False, "freed_bytes": 0, "skipped": "unsupported"}
    if engine.any_live_execution() or workflow_service.get_runtime().busy:
        return {"compacted": False, "freed_bytes": 0, "skipped": "busy"}
    before = _size_of(files)

    def _vacuum(s) -> bool:
        bind = s.get_bind()
        # VACUUM 不能在事务内执行：用自动提交连接（同在单写线程内，不与其它写交错）
        with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            free_pages = int(conn.exec_driver_sql("PRAGMA freelist_count").scalar() or 0)
            page_size = int(conn.exec_driver_sql("PRAGMA page_size").scalar() or 4096)
            if free_pages * page_size < MIN_RECLAIM_BYTES:
                return False
            conn.exec_driver_sql("VACUUM")
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            return True

    try:
        compacted = bool(await run_write_locked(_vacuum, label="archive.vacuum"))
    except Exception:  # noqa: BLE001 — 回收失败不影响已完成的删除
        logger.warning("[archive] 回收数据库空间失败（不影响删除结果）", exc_info=True)
        return {"compacted": False, "freed_bytes": 0, "skipped": "error"}
    freed = max(0, before - _size_of(files)) if compacted else 0
    return {"compacted": compacted, "freed_bytes": freed, **({} if compacted else {"skipped": "small"})}


async def purge(*, project_ids: list[int], session_ids: list[int]) -> dict[str, Any]:
    """永久删除项目与会话（含全部关联数据），返回删除统计。"""
    from app.persistence.database import run_write_locked

    targets = await _resolve_targets(project_ids, session_ids)
    projects: list[int] = targets["projects"]
    sessions: list[int] = targets["sessions"]
    result: dict[str, Any] = {
        "ok": True,
        "deleted_projects": len(projects),
        "deleted_sessions": len(sessions),
        "missing": {"projects": targets["missing_projects"], "sessions": targets["missing_sessions"]},
        "files_removed": 0,
        "compacted": False,
        "freed_bytes": 0,
    }
    if not projects and not sessions:
        return result

    await _stop_sessions(sessions)
    attachments = await _collect_attachments(sessions)
    checkpoints = await _collect_checkpoints(sessions)
    counts = await run_write_locked(
        lambda s: _purge_rows(s, projects, sessions), label="archive.purge"
    )
    result["counts"] = {k: v for k, v in (counts or {}).items() if v}
    try:
        result["files_removed"] = await _remove_orphan_uploads(attachments)
        result["files_removed"] += await _remove_orphan_checkpoints(checkpoints)
    except Exception:  # noqa: BLE001 — 文件清理失败不影响已完成的删除
        logger.warning("[archive] 清理附件 / 备份文件失败（不影响删除结果）", exc_info=True)
    result.update(await reclaim_space())
    logger.info(
        "[archive] 永久删除 projects=%d sessions=%d files=%d freed=%d",
        len(projects), len(sessions), result["files_removed"], result["freed_bytes"],
    )
    return result

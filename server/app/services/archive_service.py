"""归档内容的永久删除（plan-20261014-fix4）。

设置 → 归档恢复的「批量删除」此前由前端对项目与会话**并发**逐个调用删除接口：
- 项目删除直接 `DELETE FROM projects`，项目下还有会话（归档项目的会话通常仍是活跃状态）即外键失败；
- 会话删除漏清工作流表，用过工作流的会话同样外键失败；
- 任一失败整体中断、列表不刷新，已删掉的会话仍显示且保持选中，再点就报「会话不存在」。

现在一次请求完成整批删除：
1. 解析目标：项目 + 其工作树子项目 + 它们名下的全部会话 + 单独选中的会话；已不存在的 id 计入 missing（幂等）；
2. 先停止这些会话的执行、取消未结束的工作流；
3. 一个写事务内按外键安全顺序清理全部关联行（会话子表见 session_service.purge_session_children）；
4. 提交后清理这些会话独占的附件目录（仍被其它消息引用的保留，例如分支会话复制过去的消息）、
   回滚快照登记、已无引用的备份文件（仅限工作区 .chatcoder/checkpoints 目录内，删完顺手收口空目录），
   以及该会话产生的计划文档（`{workspace}/ai/chatcoder-plan*.md`，仍被其它 turn 登记的保留）；
5. 没有任务在运行时 VACUUM 回收空间（SQLite 删除行不会缩小文件）。

不触碰用户的代码文件：会话 / 工作树的 git 工作树目录、项目内的符号索引都保留，删除只清理应用数据。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.services import session_service

logger = logging.getLogger(__name__)

# 空闲页不足该值时不做 VACUUM（耗时与库大小成正比，少量空闲页会被后续写入复用）
MIN_RECLAIM_BYTES = 1 * 1024 * 1024
# 库文件超过该值时把 VACUUM 放到后台：大库上 VACUUM 要几十秒，同步做会让删除请求一直挂着。
# 小库（测试库、新库）仍同步回收，保持 compacted / freed_bytes 的原有含义。
VACUUM_BACKGROUND_MIN_BYTES = 64 * 1024 * 1024
_CHUNK = 900
# plan-562-2318 M13：挂靠链遍历的兜底条数上限——只防数据异常导致的死循环，
# 正常业务远达不到；超限只告警，不抛异常打断删除。
MAX_SESSION_FANOUT = 100000


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
        # plan-560-2311 E1：挂靠子会话（side_parent_id 指向这些主会话）必须一并纳入删除集合。
        # 主会话被删后，未归档的历史子会话 side_parent_id 悬空：`list_all_children` 用 inner join
        # 过滤掉它，`list_sessions` 又过滤 side_parent_id IS NULL，界面上既看不到也删不掉，
        # 成为永久孤儿数据。
        # plan-562-2318 M13：原实现是 `for _ in range(5)` 的硬上限，注释却写「循环到不动点」——
        # 挂靠超过 5 层时链尾仍会成为孤儿。改为真正循环到不动点（每轮只查上一轮新增的 id），
        # 另加总条数上限兜底数据异常：超限只告警并继续删除已解析出的集合，不抛异常。
        while True:
            newly: set[int] = set()
            for chunk in _chunks(sorted(all_sessions)):
                rows = await db.execute(select(Session.id).where(Session.side_parent_id.in_(chunk)))
                newly.update(int(x) for x in rows.scalars().all())
            newly -= all_sessions
            if not newly:
                break
            all_sessions |= newly
            if len(all_sessions) > MAX_SESSION_FANOUT:
                logger.warning(
                    "[archive] 挂靠会话链超过上限 %d，仅删除已解析出的 %d 个会话",
                    MAX_SESSION_FANOUT, len(all_sessions),
                )
                break
    return {
        "projects": sorted(all_projects),
        "sessions": sorted(all_sessions),
        "missing_projects": [p for p in want_projects if p not in found_projects],
        "missing_sessions": [s for s in want_sessions if s not in found_sessions and s not in all_sessions],
    }



def _purge_rows(s, project_ids: list[int], session_ids: list[int]) -> dict[str, Any]:

    """写事务内：会话及其全部关联行 → 会话 → 项目级配置 / 记忆 / 数据库连接 → 项目（工作树子项目先删）。

    返回 `{"counts": 各表删除行数, "sessions": 实际删掉的会话 id}`：后者供事务外清理文件系统
    资源（工具输出落盘目录），写事务内不做磁盘 IO。
    """
    from sqlalchemy import delete as _delete

    from app.persistence.models.config import ConfigProfile
    from app.persistence.models.db_connection import DbConnection, DbPolicy
    from app.persistence.models.memory import MemoryEntry
    from app.persistence.models.message import Session
    from app.persistence.models.project import Project
    from app.services.session_service import purge_session_children

    counts = purge_session_children(s, session_ids)
    purged_sessions: set[int] = {int(x) for x in session_ids}

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
            purged_sessions.update(int(x) for x in late)
            _purge(Session, Session.id.in_(late))
        _purge(ConfigProfile, ConfigProfile.project_id.in_(chunk))
        _purge(MemoryEntry, MemoryEntry.project_id.in_(chunk), MemoryEntry.scope == "project")
        _purge(DbConnection, DbConnection.project_id.in_(chunk))
        _purge(DbPolicy, DbPolicy.project_id.in_(chunk))
        _purge(Project, Project.id.in_(chunk), Project.parent_project_id.is_not(None))
        _purge(Project, Project.id.in_(chunk))
    s.commit()
    return {"counts": counts, "sessions": sorted(purged_sessions)}


async def _remove_tool_outputs(session_ids: list[int]) -> int:
    """清理这些会话的长工具输出落盘目录（`uploads/_tool_outputs/{sid}`），返回删除的目录数。

    plan-560-2311 E1：`_purge_rows` 绕开 `delete_session_permanent` 直接调
    `purge_session_children`，漏掉了工具输出目录清理，磁盘上按已删会话 id 一直累积
    （每次超预算输出最多 2MB）。这里统一走 session_service 的公开入口。

    性能：原先逐会话 `await purge_session_tool_outputs(sid)`，而那个入口每调一次都要
    做一次 fork_info 全表查 + 一次会话全表查 + 一次目录遍历；批量删 N 个会话就是
    3N 次扫描/遍历。改用批量入口：整批只查一次、只列一次目录，删除本身并发执行。
    """
    if not session_ids:
        return 0
    from app.services.session_service import purge_session_tool_outputs_many

    return await purge_session_tool_outputs_many(session_ids)


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


async def reclaim_space(*, allow_background: bool = True) -> dict[str, Any]:
    """回收数据库空闲页（VACUUM）并截断 WAL。有任务在运行、空闲页很少或大库转后台时跳过。

    大库（> VACUUM_BACKGROUND_MIN_BYTES）上 VACUUM 要扫全库，耗时几十秒且期间写队列被占；
    删除请求不应该为了回收空间一直挂着。这类库改为**调度后台任务**，本次返回
    `skipped="scheduled"`、`compacted=False`、`freed_bytes=0`——字段名与含义不变，
    只是“这次没有同步回收”；小库仍同步回收，`compacted` / `freed_bytes` 照旧有值。
    """
    from app.orchestration import engine
    from app.orchestration.workflow import service as workflow_service
    from app.persistence.database import run_write_locked

    files = _sqlite_files()
    if not files:
        return {"compacted": False, "freed_bytes": 0, "skipped": "unsupported"}
    if engine.any_live_execution() or workflow_service.get_runtime().busy:
        return {"compacted": False, "freed_bytes": 0, "skipped": "busy"}
    if allow_background and _size_of(files) >= VACUUM_BACKGROUND_MIN_BYTES:
        if _schedule_background_reclaim():
            return {"compacted": False, "freed_bytes": 0, "skipped": "scheduled"}
        # 无运行中的事件循环（同步调用 / 关停中）：退回同步回收，行为不变
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


def _schedule_background_reclaim() -> bool:
    """把空间回收排到后台执行；无事件循环时返回 False（调用方退回同步）。"""
    from app.core.background import spawn_background

    async def _job() -> None:
        # 后台任务里不能再把自己排成后台（allow_background=False），否则大库会永远“已调度”。
        try:
            result = await reclaim_space(allow_background=False)
            logger.info("[archive] 后台空间回收完成 %s", result)
        except Exception:  # noqa: BLE001 — 后台任务不能把异常漏给事件循环
            logger.warning("[archive] 后台空间回收失败（不影响删除结果）", exc_info=True)

    try:
        spawn_background(_job(), name="archive.vacuum")
        return True
    except RuntimeError:  # 无运行中的事件循环
        return False


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
        "tool_output_dirs_removed": 0,
    }
    if not projects and not sessions:
        return result

    await session_service.prepare_session_removal(sessions)
    # 三个收集都是纯读且互不依赖，且必须在写事务之前完成（行被删后就查不到了）。
    # 串行 await 白白多等一次 DB 往返，这里并发拿。
    attachments, checkpoints, plan_docs = await asyncio.gather(
        session_service.collect_session_attachments(sessions),
        session_service.collect_session_checkpoints(sessions),
        session_service.collect_session_plan_docs(sessions),
    )
    purged = await run_write_locked(
        lambda s: _purge_rows(s, projects, sessions), label="archive.purge"
    )
    counts = (purged or {}).get("counts") or {}
    result["counts"] = {k: v for k, v in counts.items() if v}
    # 附件目录 / 备份文件 / 计划文档互不依赖，并发清理（各自内部也有自己的兜底）。
    files_removed, checkpoint_files_removed, plan_docs_removed = await asyncio.gather(
        session_service.remove_orphan_uploads(attachments),
        session_service.remove_orphan_checkpoints(checkpoints),
        session_service.remove_orphan_plan_docs(plan_docs),
        return_exceptions=True,
    )
    if any(
        isinstance(x, BaseException)
        for x in (files_removed, checkpoint_files_removed, plan_docs_removed)
    ):
        # plan-20261014-fix4：文件清理失败不影响已完成的删除，只记日志（口径与原实现一致）
        logger.warning(
            "[archive] 清理附件 / 备份文件 / 计划文档失败（不影响删除结果）",
            exc_info=next(
                (x for x in (files_removed, checkpoint_files_removed, plan_docs_removed)
                 if isinstance(x, BaseException)),
                None,
            ),
        )
    # files_removed 口径保持「清理掉的附件目录 + 备份文件 + 计划文档数」，失败的那一段按 0 计
    result["files_removed"] = sum(
        int(x) for x in (files_removed, checkpoint_files_removed, plan_docs_removed)
        if isinstance(x, int)
    )

    # plan-560-2311 E1：工具输出落盘目录是文件系统资源，必须在写事务外清理
    # （写事务内不做磁盘 IO，且会话行删除已提交，文件清理失败也不影响删除结果）。
    # 单独计数与单独兜底：它删的是目录而不是文件，口径与 files_removed 不同；
    # 与附件清理分开 try，避免前者异常连带跳过这里的清理。
    try:
        result["tool_output_dirs_removed"] = await _remove_tool_outputs((purged or {}).get("sessions") or [])
    except Exception:  # noqa: BLE001
        logger.warning("[archive] 清理工具输出目录失败（不影响删除结果）", exc_info=True)
    result.update(await reclaim_space())
    logger.info(
        "[archive] 永久删除 projects=%d sessions=%d files=%d freed=%d",
        len(projects), len(sessions), result["files_removed"], result["freed_bytes"],
    )
    return result

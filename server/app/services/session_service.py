"""会话 CRUD（v2：项目下多会话，支持 fork/重命名/置顶/归档）。

plan-206-975：写操作经 WriteEngine 单写线程（无锁单写者）；读保留 async 会话。
写函数返回标量（id/title/status 等），不返回跨层 ORM 对象。
"""
import logging
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.message import Message, Session
from app.persistence.models.turn import Turn
from app.persistence.models.workflow import WorkflowRun

logger = logging.getLogger(__name__)

# 自动标题的回落截断长度（AI 归纳失败时用首条消息前若干字）
AUTO_TITLE_FALLBACK_CHARS = 30


def _legacy_default_for_type(type_name: str) -> object:
    """按列类型推断兜底默认值（仅为兼容旧库遗留 NOT NULL 列的显式插入路径）。"""
    t = (type_name or "").upper()
    if any(k in t for k in ("INT", "BOOL", "REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL")):
        return 0
    return ""


async def create_session(db: AsyncSession, *, project_id: int, title: str | None = None,
                         model_id: int | None = None, fork_parent_id: int | None = None,
                         permission_mode: str | None = None,
                         approval_mode: str | None = None,
                         goal_text: str | None = None,
                         side_parent_id: int | None = None,
                         side_kind: str | None = None,
                         title_source: str = "auto") -> int:
    """创建会话。

    plan-156-619 S8：新增 `title_source`，用于区分三种标题来源：
      · `auto`  —— 前端建会话时附带的占位标题（首条消息截断），**允许 AI 归纳覆盖**；
      · `named` —— 正式给定的标题（子会话任务名 / fork 分支名），不再归纳；
      · `manual`—— 用户手改的标题，永不覆盖。
    此前没有这个区分，`auto_title_session` 只看「title 是否为空」——
    而前端创建时总会带一个截断标题，于是 AI 归纳永远不会被触发（真实缺陷）。
    """
    from datetime import datetime, timezone

    from app.orchestration.approval_policy import (
        normalize_approval_mode, normalize_execution_mode,
    )
    from app.persistence.database import run_write_locked

    def patch(s):
        # plan-278-1391: 旧库可能残留「模型已删除但 NOT NULL 无默认值」的列
        # （典型：sessions.plan_restore_after_turn）。此类列不在 ORM 映射中，
        # SQLAlchemy 生成的 INSERT 不带它 → NOT NULL 直接失败。启动迁移多数情况下
        # 已删列/补默认值，这里作为最后一道保险：显式列出遗留列并填默认值。
        from app.persistence.migrations import legacy_notnull_defaults
        legacy = legacy_notnull_defaults("sessions")
        goal_active = bool(goal_text and goal_text.strip())
        # 只登记「明确有值」的字段；其余交给下方补齐逻辑（NOT NULL 列填默认值，
        # 可空列保持 NULL）。避免 goal_status 等 NOT NULL 列被显式写成 NULL。
        values: dict[str, object] = {
            "project_id": project_id,
            # plan-547: 首页所选模式随创建落库，会话输入框立即显示与实际运行一致
            # plan-75-332: 存的是执行模式（readonly / plan / agent）。
            "permission_mode": normalize_execution_mode(permission_mode),
            # plan-75-332: 权限模式（ask / auto / full）随创建一次落准
            "approval_mode": normalize_approval_mode(approval_mode),
        }
        for name, val in (
            ("title", title),
            # plan-156-619 S8：标题来源随会话一起落库（auto / named / manual）
            ("title_source", title_source),
            ("model_id", model_id),
            ("fork_parent_id", fork_parent_id),
            ("side_parent_id", side_parent_id),
            # plan-156-620：挂靠会话的种类（side 侧边对话 / sub 执行子会话）
            ("side_kind", side_kind if side_parent_id is not None else None),
            # plan-676: 首页目标随创建一次落准（对齐 set_goal 端点写法；空时维持默认 none）
            ("goal_text", goal_text.strip()[:2000] if goal_active else None),
            ("goal_status", "active" if goal_active else None),
            ("goal_created_at", datetime.now(timezone.utc).isoformat() if goal_active else None),
        ):
            if val is not None:
                values[name] = val
        if legacy:
            # 显式插入（含遗留列），绕开 ORM 的 INSERT 列裁剪。
            # 关键：显式 INSERT 不再经过 ORM 的 Python 侧默认值填充，必须自行补齐
            # 所有「NOT NULL 且无 server_default」的映射列（status/pinned/
            # goal_status/last_prompt_tokens/goal_turns_used 等），否则会以 NULL 失败。
            values.update(legacy)
            for col in Session.__table__.columns:
                name = col.name
                if name in values or col.server_default is not None or col.nullable:
                    continue
                # 主键/自增列必须交由数据库生成，不能显式填默认值
                if col.primary_key or col.autoincrement is True:
                    continue
                default = col.default
                if default is not None:
                    arg = getattr(default, "arg", None)
                    values[name] = arg() if callable(arg) else arg
                else:
                    values[name] = _legacy_default_for_type(str(col.type))
            cols = list(values.keys())
            sql = (
                f"INSERT INTO sessions ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)})"
            )
            bind = s.get_bind()
            dialect = bind.dialect.name if bind is not None else "sqlite"
            if dialect.startswith("sqlite"):
                cur = s.execute(text(sql), values)
                sid = int(cur.lastrowid)
            else:
                sid = int(s.execute(text(sql + " RETURNING id"), values).fetchone()[0])
            s.commit()
            return sid

        session = Session(
            project_id=project_id, title=title or None,
            title_source=title_source,
            model_id=model_id, fork_parent_id=fork_parent_id,
            side_parent_id=side_parent_id,
            side_kind=side_kind if side_parent_id is not None else None,
            # plan-75-332 R3 修复：此处必须显式写入两种模式——此前漏传 approval_mode，
            # 新建会话一律落到 ORM 默认值 "ask"，表现为「首页选完全访问，发出后变询问审批」；
            # permission_mode 一并改用归一化值（原本写的 "default" 是已废弃的旧名）。
            permission_mode=normalize_execution_mode(permission_mode),
            approval_mode=normalize_approval_mode(approval_mode),
            **(
                {
                    "goal_text": goal_text.strip()[:2000],
                    "goal_status": "active",
                    "goal_created_at": datetime.now(timezone.utc).isoformat(),
                }
                if goal_active else {}
            ),
        )
        s.add(session)
        s.flush()
        sid = session.id
        s.commit()
        return sid

    return await run_write_locked(patch, label="session.create")


async def get_session(db: AsyncSession, session_id: int) -> Session | None:
    return await db.get(Session, session_id)


async def auto_title_session(db: AsyncSession, session: Session, first_text: str) -> str | None:
    """为尚未正式命名的会话生成标题（写引擎单写线程）。

    plan-156-619 S8：从「截首条消息前 30 字」改为「一次轻量 AI 归纳」。
    标题来源三态（`title_source`）驱动是否允许覆盖：

      · `auto`   —— 前端建会话附带的**占位标题**（首条消息截断）。允许归纳覆盖，
                    归纳成功后写回 `named`，避免以后每轮重复调模型；
      · `named`  —— 已是正式标题（AI 归纳结果 / 子会话任务名 / fork 分支名），跳过；
      · `manual` —— 用户手改的标题，永不覆盖。

    此前只有「title 是否为空」一个判据，而前端创建时总会带一个截断标题，
    于是自动命名永远不会触发（真实缺陷）。
    """
    fallback = _auto_title_fallback(session, first_text)
    if fallback is None:
        return None

    # 归纳：失败即回落（不抛异常、不阻塞发送）
    from app.services.title_service import summarize_title

    title = await summarize_title(
        db, first_text=first_text, model_id=getattr(session, "model_id", None),
    ) or fallback

    # 写回 named：下一轮不再重复归纳（模型调用有成本，且同一会话标题不该反复变）。
    # plan-156-620：归纳在后台进行，期间用户可能已手动改名——条件写，只覆盖仍是 auto 的标题。
    if not await _patch_title_if_auto(session.id, title):
        return None
    return title


async def _patch_title_if_auto(session_id: int, title: str) -> bool:
    """仅当会话标题来源仍是 auto 时写入归纳标题（单写线程内比较并设置）。"""
    from app.persistence.database import run_write_locked

    def _do(s) -> bool:
        row = s.get(Session, session_id)
        if row is None or (row.title_source or "auto") != "auto":
            return False
        row.title = title
        row.title_source = "named"
        s.commit()
        return True

    return bool(await run_write_locked(_do, label="session.auto_title"))


def _auto_title_fallback(session, first_text: str) -> str | None:
    """判定会话是否需要自动归纳标题；需要时返回截断回落标题，否则返回 None。"""
    source = getattr(session, "title_source", "auto") or "auto"
    if source != "auto":
        return None

    normalized_text = (first_text or "").strip().replace("\n", " ")
    fallback = normalized_text[:AUTO_TITLE_FALLBACK_CHARS]
    if not fallback:
        return None

    # 只处理「占位标题」：空标题，或标题是首条消息的前缀截断（前端建会话时带的占位）。
    # 存量会话迁移后 title_source 都是 auto，但它们已有正式标题（旧逻辑生成）——
    # 若不加这一判定，用户下次发消息会把所有历史会话标题一起改掉。
    # plan-156-620：前端占位截断长度与这里不同，按「是首条消息前缀」判定，而不是要求完全相等。
    existing = (session.title or "").strip().rstrip("…").rstrip()
    if existing and not normalized_text.startswith(existing):
        return None
    return fallback


def schedule_auto_title(session, first_text: str) -> bool:
    """后台归纳会话标题，完成后广播 session.updated；返回是否已调度。

    plan-156-620：此前发送接口里同步 `await` 归纳（模型调用，超时上限 8 秒），
    首条消息的发送请求因此被拖慢数秒——与 title_service「不阻塞」的设计约束相反。
    这里先在请求内做零成本的资格判定，真正的模型调用放到后台任务。
    """
    from types import SimpleNamespace

    from app.core.background import spawn_background

    if _auto_title_fallback(session, first_text) is None:
        return False
    # 只带标量进后台任务：请求结束后 ORM 实例随会话关闭，不能跨任务访问
    snapshot = SimpleNamespace(
        id=session.id, title=session.title,
        title_source=getattr(session, "title_source", "auto"),
        model_id=getattr(session, "model_id", None),
    )

    async def _run() -> None:
        from app.orchestration.agent_events import broadcast
        from app.persistence.database import async_session_factory

        try:
            async with async_session_factory() as db:
                title = await auto_title_session(db, snapshot, first_text)
            if title:
                await broadcast(snapshot.id, {
                    "event": "session.updated",
                    "payload": {"session_id": snapshot.id, "title": title},
                })
        except Exception:  # noqa: BLE001 — 命名失败不影响任何业务
            logger.warning("[title] 后台归纳失败 sid=%s", snapshot.id, exc_info=True)

    try:
        spawn_background(_run(), name=f"auto-title-{snapshot.id}")
    except RuntimeError:
        return False
    return True


async def patch_session(session_id: int, **fields) -> None:
    """写引擎单写线程提交 Session 字段补丁（无锁单写者；None 跳过）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        row = s.get(Session, session_id)
        if row is None:
            return
        for k, v in fields.items():
            if v is not None:
                setattr(row, k, v)
        s.commit()

    await run_write_locked(patch, label=f"session.patch.{session_id}")


async def list_sessions(db: AsyncSession, project_id: int | None = None,
                        include_archived: bool = False) -> list[Session]:
    stmt = select(Session)
    if project_id is not None:
        stmt = stmt.where(Session.project_id == project_id)
    if not include_archived:
        stmt = stmt.where(Session.status != "archived")
    # 侧边会话（子会话）不进入会话列表：只能从所属主会话的侧边面板访问
    stmt = stmt.where(Session.side_parent_id.is_(None))
    res = await db.execute(stmt.order_by(Session.pinned.desc(), Session.pinned_at.desc(), Session.updated_at.desc()))
    return list(res.scalars().all())


async def list_side_sessions(db: AsyncSession, parent_id: int) -> list[Session]:
    """某主会话下的侧边对话（右侧面板，按创建先后）。归档的同样返回，由前端决定如何展示。

    plan-156-620：只返回 side 种类——执行子会话在左栏展示，若在这里一并返回，
    右侧面板会自动选中最新一条并接管它的执行流（用户看到「子会话跑到了右侧」）。
    """
    from app.core.enums import SideKind

    res = await db.execute(
        select(Session)
        # side_kind 为空 = 字段上线前的存量行且回填未成功：按侧边对话兜底，宁可留在右侧也不凭空消失
        .where(
            Session.side_parent_id == parent_id,
            (Session.side_kind == SideKind.SIDE.value) | Session.side_kind.is_(None),
        )
        .order_by(Session.id)
    )
    return list(res.scalars().all())


async def update_session(db: AsyncSession, session_id: int, **kwargs) -> str | None:
    """更新会话字段（写引擎单写线程）。返回 None（无对象）/ 任意标量（成功）。

    v7: pinned 状态变化时同步维护 pinned_at——置顶写当前时间（"后置顶在上"依据），
    取消置顶清空。仅在 pinned 有值且与旧值不同时才触碰（避免无关更新刷新置顶序）。
    """
    from datetime import datetime, timezone
    from app.persistence.database import run_write_locked

    def patch(s):
        session = s.get(Session, session_id)
        if session is None:
            return None
        new_pinned = kwargs.get("pinned")
        if new_pinned is not None and new_pinned != session.pinned:
            if new_pinned:
                session.pinned_at = datetime.now(timezone.utc).isoformat()
            else:
                session.pinned_at = None
        for k, v in kwargs.items():
            if v is not None:
                setattr(session, k, v)
        s.commit()
        return session.status

    return await run_write_locked(patch, label=f"session.update.{session_id}")


def purge_session_children(s, session_ids: list[int]) -> dict:
    """在给定写事务内**按外键安全顺序**清理这些会话的全部关联数据。

    plan-308-1542 修复（用户反馈"删除报 IntegrityError，实际分支目录都删了但左面板还在"）：
    原实现漏了两类引用，导致 `DELETE FROM tasks ... FOREIGN KEY constraint failed`：
      1. **artifacts.task_id → tasks.id**：产物表引用任务表，必须先删产物再删任务；
      2. memory_entries：非 session 作用域（project/global）的行同样带 session_id 外键，
         "只删会话级记忆"的过滤让它们变成孤儿并阻断 sessions 删除。
    另外补齐 exec_policy_rules / scheduled_tasks / file_reviews（按 turn_id 关联）。

    plan-20261014-fix4：补齐工作流 7 张表——workflow_nodes / workflow_attempts.agent_id 引用 agents.id、
    workflow_runs.session_id 引用 sessions.id，用过工作流的会话此前永久删除必报外键错误。
    派生集合一律用子查询（不把 id 展开成参数），大会话不会撞上 SQLite 的参数个数上限；
    usage_records 是用量统计与首页热力图的数据源，只解除与会话的关联、不删除，删会话不改写历史用量。

    返回各表删除行数（供提示与测试断言）。调用方负责 commit。
    """
    from sqlalchemy import delete as _delete
    from sqlalchemy import select as _select
    from sqlalchemy import update as _update

    from app.persistence.models.agent import Agent
    from app.persistence.models.audit import AuditLog
    from app.persistence.models.exec_policy import ExecPolicyRule
    from app.persistence.models.memory import MemoryEntry
    from app.persistence.models.message import Message
    from app.persistence.models.review import FileReview
    from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
    from app.persistence.models.scheduled import ScheduledTask
    from app.persistence.models.task import Artifact, Task
    from app.persistence.models.tool_call import ToolCall
    from app.persistence.models.turn import Turn
    from app.persistence.models.usage_record import UsageRecord
    from app.persistence.models.workflow import (
        WorkflowAttempt,
        WorkflowEvent,
        WorkflowMessage,
        WorkflowNode,
        WorkflowPlanRevision,
        WorkflowRun,
        WorkflowToolCall,
    )

    counts: dict[str, int] = {}
    ids = sorted({int(x) for x in session_ids})
    if not ids:
        return counts

    def _purge(model, *criteria) -> None:
        res = s.execute(_delete(model).where(*criteria))
        counts[model.__tablename__] = counts.get(model.__tablename__, 0) + int(res.rowcount or 0)

    # 分批：每批会话 id 数远低于 SQLite 参数上限
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        turns_q = _select(Turn.id).where(Turn.session_id.in_(chunk))
        tasks_q = _select(Task.id).where(Task.session_id.in_(chunk))
        runs_q = _select(WorkflowRun.id).where(WorkflowRun.session_id.in_(chunk))
        nodes_q = _select(WorkflowNode.id).where(WorkflowNode.run_id.in_(runs_q))
        attempts_q = _select(WorkflowAttempt.id).where(WorkflowAttempt.node_id.in_(nodes_q))

        # 0) 工作流：工具台账 / 检查点日志 → 尝试 → 节点 → 事件与修订 → 子工作流 → 程序（均先于 agents）
        _purge(WorkflowToolCall, WorkflowToolCall.node_id.in_(nodes_q))
        _purge(WorkflowMessage, WorkflowMessage.attempt_id.in_(attempts_q))
        _purge(WorkflowAttempt, WorkflowAttempt.node_id.in_(nodes_q))
        _purge(WorkflowNode, WorkflowNode.run_id.in_(runs_q))
        _purge(WorkflowEvent, WorkflowEvent.run_id.in_(runs_q))
        _purge(WorkflowPlanRevision, WorkflowPlanRevision.run_id.in_(runs_q))
        _purge(WorkflowRun, WorkflowRun.session_id.in_(chunk), WorkflowRun.parent_run_id.is_not(None))
        _purge(WorkflowRun, WorkflowRun.session_id.in_(chunk))

        # 1) 最底层叶子：**先删 artifacts**（引用 tasks.id；顺序错即报外键错，正是 plan-308-1542 根因）
        _purge(Artifact, Artifact.task_id.in_(tasks_q))
        _purge(Message, Message.session_id.in_(chunk))
        _purge(ToolCall, ToolCall.session_id.in_(chunk))
        _purge(RollbackWrite, RollbackWrite.session_id.in_(chunk))
        _purge(TurnSnapshot, TurnSnapshot.session_id.in_(chunk))
        _purge(AuditLog, AuditLog.session_id.in_(chunk))
        _purge(ExecPolicyRule, ExecPolicyRule.session_id.in_(chunk))
        _purge(ScheduledTask, ScheduledTask.session_id.in_(chunk))
        _purge(FileReview, FileReview.turn_id.in_(turns_q))
        # 2) 记忆：session_id 是 NOT NULL 外键——**所有**引用这些会话的行都必须删，
        #    否则孤儿行会阻断 sessions 删除（project/global 记忆只在删除这些会话时受影响）。
        _purge(MemoryEntry, MemoryEntry.session_id.in_(chunk))
        # 3) 用量流水：只解除关联（保留历史用量）
        res = s.execute(
            _update(UsageRecord)
            .where(UsageRecord.session_id.in_(chunk))
            .values(session_id=None, turn_id=None, agent_id=None)
        )
        counts["usage_records_detached"] = counts.get("usage_records_detached", 0) + int(res.rowcount or 0)
        # 4) 任务与代理，最后 turns
        _purge(Task, Task.session_id.in_(chunk))
        _purge(Agent, Agent.session_id.in_(chunk))
        _purge(Turn, Turn.session_id.in_(chunk))
    return counts


async def delete_session_permanent(db: AsyncSession, session_id: int) -> dict | None:
    """**永久删除**会话及其全部关联数据（plan-282-1441 #4：归档页批量删除）。

    与 `delete_session`（仅置 status="archived"）语义不同：这是不可恢复的物理删除，
    供"设置 → 归档"页的批量删除与工作树级联删除（plan-308-1542）使用。
    会话不存在返回 None。

    关联数据清理统一走 `purge_session_children`（外键安全顺序，含 artifacts）。
    """
    from app.persistence.database import run_write_locked

    def patch(s):
        session = s.get(Session, session_id)
        if session is None:
            return None
        counts = purge_session_children(s, [session_id])
        s.delete(session)
        s.commit()
        return {"ok": True, "deleted": counts}

    result = await run_write_locked(patch, label=f"session.delete_permanent.{session_id}")
    if result is not None:
        _purge_tool_outputs(session_id)
    return result


def _purge_tool_outputs(session_id: int) -> None:
    """plan-156-621：删除会话时清理它的长工具输出落盘目录。

    落盘文件名带 uuid，无法按会话内消息反查；不跟随会话删除会一直累积
    （每次超预算输出最多 2MB）。清理失败只记日志，不影响删除结果。
    """
    import shutil
    from pathlib import Path

    try:
        from app.core.config import settings
        from app.orchestration.output_paging import SPILL_DIRNAME

        folder = Path(settings.uploads_dir).resolve() / SPILL_DIRNAME / str(session_id)
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
    except Exception:  # noqa: BLE001 — 清理失败不影响会话删除
        logger.debug("清理工具输出落盘失败 sid=%s", session_id, exc_info=True)


async def fork_session(db: AsyncSession, session_id: int, title: str | None = None) -> int:
    """复制会话（仅复制元数据与消息，任务/子代理不复制；写引擎单写线程）。"""
    from app.orchestration.approval_policy import (
        normalize_approval_mode, normalize_execution_mode,
    )
    from app.persistence.database import run_write_locked

    def patch(s):
        src = s.get(Session, session_id)
        if src is None:
            raise ValueError("session not found")
        new_session = Session(
            project_id=src.project_id,
            title=title or f"{src.title or '会话'} · 分支",
            # plan-156-619 S8：fork 出的会话标题是正式名称（分支名），
            # 标 named 避免被首条消息的自动归纳覆盖。
            title_source="named",
            model_id=src.model_id,
            fork_parent_id=session_id,
            status="active",
            # plan-75-332 R3 修复：分支会话必须继承源会话的两种模式——
            # 此前未复制，fork 出来一律回到 ORM 默认（agent + 询问审批），
            # 用「完全访问」的分支会突然开始弹审批。
            # R7：继承时经归一化，避免旧值（default / accept_edits）随分支继续扩散。
            permission_mode=normalize_execution_mode(src.permission_mode),
            approval_mode=normalize_approval_mode(src.approval_mode),
        )
        s.add(new_session)
        s.flush()
        # 复制消息
        rows = s.execute(
            select(Message).where(Message.session_id == session_id, Message.deleted == False)  # noqa: E712
        ).scalars().all()
        for m in rows:
            s.add(Message(
                session_id=new_session.id,
                turn_id=None,
                thread_id=None,
                sender_type=m.sender_type,
                sender_id=m.sender_id,
                msg_type=m.msg_type,
                content=m.content,
                token_usage=m.token_usage,
            ))
        s.commit()
        return new_session.id

    return await run_write_locked(patch, label="session.fork")


async def create_system_message(db: AsyncSession, *, session_id: int, content: dict) -> int:
    """v2.2 (对齐 zcode 3.11): 写一条系统消息（模型切换 divider 等，写引擎单写线程）。

    plan-41-229: 旧实现只落库不广播，前端要等到下一次消息刷新（发送消息时触发）
    才把它拉进消息列表——用户反馈「切换模型后必须再发一条消息才提示切换了模型」。
    改走 message_service.create_message：同一条写引擎路径，落库后广播 message.created，
    切换模型当场即出提示；广播失败仍只影响实时性，不影响落库。
    """
    from app.core.enums import MsgType, SenderType
    from app.services.message_service import create_message

    msg = await create_message(
        db, session_id=session_id,
        sender_type=SenderType.SYSTEM.value, sender_id=None,
        msg_type=MsgType.SYSTEM.value, content=content,
    )
    return int(msg.id or 0)


async def list_main_messages(db: AsyncSession, session_id: int, limit: int | None = None) -> list[Message]:
    """主线程消息（thread_id IS NULL），默认过滤已回滚软删消息（问题14）。

    返回最近 limit 条，保持时间正序（最旧在前）；供全局摘要拼接。
    """
    stmt = (
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.thread_id.is_(None),
            Message.deleted == False,  # noqa: E712
        )
        .order_by(Message.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def list_thread_messages(db: AsyncSession, session_id: int, thread_id: int,
                               limit: int | None = None) -> list[Message]:
    """指定子代理线程消息，默认过滤已回滚软删消息（问题14）。

    保持时间正序（最旧在前），供 build_thread_context_with_window 按 token 预算择优。
    """
    stmt = (
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.thread_id == thread_id,
            Message.deleted == False,  # noqa: E712
        )
        .order_by(Message.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())


@dataclass(frozen=True)
class SessionFlags:
    """会话列表行的运行态字段（与下方逐会话函数同口径）。"""

    has_running: bool = False
    workflow_running: bool = False
    has_interrupted_turn: bool = False
    last_activity_at: str | None = None
    running_started_at: str | None = None


# IN 子句单批上限（SQLite 变量数默认上限 999，留足余量）
_FLAGS_BATCH = 500


async def session_flags_batch(db: AsyncSession, sessions: list[Session]) -> dict[int, SessionFlags]:
    """批量计算会话列表的运行态字段：固定 4 条聚合查询，替代逐会话 5 条查询。

    plan-156-620：会话列表此前对每个会话串行跑 has_running / workflow_running /
    has_interrupted / last_activity / running_started 五条查询（N+1）。
    百余个会话时单次 /sessions 要 5~6 秒，启动、切会话、任何触发列表刷新的操作都被拖慢。
    """
    ids = [s.id for s in sessions]
    running: dict[int, str | None] = {}
    interrupted: set[int] = set()
    wf_running: set[int] = set()
    last_msg: dict[int, str | None] = {}
    for i in range(0, len(ids), _FLAGS_BATCH):
        chunk = ids[i:i + _FLAGS_BATCH]
        for sid, started in (await db.execute(
            select(Turn.session_id, func.max(Turn.started_at))
            .where(Turn.session_id.in_(chunk), Turn.status == "running")
            .group_by(Turn.session_id)
        )).all():
            running[sid] = started
        interrupted.update((await db.execute(
            select(Turn.session_id)
            .where(Turn.session_id.in_(chunk), Turn.status == "interrupted")
            .distinct()
        )).scalars().all())
        wf_running.update((await db.execute(
            select(WorkflowRun.session_id)
            .where(WorkflowRun.session_id.in_(chunk), WorkflowRun.status == "running")
            .distinct()
        )).scalars().all())
        for sid, ts in (await db.execute(
            select(Message.session_id, func.max(Message.created_at))
            .where(Message.session_id.in_(chunk), Message.deleted.is_(False))
            .group_by(Message.session_id)
        )).all():
            last_msg[sid] = ts
    return {
        s.id: SessionFlags(
            has_running=s.id in running,
            workflow_running=s.id in wf_running,
            has_interrupted_turn=s.id in interrupted,
            last_activity_at=last_msg.get(s.id) or s.created_at,
            running_started_at=running.get(s.id),
        )
        for s in sessions
    }


async def last_activity_at(db: AsyncSession, session_id: int) -> str | None:
    """最近一条未删除消息时间；无消息回退会话创建时间。"""
    res = await db.execute(
        select(func.max(Message.created_at)).where(
            Message.session_id == session_id,
            Message.deleted.is_(False),
        )
    )
    ts = res.scalar_one_or_none()
    if ts is None:
        s = await db.get(Session, session_id)
        ts = s.created_at if s else None
    return ts


async def has_running_turn(db: AsyncSession, session_id: int) -> bool:
    """会话是否存在运行中的 turn。"""
    res = await db.execute(
        select(Turn.id).where(
            Turn.session_id == session_id,
            Turn.status == "running",
        ).limit(1)
    )
    return res.scalars().first() is not None


async def has_running_workflow(db: AsyncSession, session_id: int) -> bool:
    """会话是否有正在后台执行的工作流程序（status=running）。

    工作流在后台跑时，本会话可能已经没有运行中的 turn（例如「开始执行」只是启动了程序，
    主代理的 turn 随即结束）。侧栏 / 输入框 / 摘要的「执行中」必须把它算进来，否则会出现
    工作流在跑、界面却显示停止的矛盾。
    """
    res = await db.execute(
        select(WorkflowRun.id).where(
            WorkflowRun.session_id == session_id,
            WorkflowRun.status == "running",
        ).limit(1)
    )
    return res.scalars().first() is not None


async def running_turn_started_at(db: AsyncSession, session_id: int) -> str | None:
    """会话正在运行的 turn 的开始时间；无运行中 turn 返回 None。

    侧栏「执行中任务」需要一个运行期间**不变**的排序键：用 last_activity_at 会随
    每条流式消息刷新，多个并发任务互相超车 → 上下跳动。取 turn.started_at 则整个
    执行期恒定，实现「最新开始执行的排最上」的稳定时序。
    """
    res = await db.execute(
        select(Turn.started_at).where(
            Turn.session_id == session_id,
            Turn.status == "running",
        ).order_by(Turn.started_at.desc(), Turn.id.desc()).limit(1)
    )
    return res.scalar_one_or_none()


async def has_interrupted_turn(db: AsyncSession, session_id: int) -> bool:
    res = await db.execute(
        select(Turn.id).where(
            Turn.session_id == session_id,
            Turn.status == "interrupted",
        ).limit(1)
    )
    return res.scalars().first() is not None
